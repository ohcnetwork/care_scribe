import base64
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
You will be provided with details of a patient's encounter in the form of text, audio, or visual content. 
Your task is to analyze this information and extract relevant data to structure it according to a predefined JSON schema.

- Analyze data from text, audio, and images while ensuring all relevant information is extracted thoroughly.
- Adhere strictly to the predefined JSON schema, ensuring the extracted data is accurately structured.
- When using the schema, if a field cannot be populated due to missing data, exclude the field entirely from the output.
- Do not make assumptions or fill in data unless it is explicitly stated in the content.
- When decimals are provided for fields requiring integers, use the default value specified in the schema.
- If data is "entered in error", exclude it from the output.
- If the "current" data is array formatted, update ONLY when the user specifies. Avoid modifying existing data unless instructed.
- Append any observations or understanding derived from the analysis under the key "__scribe__transcription" in the JSON output.

# Steps

1. **Content Analysis**: Carefully analyze the provided content (text, audio, or images) to identify and extract all relevant data.
   
2. **Schema Adherence**: Structure the extracted data according to the provided JSON schema, ensuring compliance with format requirements.

3. **Data Exclusion**: If certain fields cannot be populated due to absence of data, exclude them. Do not guess or assume any information.

4. **Extra Data Handling**: If there is data that does not fit into the schema, put it under the other details field if present. If not, exclude it.

5. **Array Data Handling**: Treat "current" structured data according to user instructions, avoiding unwanted modifications.

6. **Transcription Key Addition**: Conclude analysis by appending a summarized understanding of the content under "__scribe__transcription" in the output.

# Output Format

- Provide all extracted and structured data in a JSON format as per the schema.
- Include a "__scribe__transcription" field summarizing the insights from the content as text.

# Notes

- Ensure no assumptions are made beyond what is explicitly stated in the content inputs.
- Prioritize adherence to the predefined schema for accuracy in representation.
- Ensure every bit of content (text, audio, image) is thoroughly read and understood before being omitted from the JSON response.

# SCHEMA
{form_schema}
"""


@shared_task
def process_ai_form_fill(external_id):
    ai_form_fills = Scribe.objects.filter(
        external_id=external_id, status=Scribe.Status.READY
    )

    for form in ai_form_fills:
        
        logger.info(f"Processing AI form fill {form.external_id}")

        messages = [
            {
                "role": "system",
                "content": [{
                    "type" : "text",
                    "text": form.prompt or prompt.replace(
                        "{form_schema}", json.dumps(form.form_data, indent=2)
                    ),
                }]
            },
        ]

        user_contents = []

        # In case text support is needed
        # if form.text:
        #     user_contents.append(
        #         {
        #             "type": "text",
        #             "text": form.text
        #         }
        #     )

        try:
            logger.info(f"Generating transcript for AI form fill {form.external_id}")
            form.status = Scribe.Status.GENERATING_TRANSCRIPT
            form.save()
            
            transcript = ""
            if not form.transcript:
                audio_file_objects = ScribeFile.objects.filter(
                    external_id__in=form.audio_file_ids
                )

                logger.info(f"Audio file objects: {audio_file_objects}")

                for audio_file_object in audio_file_objects:
                    _, audio_file_data = audio_file_object.file_contents()
                    format = audio_file_object.internal_name.split('.')[-1]                    
                    buffer = io.BytesIO(audio_file_data)
                    buffer.name = "file." + format

                    transcription = get_openai_client().audio.translations.create(
                        model=plugin_settings.AUDIO_MODEL_NAME, file=buffer # This can be the model name (OPENAI) or the custom deployment name (AZURE)
                    )
                    transcript += transcription.text
                    logger.info(f"Transcript: {transcript}")

                # Save the transcript to the form
                form.transcript = transcript
            else:
                transcript = form.transcript

            if transcript != "":
                user_contents.append(
                    {
                        "type": "text",
                        "text": transcript
                    }
                )

            document_file_objects = ScribeFile.objects.filter(
                    external_id__in=form.document_file_ids
            )
            logger.info(f"Document file objects: {document_file_objects}")
            for document_file_object in document_file_objects:
                _, document_file_data = document_file_object.file_contents()
                format = document_file_object.internal_name.split('.')[-1]
                encoded_string = base64.b64encode(document_file_data).decode('utf-8')

                user_contents.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{format};base64,{encoded_string}"
                        }
                    }
                )


            logger.info(f"Generating AI form fill {form.external_id}")
            form.status = Scribe.Status.GENERATING_AI_RESPONSE
            form.save()

            messages.append({
                "role": "user",
                "content":user_contents
            })

            print(messages)

            # Process the transcript with Ayushma
            ai_response = get_openai_client().chat.completions.create(
                model=plugin_settings.CHAT_MODEL_NAME,
                response_format={"type": "json_object"},
                max_tokens=10000,
                temperature=0,
                messages=messages
            )
            response = ai_response.choices[0].message
            
            if response.content == None:
                form.status = Scribe.Status.REFUSED
                form.save()
                continue

            ai_response_json = response.content
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
