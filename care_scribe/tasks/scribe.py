import base64
import io
import json
import logging
import os
import re
import textwrap
from time import perf_counter
import traceback

from celery import shared_task
from django.utils import timezone
from django.conf import settings
from google import genai
from google.genai import types
from google.oauth2 import service_account
from openai import AzureOpenAI, OpenAI

from care_scribe.models.scribe import Scribe
from care_scribe.models.scribe_file import ScribeFile
from care_scribe.models.scribe_quota import ScribeQuota
from care_scribe.settings import plugin_settings
from care_scribe.utils import hash_string

logger = logging.getLogger(__name__)


BASE_PROMPT = textwrap.dedent(
    """
    You will receive a patient's encounter in the form of text, audio, or image. Your task is to extract all relevant data and populate the specified form fields accordingly. Follow the instructions and rules meticulously to ensure accuracy and compliance.

    Instructions:
    1. Analyze the encounter content thoroughly to identify and extract valid data.
    2. Use readable terms for coded entries (e.g., convert “A32Q Brain Hemorrhage” to “Brain Hemorrhage”).
    3. If the encounter contains non-English content, translate it to English before processing.
    4. If the audio or image contains no relevant data, return an empty string for the transcription field, and do not assume any context or information.
    5. You do not have to fill all fields. Only fill the fields that are relevant to the encounter. Let the rest have a null value.

    Notes Handling:
    - Populate the `note` field only if there is additional context that cannot be captured in the `value`.
    - For example, if the encounter states, “Patient's SPO2 is 20%, but had spiked to 50% an hour ago,” then you should fill `value: 20%` and `note: Spiked to 50% an hour ago`.
    - If the encounter simply states, “Patient's SPO2 is 20%,” set note as null.
    - If additional context does not exist beyond the value, set `note` field to null.

    Current Date and Time: {current_date_time}
"""
)


class ScribeError(Exception):
    pass


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
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        AiClient = genai.Client(
            vertexai=True,
            project=plugin_settings.SCRIBE_GOOGLE_PROJECT_ID,
            location=plugin_settings.SCRIBE_GOOGLE_LOCATION,
            credentials=credentials,
        )

    else:
        raise Exception("Invalid api provider")
    return AiClient


def chat_message(
    provider=plugin_settings.SCRIBE_API_PROVIDER,
    role="user",
    text=None,
    file_object=None,
    file_type="audio",
):
    """Generates a chat message compatible with the given AI provider client."""
    if file_object:
        _, file_data = file_object.files_manager.file_contents(file_object)
        format = file_object.internal_name.split(".")[-1]
        buffer = io.BytesIO(file_data)
        buffer.name = "file." + format

        if provider == "google":
            return types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=f"{file_type} : "),
                    types.Part.from_bytes(
                        data=file_data,
                        mime_type=f"{file_type}/" + format,
                    ),
                ],
            )
        else:
            encoded_string = base64.b64encode(file_data).decode("utf-8")

            return {
                "role": role,
                "content": [
                    {
                        "type": f"{file_type}_url",
                        f"{file_type}_url": {"url": f"data:{file_type}/{format};base64,{encoded_string}"},
                    }
                ],
            }

    else:
        if provider == "google":
            return types.Content(role="user", parts=[types.Part.from_text(text=text)])
        else:
            return {"role": role, "content": [{"type": "text", "text": text}]}


@shared_task
def process_ai_form_fill(external_id):
    current_time = timezone.now()

    form = Scribe.objects.get(external_id=external_id, status=Scribe.Status.READY)

    processing = {
        "created_date": current_time.isoformat(),
    }

    try:
        base_prompt = form.prompt or BASE_PROMPT
        base_prompt = base_prompt.replace("{current_date_time}", current_time.isoformat())

        if not form.audio_file_ids and not form.document_file_ids:
            raise ScribeError("No audio or documents associated with the Scribe. Your upload might have failed.")

        # Verify if the user/facility has not exceeded their quota and has accepted the terms and conditions
        user_quota = None
        facility_quota = None

        is_benchmark = form.meta.get("benchmark", False)

        if not is_benchmark:
            user_quota: ScribeQuota | None = form.requested_by.scribe_quota.filter(
                facility=form.requested_in_facility
            ).first()
            facility_quota: ScribeQuota | None = form.requested_in_facility.scribe_quota.filter(user=None).first()

            if not facility_quota:
                raise ScribeError("Facility does not have a scribe quota.")

            if not user_quota:
                raise ScribeError("User does not have a scribe quota.")

            if user_quota.tnc_hash != plugin_settings.tnc_hash:
                raise ScribeError("User has not accepted the latest terms and conditions.")

            if not facility_quota.allow_ocr and not user_quota.allow_ocr and len(form.document_file_ids) > 0:
                raise ScribeError("OCR is not enabled for this user or facility.")

            # Recalculate used quota. This prevents edge cases where quota
            # was exceeded last month and this is the first request this month
            if (
                facility_quota.last_modified_date.year != current_time.year
                or facility_quota.last_modified_date.month != current_time.month
            ):
                facility_quota.calculate_used()

            if (
                user_quota.last_modified_date.year != current_time.year
                or user_quota.last_modified_date.month != current_time.month
            ):
                user_quota.calculate_used()

            if facility_quota.used >= facility_quota.tokens:
                raise ScribeError("Facility has exceeded its scribe quota.")

            if user_quota.used >= facility_quota.tokens_per_user:
                raise ScribeError("User has exceeded their scribe quota.")

        api_provider = plugin_settings.SCRIBE_API_PROVIDER
        chat_model = plugin_settings.SCRIBE_CHAT_MODEL_NAME
        audio_model = plugin_settings.SCRIBE_AUDIO_MODEL_NAME
        temperature = 0

        if form.chat_model:
            try:
                api_provider, chat_model = form.chat_model.split("/")
                if api_provider == "openai" and plugin_settings.SCRIBE_AZURE_API_KEY:
                    api_provider = "azure"
            except ValueError as e:
                raise ScribeError("Invalid chat model format. Use 'provider/model_name'.") from e

        if form.audio_model:
            audio_model = form.audio_model

        if form.chat_model_temperature is not None:
            temperature = form.chat_model_temperature

        processing["provider"] = api_provider
        processing["chat_model"] = chat_model
        processing["audio_model"] = audio_model if api_provider != "google" else None
        processing["form_data"] = form.form_data

        # Instantiate the AI client once to avoid premature closure and resource management issues,
        # especially with the Google GenAI provider. Reuse this client instance throughout the function.
        client = ai_client(api_provider)

        audio_files = ScribeFile.objects.filter(external_id__in=form.audio_file_ids)
        total_audio_duration = sum(file.meta.get("length", 0) for file in audio_files)

        processed_fields = {}

        def process_fields(fields: list, indent: int = 0):
            for fd in fields:
                if "fields" in fd:
                    process_fields(fd["fields"], indent + 1)
                else:
                    schema = fd.get("schema", {})
                    field_id = fd.get("id", "")
                    processed_fields[field_id] = schema

        for qn in form.form_data:
            process_fields(qn["fields"])

        processed_fields_no_keys = {f"q{i}": v for i, v in enumerate(processed_fields.values())}

        output_schema = {
            "type": "object",
            "properties": {
                **processed_fields_no_keys,
                "__scribe__transcription": {
                    "type": "string",
                    "description": "The transcription of the audio",
                },
            },
            "required": ["__scribe__transcription"],
        }

        initiation_time = perf_counter()

        if (has_images := len(form.document_file_ids) > 0) or total_audio_duration > (3 * 60 * 1000):
            # Asking for the full transcription on longer audio would eat up too many tokens.
            content_type = "image" if has_images else "audio"
            output_schema["properties"]["__scribe__transcription"]["description"] = (
                f"A short summarized transcription of the {content_type} content, focusing on key points and insights in English."
            )

        if api_provider != "google" and len(form.document_file_ids) == 0:
            # As we are transcribing using whisper, we do not need the transcription field in the output schema
            del output_schema["properties"]["__scribe__transcription"]
            output_schema["required"].remove("__scribe__transcription")

        logger.info(f"Scribe[{form.external_id}] status: Processing AI form fill")

        processing["function"] = output_schema
        processing["prompt"] = base_prompt

        messages = []

        messages.append(
            chat_message(
                provider=api_provider,
                role="system",
                text=base_prompt,
            )
        )

        if form.text:
            messages.append(
                chat_message(
                    provider=api_provider,
                    role="user",
                    text=form.text,
                )
            )

        form.status = Scribe.Status.GENERATING_TRANSCRIPT
        form.save()

        transcript = form.transcript or ""
        if not form.transcript:
            logger.info(f"Scribe[{form.external_id}] audio file objects: {audio_files}")

            for audio_file_object in audio_files:
                if api_provider == "google":
                    messages.append(
                        chat_message(
                            provider=api_provider,
                            role="user",
                            file_object=audio_file_object,
                            file_type="audio",
                        )
                    )

                else:
                    logger.info(
                        f"Scribe[{form.external_id}] status: "
                        f"Generating transcript for AI form fill {audio_file_object.external_id}"
                    )
                    try:
                        _, audio_file_data = audio_file_object.files_manager.file_contents(audio_file_object)
                        format = audio_file_object.internal_name.split(".")[-1]
                        buffer = io.BytesIO(audio_file_data)
                        buffer.name = "file." + format
                        transcription = client.audio.translations.create(model=audio_model, file=buffer)
                    except Exception as e:
                        raise ScribeError("Error generating transcript") from e

                    transcript += transcription.text
                    logger.info(f"Scribe[{form.external_id}] transcript: {transcript}")

                    transcription_time = perf_counter() - initiation_time
                    processing["transcription_time"] = transcription_time
                    form.save()

                    # Save the transcript to the form
                    form.transcript = transcript

        document_file_objects = ScribeFile.objects.filter(external_id__in=form.document_file_ids)
        logger.info(f"Scribe[{form.external_id}] document file objects: {document_file_objects}")

        for document_file_object in document_file_objects:
            messages.append(
                chat_message(
                    provider=api_provider,
                    role="user",
                    file_object=document_file_object,
                    file_type="image",
                )
            )

        if transcript != "":
            messages.append(
                chat_message(
                    provider=api_provider,
                    role="user",
                    text=transcript,
                )
            )

        logger.info(f"Scribe[{form.external_id}] status: generating AI form fill")
        form.status = Scribe.Status.GENERATING_AI_RESPONSE
        form.save()

        completion_start_time = perf_counter()

        if api_provider == "google":
            output_schema_hash = hash_string(json.dumps(output_schema, sort_keys=True))

            tools = [
                types.Tool(
                    function_declarations=[
                        {
                            "name": "process_ai_form_fill",
                            "description": "Process the AI form fill and return the filled form data.",
                            "parameters": output_schema,
                        }
                    ]
                )
            ]
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.ANY)
            )

            cached_content = None
            cache_name = f"scribe_{chat_model.replace("/", "_")}_{output_schema_hash}"
            try:
                logger.info(
                    f"Scribe[{form.external_id}] status: fetching cache for {cache_name}"
                )
                cached_content = client.caches.get(name=cache_name)
            except Exception as e:
                logger.error(f"Scribe[{form.external_id}] error fetching cache: {e}")

            if not cached_content:
                logger.info(
                    f"Scribe[{form.external_id}] status: creating new cache for {cache_name}"
                )
                try:
                    cached_content = client.caches.create(
                        model=chat_model,
                        config=types.CreateCachedContentConfig(
                            display_name=cache_name,
                            tools=tools,
                            tool_config=tool_config,
                            ttl="86400s",
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Scribe[{form.external_id}] error creating cache: {e}")
                    match = re.search(r"'message': '([^']+)'", str(e))
                    if match and "constraint-is-too-big" in match.group(1):
                        raise ScribeError(
                            "The form is too large for Scribe. Please try again with a smaller form."
                        ) from e

            will_use_cache = cached_content and cached_content.usage_metadata.total_token_count > 1024
            if will_use_cache:
                processing["cache_name"] = cached_content.name
                logger.info(
                    f"Scribe[{form.external_id}] cached token count: {cached_content.usage_metadata.total_token_count}"
                )
            else:
                logger.info(f"Scribe[{form.external_id}] cache is not large enough, will not use it for this iteration")

            def generate_response(retry=0):
                try:
                    ai_resp = client.models.generate_content(
                        model=chat_model,
                        contents=messages,
                        config=types.GenerateContentConfig(
                            temperature=temperature,
                            cached_content=cached_content.name if will_use_cache else None,
                            tool_config=tool_config if not will_use_cache else None,
                            tools=tools if not will_use_cache else None,
                            thinking_config=types.ThinkingConfig(
                                thinking_budget=0 if "pro" not in chat_model else 1024,
                                include_thoughts=True if "pro" in chat_model else False,
                            )
                            if "2.5" in chat_model
                            else None,
                        ),
                    )
                    processing["ai_response_raw"] = ai_resp.model_dump(mode="json", exclude_defaults=True)
                except Exception as e:
                    raise ScribeError("Error while calling AI model") from e

                # Sometimes gemini creates a malformed function call on it's server, which causes a failure. Nothing we can do about it really.
                # Refer to : https://discuss.ai.google.dev/t/malformed-function-call-finish-reason-happens-too-frequently-with-vertex-ai/93630
                if ai_resp.candidates[0].finish_reason == types.FinishReason.MALFORMED_FUNCTION_CALL:
                    if retry > 0:
                        raise ScribeError(
                            f"AI response was malformed, please retry: {str(ai_resp.candidates[0].finish_message)}"
                        )
                    else:
                        processing["retries"] = retry + 1
                        return generate_response(retry + 1)
                return ai_resp

            ai_response = generate_response()

            if ai_response.candidates[0].finish_reason != types.FinishReason.STOP:
                raise ScribeError(
                    f"AI response did not finish successfully: {str(ai_response.candidates[0].finish_reason)}: "
                    f"{str(ai_response.candidates[0].finish_message)}"
                )

            thinking = next(
                (part for part in ai_response.candidates[0].content.parts if part.thought),
                None,
            )
            processing["thinking"] = thinking.text if thinking else None

            function_call_part = next(
                (part for part in ai_response.candidates[0].content.parts if part.function_call),
                None,
            )
            if not function_call_part:
                raise ScribeError("AI response did not contain a function call")
            ai_response_json = function_call_part.function_call.args

            form.transcript = ai_response_json["__scribe__transcription"]

            processing["completion_id"] = ai_response.response_id
            processing["completion_input_tokens"] = ai_response.usage_metadata.prompt_token_count
            processing["completion_audio_input_tokens"] = sum(
                [
                    detail.token_count
                    for detail in ai_response.usage_metadata.prompt_tokens_details
                    if detail.modality == types.MediaModality.AUDIO and detail.token_count is not None
                ]
            )
            processing["completion_image_input_tokens"] = sum(
                [
                    detail.token_count
                    for detail in ai_response.usage_metadata.prompt_tokens_details
                    if detail.modality == types.MediaModality.IMAGE
                ]
            )
            processing["completion_text_input_tokens"] = sum(
                [
                    detail.token_count
                    for detail in ai_response.usage_metadata.prompt_tokens_details
                    if detail.modality == types.MediaModality.TEXT
                ]
            )
            processing["completion_cached_tokens"] = ai_response.usage_metadata.cached_content_token_count
            processing["completion_cached_audio_tokens"] = (
                sum(
                    [
                        detail.token_count
                        for detail in ai_response.usage_metadata.cache_tokens_details
                        if detail.modality == types.MediaModality.AUDIO
                    ]
                )
                if ai_response.usage_metadata.cache_tokens_details
                else None
            )
            processing["completion_cached_image_tokens"] = (
                sum(
                    [
                        detail.token_count
                        for detail in ai_response.usage_metadata.cache_tokens_details
                        if detail.modality == types.MediaModality.IMAGE
                    ]
                )
                if ai_response.usage_metadata.cache_tokens_details
                else None
            )
            processing["completion_cached_text_tokens"] = (
                sum(
                    [
                        detail.token_count
                        for detail in ai_response.usage_metadata.cache_tokens_details
                        if detail.modality == types.MediaModality.TEXT
                    ]
                )
                if ai_response.usage_metadata.cache_tokens_details
                else None
            )
            processing["completion_output_tokens"] = ai_response.usage_metadata.candidates_token_count
            processing["completion_thinking_tokens"] = ai_response.usage_metadata.thoughts_token_count
            processing["completion_total_tokens"] = ai_response.usage_metadata.total_token_count
            form.chat_input_tokens = ai_response.usage_metadata.prompt_token_count + (
                ai_response.usage_metadata.cached_content_token_count or 0
            )
            form.chat_output_tokens = ai_response.usage_metadata.candidates_token_count

        else:
            try:
                ai_response = client.chat.completions.create(
                    model=chat_model,
                    temperature=temperature,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "process_ai_form_fill",
                            "schema": {
                                **output_schema,
                                "required": [key for key in output_schema["properties"]],
                                "additionalProperties": False,
                            },
                            "strict": True,
                        },
                    },
                )
                processing["ai_response_raw"] = ai_response.model_dump(mode="json", exclude_defaults=True)
            except Exception as e:
                raise ScribeError("Error while calling AI model") from e

            try:
                ai_response_json = json.loads(ai_response.choices[0].message.content)
            except Exception as e:
                logger.error(f"Scribe[{form.external_id}] error parsing response: {ai_response}")
                raise ScribeError("Error parsing AI response") from e

            if not form.transcript and not transcript:
                form.transcript = ai_response_json["__scribe__transcription"]

            processing["completion_id"] = ai_response.id
            processing["completion_input_tokens"] = ai_response.usage.prompt_tokens
            processing["completion_output_tokens"] = ai_response.usage.completion_tokens
            processing["completion_cached_tokens"] = ai_response.usage.prompt_tokens_details.cached_tokens
            form.chat_input_tokens = ai_response.usage.prompt_tokens
            form.chat_output_tokens = ai_response.usage.completion_tokens

        logger.info(f"AI response: {ai_response_json}")
        processing["completion_time"] = perf_counter() - completion_start_time

        # convert the keys back to the original field IDs
        converted_response = {
            k: ai_response_json.get(f"q{i}")
            for i, k in enumerate(processed_fields)
            if ai_response_json.get(f"q{i}") is not None
        }
        form.ai_response = converted_response
        processing["ai_response"] = converted_response
        form.meta["processings"] = [*form.meta.get("processings", []), processing]
        form.status = Scribe.Status.COMPLETED
        form.save()

        logger.info(f"Scribe[{form.external_id}] status: completed AI form processing")

        # Update the user and facility quotas
        if not is_benchmark:
            user_quota.calculate_used()
            facility_quota.calculate_used()

    except Exception as e:
        logger.error(f"Scribe[{form.external_id}] status: error occurred while processing form at line {e.__traceback__.tb_lineno}: {e}")

        if getattr(settings, "SENTRY_DSN", None):
            import sentry_sdk

            sentry_sdk.capture_exception(e)
        else:
            processing["error_trace"] = "".join(traceback.format_exception(None, e, e.__traceback__))

        processing["error"] = str(e)
        form.meta["processings"] = [*form.meta.get("processings", []), processing]
        form.status = Scribe.Status.FAILED
        form.save()

    return
