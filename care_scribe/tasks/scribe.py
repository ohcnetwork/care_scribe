import base64
import datetime
import json
import logging
import io
import os
import textwrap
from time import perf_counter
from typing import Annotated

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

logger = logging.getLogger(__name__)

AiClient = None


def ai_client():
    global AiClient
    if AiClient is None:
        if plugin_settings.SCRIBE_API_PROVIDER == "azure":
            AiClient = AzureOpenAI(
                api_key=plugin_settings.SCRIBE_PROVIDER_API_KEY,
                api_version=plugin_settings.SCRIBE_AZURE_API_VERSION,
                azure_endpoint=plugin_settings.SCRIBE_AZURE_ENDPOINT,
            )
        elif plugin_settings.SCRIBE_API_PROVIDER == "openai":
            AiClient = OpenAI(
                api_key=plugin_settings.SCRIBE_PROVIDER_API_KEY,
            )

        elif plugin_settings.SCRIBE_API_PROVIDER == "google":
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
            raise Exception("Invalid API_PROVIDER in plugin_settings")
    return AiClient


class Transcription(BaseModel):
    transcription: str = Field(
        ...,
        description="The transcription of the audio or text content, or a summary of the image content. (In English)",
    )


@shared_task
def process_ai_form_fill(external_id):
    prompt = textwrap.dedent(
        """
        You'll receive a patient's encounter (text, audio, or image). Extract all valid data per the given form and invoke the required tool for the data.

        Rules:
            •	Extract only confirmed data. Omit anything uncertain or missing.
            •	Use only the readable term for coded entries (e.g., “Brain Hemorrhage” from “A32Q Brain Hemorrhage”).
            •	For required fields, include only if data is available.
            •	Don't guess, assume, or include data marked “entered in error.”
            •	If relevant info doesn't fit the schema but other_details exists, put it there.
            •	Don't mutate existing data in array fields marked as “current” unless user asks.
            •   ONLY fill in what the user has requested. Do not fill in any other fields. If the user has not requested any fields, do not fill in any fields.
            •	After filling the form, return the transcription with the original text / transcript / image summary (in English) as __scribe__transcription.

        Important:
            •	Do not return JSON or any output—only call the tool.
            •	Translate non-English content to English before calling the tool.
        
        Current Date and Time: {current_date_time}

        # Form

        {form_schema}
    """
    )
    # Get current timezone-aware datetime
    prompt = prompt.replace("{current_date_time}", datetime.datetime.now().isoformat())

    ai_form_fills = Scribe.objects.filter(external_id=external_id, status=Scribe.Status.READY)

    for form in ai_form_fills:

        iterations = []

        if plugin_settings.SCRIBE_API_PROVIDER == "google":
            print("Using Google as provider, will chunk form data")

            current_chunk = []
            current_field_count = 0
            max_fields = 20

            for questionnaire_index, questionnaire in enumerate(form.form_data):
                title = questionnaire["title"]
                description = questionnaire["description"]
                fields = questionnaire["fields"]

                print(f"\nProcessing questionnaire {questionnaire_index}: '{title}' with {len(fields)} fields")

                i = 0
                while i < len(fields):
                    remaining_capacity = max_fields - current_field_count
                    take = min(remaining_capacity, len(fields) - i)
                    field_chunk = fields[i : i + take]

                    if current_field_count == 0:
                        # Start a new questionnaire section
                        current_chunk.append({"title": title, "description": description, "fields": field_chunk})
                        print(f"  -> Starting new chunk with {take} fields")
                    else:
                        # Check if we can append to the last questionnaire in the current chunk
                        last = current_chunk[-1]
                        if last["title"] == title and last["description"] == description:
                            last["fields"].extend(field_chunk)
                            print(f"  -> Appending {take} fields to existing section in current chunk")
                        else:
                            current_chunk.append({"title": title, "description": description, "fields": field_chunk})
                            print(f"  -> Adding new section to current chunk with {take} fields")

                    current_field_count += take
                    i += take

                    # If we reach max_fields, commit current_chunk and reset
                    if current_field_count == max_fields:
                        print(f"  -> Max fields reached, committing chunk with {current_field_count} fields")
                        iterations.append(current_chunk)
                        current_chunk = []
                        current_field_count = 0

            # Add any leftover fields in the last chunk
            if current_chunk:
                print(f"Final chunk has {current_field_count} fields, committing.")
                iterations.append(current_chunk)

        else:
            print("Using OpenAI/Azure provider, no chunking needed")
            iterations = [form.form_data]

        print(len(iterations), "iterations to process for form", form.external_id)

        form.meta["provider"] = plugin_settings.SCRIBE_API_PROVIDER
        form.meta["chat_model"] = plugin_settings.SCRIBE_CHAT_MODEL_NAME
        form.meta["audio_model"] = plugin_settings.SCRIBE_AUDIO_MODEL_NAME

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

        full_response = {}
        meta_iterations = []
        for iteration in iterations:
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

            existing_data_prompt = ""

            for qn in iteration:

                existing_data_prompt += textwrap.dedent(
                    f"""
                    ## {qn.get("title", "Untitled Questionnaire")}
                    {qn.get("description", "")}
                    """
                )

                for fd in qn["fields"]:

                    schema = fd.get("schema", {})

                    id = fd.get("id", "")

                    keys_to_remove = {"$schema", "const", "$ref", "$defs"}
                    if plugin_settings.SCRIBE_API_PROVIDER != "openai":
                        keys_to_remove.add("additionalProperties")
                    schema = remove_keys(schema, keys_to_remove)

                    function["parameters"]["properties"][id] = schema

                    existing_data_prompt += textwrap.dedent(
                        f"""
                    ### {fd.get('friendlyName', '')}
                    {"Options: " + ", ".join(schema.get('options', [])) if 'options' in schema else ''}
                    Current Value: {fd.get('humanValue', '')}\n
                    """
                    )

            if plugin_settings.SCRIBE_API_PROVIDER == "openai":
                function = {
                    **function,
                    "parameters": fill_missing_types(function["parameters"]),
                }

            logger.info(f"=== Processing AI form fill {form.external_id} ===")

            this_iteration = {"function": function, "prompt": (form.prompt or prompt).replace("{form_schema}", existing_data_prompt)}

            if plugin_settings.SCRIBE_API_PROVIDER == "google":

                messages = [
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=form.prompt or prompt.replace("{form_schema}", existing_data_prompt))],
                    )
                ]

            else:
                messages = [
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": form.prompt or prompt.replace("{form_schema}", existing_data_prompt),
                            }
                        ],
                    },
                ]

            user_contents = []

            if form.text:
                if plugin_settings.SCRIBE_API_PROVIDER == "google":
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

                        if plugin_settings.SCRIBE_API_PROVIDER == "google":
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

                            transcription = ai_client().audio.translations.create(model=plugin_settings.SCRIBE_AUDIO_MODEL_NAME, file=buffer)
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
                    facility_has_ocr_flag = FacilityFlag.check_facility_has_flag(form.requested_in_facility.id, "SCRIBE_OCR_ENABLED")
                    user_has_ocr_flag = UserFlag.check_user_has_flag(form.requested_by.id, "SCRIBE_OCR_ENABLED")

                    if not (user_has_ocr_flag or facility_has_ocr_flag):
                        raise Exception("OCR is not enabled for this user or facility")

                for document_file_object in document_file_objects:
                    _, document_file_data = document_file_object.file_contents()
                    format = document_file_object.internal_name.split(".")[-1]
                    encoded_string = base64.b64encode(document_file_data).decode("utf-8")

                    if plugin_settings.SCRIBE_API_PROVIDER == "google":
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
                    if plugin_settings.SCRIBE_API_PROVIDER == "google":
                        messages.append(types.Content(role="user", parts=[types.Part.from_text(text=transcript)]))
                    else:
                        user_contents.append({"type": "text", "text": transcript})

                logger.info(f"=== Generating AI form fill {form.external_id} ===")
                form.status = Scribe.Status.GENERATING_AI_RESPONSE
                form.save()

                completion_start_time = perf_counter()

                logger.info("=== Function Call ===")
                logger.info(json.dumps(function, indent=2))
                logger.info("=== End of Function Call ===")

                if plugin_settings.SCRIBE_API_PROVIDER == "google":
                    ai_response = ai_client().models.generate_content(
                        model=plugin_settings.SCRIBE_CHAT_MODEL_NAME,
                        contents=messages,
                        config=types.GenerateContentConfig(
                            temperature=0,
                            max_output_tokens=8192,
                            tools=[types.Tool(function_declarations=[function])],
                            tool_config=types.ToolConfig(
                                function_calling_config=types.FunctionCallingConfig(
                                    mode=types.FunctionCallingConfigMode.ANY,
                                ),
                            ),
                        ),
                    )

                    try:

                        if ai_response.candidates[0].content.parts[0].function_call:
                            function_call = ai_response.candidates[0].content.parts[0].function_call
                            logger.info(f"Function to call: {function_call.name}")
                            ai_response_json = function_call.args
                        else:
                            logger.info("No function call found in the response.")
                            logger.info(ai_response.text)
                            ai_response_json = {"__scribe__transcription": ai_response.text}
                    except Exception as e:
                        logger.error(f"Response: {ai_response}")
                        raise e

                    completion_time = perf_counter() - completion_start_time

                    form.transcript = ai_response_json["__scribe__transcription"]

                    this_iteration["completion_id"] = ai_response.response_id
                    this_iteration["completion_input_tokens"] = ai_response.usage_metadata.prompt_token_count
                    this_iteration["completion_output_tokens"] = ai_response.usage_metadata.candidates_token_count
                    this_iteration["completion_time"] = completion_time

                else:

                    messages.append({"role": "user", "content": user_contents})

                    ai_response = ai_client().chat.completions.create(
                        model=plugin_settings.SCRIBE_CHAT_MODEL_NAME,
                        max_tokens=10000,
                        temperature=0,
                        messages=messages,
                        tools=[
                            {
                                "type": "function",
                                "function": {
                                    **function,
                                    "parameters": {**function["parameters"], "additionalProperties": False},
                                },
                            }
                        ],
                    )

                    try:
                        if ai_response.choices[0].message.tool_calls:
                            function_call = ai_response.choices[0].message.tool_calls[0]
                            logger.info(f"Function to call: {function_call.function.name}")
                            ai_response_json = json.loads(function_call.function.arguments)
                        else:
                            logger.info("No function call found in the response.")
                            logger.info(ai_response.choices[0].message.content)
                            ai_response_json = {"__scribe__transcription": ai_response.choices[0].message.content}
                    except Exception as e:
                        logger.error(f"Response: {ai_response}")
                        raise e

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
