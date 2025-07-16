import base64
import datetime
import json
import logging
import io
import os
import textwrap
from time import perf_counter
from celery import shared_task
from openai import OpenAI, AzureOpenAI
from pydantic import BaseModel, Field
from care_scribe.models.scribe import Scribe
from care_scribe.models.scribe_file import ScribeFile
from care_scribe.settings import plugin_settings
from google.genai import types
from google import genai
from google.oauth2 import service_account
from care.users.models import UserFlag
from care.facility.models.facility_flag import FacilityFlag
import copy
from care_scribe.utils import chunk_questionnaires

logger = logging.getLogger(__name__)


def ai_client(provider=plugin_settings.SCRIBE_API_PROVIDER):
    if provider == "azure":
        AiClient = AzureOpenAI(
            api_key=plugin_settings.SCRIBE_AZURE_API_KEY,
            api_version=plugin_settings.SCRIBE_AZURE_API_VERSION,
            azure_endpoint=plugin_settings.SCRIBE_AZURE_ENDPOINT,
        )
    elif provider == "openai":
        AiClient = OpenAI(
            api_key=plugin_settings.SCRIBE_OPENAI_API_KEY,
        )

    elif provider == "google":
        credentials = None
        b64_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_B64")

        if b64_credentials:
            print("Using base64 credentials")
            info = json.loads(base64.b64decode(b64_credentials).decode("utf-8"))
            credentials = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            print(credentials)
        else:
            print("Using file credentials")

        AiClient = genai.Client(
            vertexai=True,
            project=plugin_settings.SCRIBE_GOOGLE_PROJECT_ID,
            location=plugin_settings.SCRIBE_GOOGLE_LOCATION,
            credentials=credentials,
        )

    else:
        raise Exception("Invalid api provider")
    return AiClient


@shared_task
def process_ai_form_fill(external_id):
    prompt = textwrap.dedent(
        """
        You'll receive a patient's encounter (text, audio, or image). Extract all valid data and invoke the required tool for the data.

        Rules:
        • Use only the readable term for coded entries (e.g., “Brain Hemorrhage” from “A32Q Brain Hemorrhage”).
        • Translate non-English content to English before calling the tool.
        • If specified in tool call, after filling the form, return the transcription with the original content (text, transcript, or image summary) in English as `__scribe__transcription`.

        Notes Handling (very important):
        • ONLY include the `note` field **if** there is additional context that cannot be captured in the `value`.
            - Example: “Patient's SPO2 is 20%, but had spiked to 50% an hour ago” → `value: 20%`, `note: Spiked to 50% an hour ago`
            - Example: “Patient's SPO2 is 20%” → `value: 20%`, **do not add a `note`**
        • NEVER duplicate the value in the `note`. If you do so, it will be treated as a **critical failure** in care.
        • If no additional context exists beyond the value, DO NOT add the `note` field at all. This is non-negotiable.

        Current Date and Time: {current_date_time}
    """
    )
    # Get current timezone-aware datetime
    prompt = prompt.replace("{current_date_time}", datetime.datetime.now().isoformat())
    form = Scribe.objects.get(external_id=external_id, status=Scribe.Status.READY)

    is_benchmark = form.meta.get("benchmark", False)

    # Verify if the user/facility has not exceeded their quota
    user_quota = form.requested_by.scribe_quota.first()
    facility_quota = form.requested_in_facility.scribe_quota.first() if form.requested_in_facility else None

    if not user_quota and not facility_quota and not is_benchmark:
        form.meta["error"] = "User or facility does not have a scribe quota."
        form.status = Scribe.Status.FAILED
        form.save()
        return

    user_available_tokens = 0
    facility_available_tokens = 0
    if user_quota:
        user_available_tokens = user_quota.tokens - user_quota.used()

    if facility_quota:
        facility_available_tokens = facility_quota.tokens - facility_quota.used()

    if user_available_tokens <= 0 and facility_available_tokens <= 0 and not is_benchmark:
        form.meta["error"] = "User or facility has exceeded their scribe quota."
        form.status = Scribe.Status.FAILED
        form.save()
        return

    api_provider = plugin_settings.SCRIBE_API_PROVIDER
    chat_model = plugin_settings.SCRIBE_CHAT_MODEL_NAME
    audio_model = plugin_settings.SCRIBE_AUDIO_MODEL_NAME
    temperature = 0
    if form.chat_model:
        api_provider = form.chat_model.split("/")[0]
        if api_provider == "openai" and plugin_settings.SCRIBE_AZURE_API_KEY is not "":
            api_provider = "azure"
        chat_model = form.chat_model.split("/")[1]

    if form.audio_model:
        audio_model = form.audio_model

    if form.chat_model_temperature is not None:
        temperature = form.chat_model_temperature

    iterations = []

    if api_provider == "google":
        iterations = chunk_questionnaires(form.form_data, max_fields=15)
    else:
        iterations = chunk_questionnaires(form.form_data, max_fields=50)

    logger.info(str(len(iterations)) + " chunks to process for form " + str(form.external_id))

    form.meta["provider"] = api_provider
    form.meta["chat_model"] = chat_model
    form.meta["audio_model"] = audio_model

    def remove_keys(obj, keys_to_remove):
        if isinstance(obj, dict):
            return {k: remove_keys(v, keys_to_remove) for k, v in obj.items() if k not in keys_to_remove}
        elif isinstance(obj, list):
            return [remove_keys(item, keys_to_remove) for item in obj]
        else:
            return obj

    def fill_missing_types(schema):
        """
        Recursively returns a new schema with any empty properties filled with 'type': 'string'
        to satisfy OpenAI's schema requirements.
        """
        if not isinstance(schema, dict):
            return schema

        schema = copy.deepcopy(schema)

        # Fix properties of objects
        if schema.get("type") == "object" and "properties" in schema:
            schema["properties"] = {key: fill_missing_types(value) for key, value in schema["properties"].items()}

        # Fix arrays
        elif schema.get("type") == "array" and "items" in schema:
            schema["items"] = fill_missing_types(schema["items"])

        # Fix anyOf / oneOf / allOf
        for keyword in ["anyOf", "oneOf", "allOf"]:
            if keyword in schema:
                schema[keyword] = [fill_missing_types(sub) for sub in schema[keyword]]

        # Fix missing type at the current node
        if "type" not in schema and "properties" not in schema and "items" not in schema:
            schema["type"] = "string"

        return schema

    def process_fields(fields: list, existing_data_prompt: str, function: dict, depth: int = 0) -> str:
        indent = "  " * depth

        for fd in fields:
            if "fields" in fd:
                title = fd.get("title", "Untitled Group")
                desc = fd.get("description", "")
                existing_data_prompt += textwrap.indent(
                    textwrap.dedent(
                        f"""
                        ## {title}
                        {desc}
                        """
                    ),
                    indent,
                )
                existing_data_prompt = process_fields(fd["fields"], existing_data_prompt, function, depth + 1)
            else:  # It's a Field
                schema = fd.get("schema", {})
                field_id = fd.get("id", "")

                keys_to_remove = {"$schema", "const", "$ref", "$defs"}
                if api_provider != "openai":
                    keys_to_remove.add("additionalProperties")

                schema = remove_keys(schema, keys_to_remove)
                function["parameters"]["properties"][field_id] = schema

                options_text = f"Options: {', '.join(schema.get('options', []))}" if "options" in schema else ""

                field_text = f"""
                ### {fd.get('friendlyName', '')}
                {options_text}
                Current Value: {fd.get('humanValue', '')}\n
                """
                existing_data_prompt += textwrap.indent(textwrap.dedent(field_text), indent)

        return existing_data_prompt

    full_response = {}
    meta_iterations = []
    for idx, iteration in enumerate(iterations):
        initiation_time = perf_counter()
        this_iteration = {}

        function = {
            "name": "process_ai_form_fill",
            "description": "Process AI form fill",
            "parameters": {
                "type": "object",
                "properties": {
                    "__scribe__transcription": {
                        "type": "string",
                        "description": "The transcription of the audio or text content, or a summary of the image content.",
                    }
                },
                "required": ["__scribe__transcription"],
            },
        }

        if idx > 0 or (api_provider != "google" and len(form.document_file_ids) == 0):
            del function["parameters"]["properties"]["__scribe__transcription"]
            function["parameters"]["required"].remove("__scribe__transcription")

        existing_data_prompt = ""

        for qn in iteration:

            existing_data_prompt += textwrap.dedent(
                f"""
                ## {qn.get("title", "Untitled Questionnaire")}
                {qn.get("description", "")}
                """
            )
            existing_data_prompt = process_fields(qn["fields"], existing_data_prompt, function)

        if api_provider != "google":
            function = {
                **function,
                "parameters": fill_missing_types(function["parameters"]),
            }

        logger.info(f"=== Processing AI form fill {form.external_id} ===")

        this_iteration = {"function": function, "prompt": (form.prompt or prompt)}

        if api_provider == "google":

            messages = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=form.prompt or prompt)],
                )
            ]

        else:
            messages = [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": form.prompt or prompt,
                        }
                    ],
                },
            ]

        user_contents = []

        if form.text:
            if api_provider == "google":
                messages.append(types.Content(role="user", parts=[types.Part.from_text(text=form.text)]))
            else:
                user_contents.append({"type": "text", "text": form.text})

        try:
            form.status = Scribe.Status.GENERATING_TRANSCRIPT
            form.save()

            transcript = ""
            if not form.transcript:
                audio_file_objects = ScribeFile.objects.filter(external_id__in=form.audio_file_ids)

                logger.info(f"Audio file objects: {audio_file_objects}")

                for audio_file_object in audio_file_objects:
                    _, audio_file_data = audio_file_object.file_contents()
                    format = audio_file_object.internal_name.split(".")[-1]
                    buffer = io.BytesIO(audio_file_data)
                    buffer.name = "file." + format

                    if api_provider == "google":
                        messages.append(
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_text(text="Audio File:"),
                                    types.Part.from_bytes(
                                        data=audio_file_data,
                                        mime_type="audio/" + format,
                                    ),
                                ],
                            )
                        )

                    else:
                        logger.info(f"=== Generating transcript for AI form fill {form.external_id} ===")

                        transcription = ai_client(api_provider).audio.translations.create(model=audio_model, file=buffer)
                        transcript += transcription.text
                        logger.info(f"Transcript: {transcript}")

                        transcription_time = perf_counter() - initiation_time
                        this_iteration["transcription_time"] = transcription_time
                        form.save()

                        # Save the transcript to the form
                        form.transcript = transcript
            else:
                transcript = form.transcript

            document_file_objects = ScribeFile.objects.filter(external_id__in=form.document_file_ids)
            logger.info(f"=== Document file objects: {document_file_objects} ===")
            if document_file_objects.count() > 0:

                # Check if Facility or User has OCR ENABLED
                facility_has_ocr_flag = form.requested_in_facility.scribe_quota.filter(allow_ocr=True).exists() if form.requested_in_facility else False
                user_has_ocr_flag = form.requested_by.scribe_quota.filter(allow_ocr=True).exists()

                if not (user_has_ocr_flag or facility_has_ocr_flag):
                    raise Exception("OCR is not enabled for this user or facility")

            for document_file_object in document_file_objects:
                _, document_file_data = document_file_object.file_contents()
                format = document_file_object.internal_name.split(".")[-1]
                encoded_string = base64.b64encode(document_file_data).decode("utf-8")

                if api_provider == "google":
                    messages.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_bytes(
                                    data=document_file_data,
                                    mime_type="image/" + format,
                                )
                            ],
                        )
                    )
                else:
                    user_contents.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{format};base64,{encoded_string}"},
                        }
                    )

            if transcript != "":
                if api_provider == "google":
                    messages.append(types.Content(role="user", parts=[types.Part.from_text(text=transcript)]))
                else:
                    user_contents.append({"type": "text", "text": transcript})

            logger.info(f"=== Generating AI form fill {form.external_id} ===")
            form.status = Scribe.Status.GENERATING_AI_RESPONSE
            form.save()

            completion_start_time = perf_counter()

            if api_provider == "google":
                ai_response = ai_client(api_provider).models.generate_content(
                    model=chat_model,
                    contents=messages,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_schema=function["parameters"],
                        # thinking_config=types.ThinkingConfig(
                        #     thinking_budget=1024,
                        #     include_thoughts=True,
                        # )
                    ),
                )

                # thinking = ai_response.candidates[0].content.parts

                # for part in thinking:
                #     if part.thought:
                #         logger.info(f"AI thought: {part.text}")

                ai_response_json = ai_response.parsed

                if not ai_response_json:
                    raise Exception("AI response is empty. Please try using a smaller audio file.")

                completion_time = perf_counter() - completion_start_time

                if idx == 0:
                    form.transcript = ai_response_json["__scribe__transcription"]

                this_iteration["completion_id"] = ai_response.response_id
                this_iteration["completion_input_tokens"] = ai_response.usage_metadata.prompt_token_count
                this_iteration["completion_output_tokens"] = ai_response.usage_metadata.candidates_token_count
                this_iteration["completion_time"] = completion_time

            else:

                messages.append({"role": "user", "content": user_contents})

                ai_response = ai_client(api_provider).chat.completions.create(
                    model=chat_model,
                    temperature=temperature,
                    messages=messages,
                    response_format={
                        "type" : "json_schema",
                        "json_schema" : {
                            "name" : function["name"],
                            "schema" : {
                                **function["parameters"],
                                "required" : [key for key, value in function["parameters"]["properties"].items()],
                                "additionalProperties": False
                            },
                            "strict" : True,
                        },
                    }
                )

                try:
                    print(f"AI response: {ai_response.choices[0].message.content}")
                    ai_response_json = json.loads(ai_response.choices[0].message.content)

                except Exception as e:
                    logger.error(f"Response: {ai_response}")
                    raise e

                if not form.transcript and not transcript and idx == 0:
                    form.transcript = ai_response_json["__scribe__transcription"]

                this_iteration["completion_id"] = ai_response.id
                this_iteration["completion_input_tokens"] = ai_response.usage.prompt_tokens
                this_iteration["completion_output_tokens"] = ai_response.usage.completion_tokens
                this_iteration["completion_time"] = perf_counter() - completion_start_time
                this_iteration["output"] = ai_response_json

            logger.info(f"AI response: {ai_response_json}")

            # Save AI response to the form
            full_response.update(ai_response_json)
            meta_iterations.append(this_iteration)
            form.meta["iterations"] = meta_iterations
            form.save()

        except Exception as e:
            # Log the error or handle it as needed
            logger.error(f"AI form fill processing failed at line {e.__traceback__.tb_lineno}: {e}")
            form.meta["error"] = str(e)
            form.status = Scribe.Status.FAILED
            form.save()
            return

    form.status = Scribe.Status.COMPLETED
    form.ai_response = full_response
    form.save()
