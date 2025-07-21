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
from care_scribe.models.scribe import Scribe
from care_scribe.models.scribe_file import ScribeFile
from care_scribe.settings import plugin_settings
from google.genai import types
from google import genai
from google.oauth2 import service_account
import copy

from care_scribe.utils import hash_string

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
    base_prompt = textwrap.dedent(
        """
        You'll receive a patient's encounter (text, audio, or image). Extract all valid data from the encounter and fill the form with it.

        Rules:
        • Use only the readable term for coded entries (e.g., “Brain Hemorrhage” from “A32Q Brain Hemorrhage”).
        • Translate non-English content to English before responding.
        {transcript_instructions}

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
    base_prompt = base_prompt.replace("{current_date_time}", datetime.datetime.now().isoformat())
    form = Scribe.objects.get(external_id=external_id, status=Scribe.Status.READY)

    is_benchmark = form.meta.get("benchmark", False)

    # Verify if the user/facility has not exceeded their quota and has accepted the terms and conditions
    user_quota = None
    facility_quota = None
    if not is_benchmark:
        user_quota = form.requested_by.scribe_quota.filter(facility=form.requested_in_facility).first()
        facility_quota = form.requested_in_facility.scribe_quota.filter(user=None).first()

        if not facility_quota:
            form.meta["error"] = "Facility does not have a scribe quota."
            form.status = Scribe.Status.FAILED
            form.save()
            return

        if not user_quota:
            form.meta["error"] = "User does not have a scribe quota."
            form.status = Scribe.Status.FAILED
            form.save()
            return

        tnc = plugin_settings.SCRIBE_TNC
        tnc_hash = hash_string(tnc)

        if user_quota.tnc_hash != tnc_hash:
            form.meta["error"] = "User has not accepted the latest terms and conditions."
            form.status = Scribe.Status.FAILED
            form.save()
            return

        if facility_quota.used >= facility_quota.tokens:
            form.meta["error"] = "Facility has exceeded its scribe quota."
            form.status = Scribe.Status.FAILED
            form.save()
            return

        if user_quota.used >= facility_quota.tokens_per_user:
            form.meta["error"] = "User has exceeded their scribe quota."
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

    form.meta["provider"] = api_provider
    form.meta["chat_model"] = chat_model
    form.meta["audio_model"] = audio_model

    audio_files = ScribeFile.objects.filter(external_id__in=form.audio_file_ids)

    total_audio_duration = sum(file.meta.get("length", 0) for file in audio_files)

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

    processed_fields = {}

    def process_fields(fields: list):

        for fd in fields:
            if "fields" in fd:
                process_fields(fd["fields"])
            else:  # It's a Field
                schema = fd.get("schema", {})
                field_id = fd.get("id", "")

                keys_to_remove = {"$schema", "const", "$ref", "$defs", "property_ordering"}
                if api_provider != "openai":
                    keys_to_remove.add("additionalProperties")

                schema = remove_keys(schema, keys_to_remove)
                processed_fields[field_id] = schema

    for qn in form.form_data:
        process_fields(qn["fields"])

    # divide the processed fields into chunks
    chunk_size = 40

    if api_provider == "google":
        chunk_size = 20

    processed_fields_no_keys = {f"q{i}": v for i, (k, v) in enumerate(processed_fields.items())}

    items = list(processed_fields_no_keys.items())

    iterations = [
        dict(items[i:i + chunk_size])
        for i in range(0, len(items), chunk_size)
    ]
    logger.info(str(len(iterations)) + " chunks to process for form " + str(form.external_id))
    full_response = {}
    meta_iterations = []

    for idx, iteration in enumerate(iterations):
        output_schema = {
                "type": "object",
                "properties": {
                    **iteration,
                    "__scribe__transcription": {
                        "type": "string",
                        "description": "The transcription of the audio or text content, or a summary of the image content.",
                    }
                },
                "required": ["__scribe__transcription"],
        }
        initiation_time = perf_counter()
        if idx == 0 and api_provider == "google":
            if total_audio_duration > (3 * 60 * 1000):
                prompt = base_prompt.replace(
                    "{transcript_instructions}",
                    "• If specified in tool call, after filling the form, return a short summarized transcription of the audio content, focusing on key points and insights in English as `__scribe__transcription`.",
                )
                output_schema["properties"]["__scribe__transcription"]["description"] = "A short summarized transcription of the audio content, focusing on key points and insights in English."
            else:
                prompt = base_prompt.replace(
                    "{transcript_instructions}",
                    "• If specified in tool call, after filling the form, return the transcription with the original content (text, transcript, or image summary) in English as `__scribe__transcription`.",
                )
        else:
            prompt = base_prompt.replace(
                "{transcript_instructions}",
                ""
            )
        this_iteration = {}

        if idx > 0 or (api_provider != "google" and len(form.document_file_ids) == 0):
            del output_schema["properties"]["__scribe__transcription"]
            output_schema["required"].remove("__scribe__transcription")

        if api_provider != "google":
            output_schema = fill_missing_types(output_schema)

        logger.info(f"=== Processing AI form fill {form.external_id} ===")

        this_iteration = {"function": output_schema, "prompt": (form.prompt or prompt)}

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
                logger.info(f"Audio file objects: {audio_files}")

                for audio_file_object in audio_files:
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
                # print(json.dumps(output_schema, indent=2))
                ai_response = ai_client(api_provider).models.generate_content(
                    model=chat_model,
                    contents=messages,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_schema=output_schema,
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

                if ai_response.candidates[0].finish_reason != types.FinishReason.STOP:
                    raise Exception(f"AI response did not finish successfully: {str(ai_response.candidates[0].finish_reason)}")

                ai_response_json = ai_response.parsed

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
                            "name" : "process_ai_form_fill",
                            "schema" : {
                                **output_schema,
                                "required" : [key for key, value in output_schema["properties"].items()],
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

    total_input_tokens = sum(iteration.get("completion_input_tokens", 0) for iteration in meta_iterations)
    total_output_tokens = sum(iteration.get("completion_output_tokens", 0) for iteration in meta_iterations)

    form.chat_input_tokens = total_input_tokens
    form.chat_output_tokens = total_output_tokens

    form.status = Scribe.Status.COMPLETED

    # convert the keys back to the original field IDs
    full_response = {k: full_response.get(f"q{i}") for i,(k, v) in enumerate(processed_fields.items()) if full_response.get(f"q{i}") is not None}
    full_response["__scribe__transcription"] = form.transcript
    form.ai_response = full_response
    form.save()

    if not is_benchmark:
        user_quota.calculate_used()
        facility_quota.calculate_used()
