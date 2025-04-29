import base64
import json
import logging
import io

from celery import shared_task
from openai import OpenAI, AzureOpenAI, api_key
from care_scribe.models.scribe import Scribe
from care_scribe.models.scribe_file import ScribeFile
from care_scribe.settings import plugin_settings
from google.genai import types
from google import genai
from google.auth import default
import google.auth.transport.requests
from care.users.models import UserFlag
from care.facility.models.facility_flag import FacilityFlag

logger = logging.getLogger(__name__)

AiClient = None


def get_openai_client():
    global AiClient
    if AiClient is None:
        if plugin_settings.SCRIBE_API_PROVIDER == 'azure':
            AiClient = AzureOpenAI(
                api_key=plugin_settings.SCRIBE_PROVIDER_API_KEY,
                api_version=plugin_settings.SCRIBE_AZURE_API_VERSION,
                azure_endpoint=plugin_settings.SCRIBE_AZURE_ENDPOINT
            )
        elif plugin_settings.SCRIBE_API_PROVIDER == 'openai':
            AiClient = OpenAI(
                api_key=plugin_settings.SCRIBE_PROVIDER_API_KEY,
            )

        elif plugin_settings.SCRIBE_API_PROVIDER == 'google':
            AiClient = genai.Client(
                vertexai=True,
                project=plugin_settings.SCRIBE_GOOGLE_PROJECT_ID,
                location=plugin_settings.SCRIBE_GOOGLE_LOCATION,
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
- You have to make sure that all data you read from the content is included as per the schema. 
  If data does not fit into the schema, put it under the other details field if present. If not, exclude it.
- If data contains medical terms with their codes, example : A32Q Brain Hemorrhage, you can safely ignore the code and only keep the term.
- Append any observations or understanding derived from the analysis under the key "__scribe__transcription" in the JSON output.
- If the schema suggests a field to not be optional (i.e. the field key does not end with a "?"), ensure that the field is populated with the correct data type as per the schema.
  If you are unable to figure out the correct data type, use the first value from the schema as a default.

# Steps

1. **Content Analysis**: Carefully analyze the provided content (text, audio, or images) to identify and extract all relevant data.
   
2. **Schema Adherence**: Structure the extracted data according to the provided JSON schema, ensuring compliance with format requirements.

3. **Data Exclusion**: If certain fields cannot be populated due to absence of data, exclude them. Do not guess or assume any information.

4. **Extra Data Handling**: If there is data that does not fit into the schema, put it under the other details field if present. If not, exclude it.

5. **Array Data Handling**: Treat "current" structured data according to user instructions, avoiding unwanted modifications.

6. **Transcription Key Addition**: Conclude analysis by appending a summarized understanding of the content under "__scribe__transcription" in the output.

# Existing data

{form_schema}

# Output Format

- Provide all extracted and structured data in a JSON format as per the schema.
- Include a "__scribe__transcription" field summarizing the insights from the content as text.
- Make sure to translate everything to English if the content is in a different language.

```
{
  "<id>": "value",
  ...
  "__scribe__transcription": "Your summarized understanding of the content goes here.",
}
```

example: 

````
{
    "0" : 34,
    "1" : "CRITICAL".
    "__scribe__transcription": "The patient is in critical condition and requires immediate attention. Their SPO2 is 34",
}
```

# Notes

- Ensure no assumptions are made beyond what is explicitly stated in the content inputs.
- Prioritize adherence to the predefined schema for accuracy in representation.
- Ensure every bit of content (text, audio, image) is thoroughly read and understood before being omitted from the JSON response.
"""


@shared_task
def process_ai_form_fill(external_id):
    ai_form_fills = Scribe.objects.filter(
        external_id=external_id, status=Scribe.Status.READY
    )

    for form in ai_form_fills:
        
        logger.info(f"Processing AI form fill {form.external_id}")

        if plugin_settings.SCRIBE_API_PROVIDER == 'google':

            messages = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=
                            form.prompt or prompt.replace(
                                "{form_schema}", json.dumps(form.form_data, indent=2)
                            )
                        )
                    ]
                )
            ]

        else:
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

                    if plugin_settings.SCRIBE_API_PROVIDER == 'google':
                        messages.append(types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(text="Audio File:"),
                                types.Part.from_bytes(
                                    data=audio_file_data,
                                    mime_type="audio/" + format,
                                )
                            ]
                        ))
                        
                    else:

                        transcription = get_openai_client().audio.translations.create(
                            model=plugin_settings.SCRIBE_AUDIO_MODEL_NAME, file=buffer # This can be the model name (OPENAI) or the custom deployment name (AZURE)
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
            if document_file_objects.count() > 0:
                
                # Check if Facility or User has OCR ENABLED
                facility_has_ocr_flag = FacilityFlag.check_facility_has_flag(form.requested_in_facility.id, "SCRIBE_OCR_ENABLED")
                user_has_ocr_flag = UserFlag.check_user_has_flag(form.requested_by.id, "SCRIBE_OCR_ENABLED")

                if not (user_has_ocr_flag or facility_has_ocr_flag):
                    raise Exception("OCR is not enabled for this user or facility")

            for document_file_object in document_file_objects:
                _, document_file_data = document_file_object.file_contents()
                format = document_file_object.internal_name.split('.')[-1]
                encoded_string = base64.b64encode(document_file_data).decode('utf-8')

                if plugin_settings.SCRIBE_API_PROVIDER == 'google':
                    messages.append(types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(
                                data=document_file_data,
                                mime_type="image/" + format,
                            )
                        ]
                    ))
                else:
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

            if plugin_settings.SCRIBE_API_PROVIDER == 'google':

                print(messages)
                
                ai_response = get_openai_client().models.generate_content(
                    model=plugin_settings.SCRIBE_CHAT_MODEL_NAME,
                    contents=messages,
                    config=types.GenerateContentConfig(
                        temperature=0,
                        max_output_tokens=8192,
                        response_mime_type="application/json",
                    )
                )

                ai_response_json = ai_response.text

                form.transcript = json.loads(ai_response_json).get("__scribe__transcription", "")
                form.save()
            
            else:

                messages.append({
                    "role": "user",
                    "content":user_contents
                })

                print(messages)

                # Process the transcript with Ayushma
                ai_response = get_openai_client().chat.completions.create(
                    model=plugin_settings.SCRIBE_CHAT_MODEL_NAME,
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
            logger.error(f"AI form fill processing failed at line {e.__traceback__.tb_lineno}: {e}")
