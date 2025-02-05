import json
import logging
import io

from celery import shared_task
from openai import OpenAI, AzureOpenAI

from care_scribe.models.scribe import Scribe
from care_scribe.models.scribe_file import ScribeFile
from care_scribe.settings import plugin_settings

logger = logging.getLogger(__name__)

AiClient = None


def get_openai_client():
    global AiClient
    if AiClient is None:
        if plugin_settings.API_PROVIDER == 'azure':
            AiClient = AzureOpenAI(
                api_key=plugin_settings.TRANSCRIBE_SERVICE_PROVIDER_API_KEY,
                api_version=plugin_settings.AZURE_API_VERSION,
                azure_endpoint=plugin_settings.AZURE_ENDPOINT
            )
        elif plugin_settings.API_PROVIDER == 'openai':
            AiClient = OpenAI(
                api_key=plugin_settings.TRANSCRIBE_SERVICE_PROVIDER_API_KEY
            )
        else:
            raise Exception('Invalid API_PROVIDER in plugin_settings')
    return AiClient


prompt = """
Given a raw transcript by the user, your task is to extract relevant information and structure it according to a predefined schema.
Make sure to produce the response keeping the "current" data in mind. Make sure to infer from the "example" in the schema.
Output the structured data in JSON format.
If a field cannot be filled due to missing information in the transcript, do not include it in the output, skip that JSON key.
For fields that offer options, output the chosen option's ID. Ensure the output strictly adheres to the JSON schema provided.
If the option is not available in the schema, omit the field from the output.
DO NOT Hallucinate or make assumptions about the data. Only include information that is explicitly mentioned in the transcript.
If decimals are requested in the output where the field type is integer, send the default value as per the schema. Do not round off the value.
If "current" data is in the form of an array, make sure to ONLY update the "current" data if specifically asked by the user. Do not replace or remove existing data unless the user has asked you to.

SCHEMA:
{form_schema}
"""


@shared_task
def process_ai_form_fill(external_id):
    ai_form_fills = Scribe.objects.filter(
        external_id=external_id, status=Scribe.Status.READY
    )

    for form in ai_form_fills:
        # Skip forms without audio files
        if not form.audio_file_ids:
            logger.warning(f"AI form fill {form.external_id} has no audio files")
            continue

        logger.info(f"Processing AI form fill {form.external_id}")

        transcript = ""
        try:
            # Update status to GENERATING_TRANSCRIPT
            logger.info(f"Generating transcript for AI form fill {form.external_id}")
            form.status = Scribe.Status.GENERATING_TRANSCRIPT
            form.save()

            if not form.transcript:
                # Use Ayushma to generate transcript from the audio file
                transcript = ""
                audio_file_objects = ScribeFile.objects.filter(
                    external_id__in=form.audio_file_ids
                )
                logger.info(f"Audio file objects: {audio_file_objects}")
                for audio_file_object in audio_file_objects:
                    _, audio_file_data = audio_file_object.file_contents()
                    
                    buffer = io.BytesIO(audio_file_data)
                    buffer.name = "file.mp3"

                    transcription = get_openai_client().audio.translations.create(
                        model=plugin_settings.AUDIO_MODEL_NAME, file=buffer # This can be the model name (OPENAI) or the custom deployment name (AZURE)
                    )
                    transcript += transcription.text
                    logger.info(f"Transcript: {transcript}")

                # Save the transcript to the form
                form.transcript = transcript
            else:
                transcript = form.transcript

            # Update status to GENERATING_AI_RESPONSE
            logger.info(f"Generating AI response for AI form fill {form.external_id}")
            form.status = Scribe.Status.GENERATING_AI_RESPONSE
            form.save()

            messages = [
                    {
                        "role": "system",
                        "content": form.prompt or prompt.replace(
                            "{form_schema}", json.dumps(form.form_data, indent=2)
                        ),
                    },
                    {
                        "role": "user",
                        "content": transcript,
                    },
                ]

            # Process the transcript with Ayushma
            ai_response = get_openai_client().chat.completions.create(
                model=plugin_settings.CHAT_MODEL_NAME, # This can be the model name (OPENAI) or the custom deployment name (AZURE) 
                response_format={"type": "json_object"},
                max_tokens=4096,
                temperature=0,
                messages=messages
            )
            ai_response_json = ai_response.choices[0].message.content
            logger.info(f"AI response: {ai_response_json}")

            # Save AI response to the form
            form.ai_response = ai_response_json
            form.status = Scribe.Status.COMPLETED
            form.save()

        except Exception as e:
            # Log the error or handle it as needed
            form.status = Scribe.Status.FAILED
            form.save()
            logger.error(f"AI form fill processing failed: {e}")
