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

from care_scribe.utils import hash_string, remove_keys

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
            info = json.loads(base64.b64decode(b64_credentials).decode("utf-8"))
            credentials = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/cloud-platform"])

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
        You will receive a patient's encounter in the form of text, audio, or image. Your task is to extract all relevant data and populate the specified form fields accordingly. Follow the instructions and rules meticulously to ensure accuracy and compliance.

        Instructions:
        1. Analyze the encounter content thoroughly to identify and extract valid data.
        2. Use readable terms for coded entries (e.g., convert “A32Q Brain Hemorrhage” to “Brain Hemorrhage”).
        3. If the encounter contains non-English content, translate it to English before processing.
        4. If the audio or image contains no relevant data, return an empty string for the transcription field, and do not assume any context or information.

        Notes Handling:
        - Populate the `note` field only if there is additional context that cannot be captured in the `value`.
        - For example, if the encounter states, “Patient's SPO2 is 20%, but had spiked to 50% an hour ago,” then you should fill `value: 20%` and `note: Spiked to 50% an hour ago`.
        - If the encounter simply states, “Patient's SPO2 is 20%,” set note as null (NEVER PUT null as a string).
        - If additional context does not exist beyond the value, set `note` field to null (NEVER PUT null as a string).

        Current Date and Time: {current_date_time}
    """
    )
    # Get current timezone-aware datetime
    form = Scribe.objects.get(external_id=external_id, status=Scribe.Status.READY)
    if form.prompt:
        base_prompt = form.prompt
    base_prompt = base_prompt.replace("{current_date_time}", datetime.datetime.now().isoformat())
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

    # Instantiate the AI client once to avoid premature closure and resource management issues,
    # especially with the Google GenAI provider. Reuse this client instance throughout the function.
    client = ai_client(api_provider)

    audio_files = ScribeFile.objects.filter(external_id__in=form.audio_file_ids)
    total_audio_duration = sum(file.meta.get("length", 0) for file in audio_files)

    processed_fields = {}
    field_tree = ""

    def process_fields(fields: list, indent: int = 0):
        nonlocal field_tree
        for fd in fields:
            if "fields" in fd:
                group_name = fd.get("title", "Group")
                field_tree += "  " * indent + f"{group_name}\n"
                process_fields(fd["fields"], indent + 1)
            else:
                schema = fd.get("schema", {})
                field_id = fd.get("id", "")
                field_name = fd.get("friendlyName", field_id)
                field_tree += "  " * indent + f"-> {field_name}\n"
                processed_fields[field_id] = schema


    for qn in form.form_data:
        process_fields(qn["fields"])

    # base_prompt = base_prompt.replace("{fields}", field_tree)

    keys_to_remove = {"$schema", "const", "$ref", "$defs", "property_ordering"}
    if api_provider != "openai":
        keys_to_remove.add("additionalProperties")

    processed_fields = remove_keys(processed_fields, keys_to_remove)

    # divide the processed fields into chunks
    chunk_size = 40

    if api_provider == "google":
        chunk_size = 200

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

        if idx == 0:
            if api_provider == "google" and total_audio_duration > (3 * 60 * 1000):
                output_schema["properties"]["__scribe__transcription"]["description"] = "A short summarized transcription of the audio content, focusing on key points and insights in English."

            if len(form.document_file_ids) > 0:
                output_schema["properties"]["__scribe__transcription"]["description"] = "A short summarized transcription of the image content, focusing on key points and insights in English."

        else:
            del output_schema["properties"]["__scribe__transcription"]
            output_schema["required"].remove("__scribe__transcription")

        logger.info(f"=== Processing AI form fill {form.external_id} ===")

        this_iteration = {"function": output_schema, "prompt": base_prompt }

        if api_provider == "google":
            messages = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=base_prompt)],
                )
            ]

        else:
            messages = [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": base_prompt,
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

                        transcription = client.audio.translations.create(model=audio_model, file=buffer)
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

                output_schema_hash = hash_string(json.dumps(output_schema, sort_keys=True))
                cache_list = client.caches.list()

                try:
                    existing_cache =  next((cache for cache in cache_list if cache.display_name == f"scribe_{output_schema_hash}" and cache.model == chat_model), None)
                    print(f"Using existing cache: {existing_cache.name}")
                except:
                    existing_cache = None

                if not existing_cache:
                    print(f"Creating new cache for scribe_{output_schema_hash}")
                    try:
                        existing_cache = client.caches.create(
                            model=chat_model,
                            config=types.CreateCachedContentConfig(
                                display_name=f"scribe_{output_schema_hash}",
                                tools=[
                                    types.Tool(
                                        function_declarations=[{
                                            "name": "process_ai_form_fill",
                                            "description": "Process the AI form fill and return the filled form data.",
                                            "parameters": output_schema,
                                        }]
                                    )
                                ],
                                tool_config= types.ToolConfig(
                                    function_calling_config=types.FunctionCallingConfig(
                                        mode=types.FunctionCallingConfigMode.ANY
                                    )
                                ),
                                ttl="86400s"
                            )
                        )
                    except Exception as e:
                        print(f"Error creating cache: {e}")
                        existing_cache = None

                will_use_cache = existing_cache and existing_cache.usage_metadata.total_token_count > 1024
                if will_use_cache:
                    this_iteration["cache_name"] = existing_cache.name
                    print(f"CACHED TOKEN COUNT: {existing_cache.usage_metadata.total_token_count}")

                else:
                    print(f"Cache is not large enough, will not use it for this iteration")

                ai_response = client.models.generate_content(
                    model=chat_model,
                    contents=messages,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        # response_mime_type="application/json",
                        # response_schema=output_schema,
                        cached_content=existing_cache.name if will_use_cache else None,
                        tool_config=types.ToolConfig(
                            function_calling_config=types.FunctionCallingConfig(
                                mode=types.FunctionCallingConfigMode.ANY,
                            ),
                        ) if not will_use_cache else None,
                        tools=[
                            types.Tool(
                                function_declarations=[{
                                    "name": "process_ai_form_fill",
                                    "description": "Process the AI form fill and return the filled form data.",
                                    "parameters": output_schema,
                                }]
                            )
                        ] if not will_use_cache else None,
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=0 if "pro" not in chat_model else 1024,
                            # include_thoughts=True,
                        ) if "2.5" in chat_model else None
                    ),
                )

                # thinking = ai_response.candidates[0].content.parts

                # for part in thinking:
                #     if part.thought:
                #         logger.info(f"AI thought: {part.text}")

                if ai_response.candidates[0].finish_reason != types.FinishReason.STOP:
                    raise Exception(f"AI response did not finish successfully: {str(ai_response.candidates[0].finish_reason)}")

                ai_response_json = ai_response.candidates[0].content.parts[0].function_call.args

                completion_time = perf_counter() - completion_start_time

                if idx == 0:
                    form.transcript = ai_response_json["__scribe__transcription"]

                this_iteration["completion_id"] = ai_response.response_id
                this_iteration["completion_input_tokens"] = ai_response.usage_metadata.prompt_token_count
                this_iteration["completion_output_tokens"] = ai_response.usage_metadata.candidates_token_count
                this_iteration["completion_cached_tokens"] = ai_response.usage_metadata.cached_content_token_count
                this_iteration["completion_time"] = completion_time
                this_iteration["output"] = ai_response_json

            else:

                messages.append({"role": "user", "content": user_contents})

                ai_response = client.chat.completions.create(
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
