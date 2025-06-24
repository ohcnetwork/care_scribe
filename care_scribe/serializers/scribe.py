from rest_framework import serializers
from care.facility.models.facility import Facility
from care.emr.models.encounter import Encounter
from care.emr.models.patient import Patient
from care.users.models import User
from care_scribe.models.scribe import Scribe
from care.users.api.serializers.user import FacilityBareMinimumSerializer
from care_scribe.models.scribe_file import ScribeFile
from care_scribe.serializers.scribe_file import ScribeFileUploadUpdateSerializer


class ScribePatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = [
            "external_id",
            "name",
        ]


class ScribeEncounterSerializer(serializers.ModelSerializer):
    patient = ScribePatientSerializer(read_only=True)

    class Meta:
        model = Encounter
        fields = [
            "external_id",
            "patient",
        ]


class ScribeUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "username", "last_name", "read_profile_picture_url"]


class ScribeSerializer(serializers.ModelSerializer):

    requested_in_facility_id = serializers.CharField(write_only=True, required=True)
    requested_in_encounter_id = serializers.CharField(write_only=True, required=False)
    requested_in_facility = FacilityBareMinimumSerializer(read_only=True)
    requested_in_encounter = ScribeEncounterSerializer(read_only=True)
    requested_by = ScribeUserSerializer(read_only=True)

    audio = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()

    class Meta:
        model = Scribe
        fields = [
            "external_id",
            "requested_by",
            "requested_in_facility",
            "requested_in_facility_id",
            "requested_in_encounter",
            "requested_in_encounter_id",
            "transcript",
            "ai_response",
            "status",
            "form_data",
            "audio",
            "prompt",
            "text",
            "meta",
            "created_date",
            "modified_date",
            "documents",
        ]
        read_only_fields = [
            "external_id",
            "requested_by",
            "requested_in_facility",
            "ai_response",
            "audio",
            "created_date",
            "modified_date",
            "documents",
        ]

    def save(self, **kwargs):
        facility_id = self.validated_data.get("requested_in_facility_id", None)
        encounter_id = self.validated_data.get("requested_in_encounter_id", None)
        self.validated_data["requested_in_facility"] = Facility.objects.filter(external_id=facility_id).first()
        self.validated_data["requested_in_encounter"] = Encounter.objects.filter(external_id=encounter_id).first()

        # TODO : Check if the user has access to the facility. This is not a very huge concern rn, but still should be done

        if not self.validated_data["requested_in_facility"]:
            raise serializers.ValidationError({"requested_in_facility": "Invalid facility ID"})

        if not self.validated_data["requested_in_encounter"]:
            raise serializers.ValidationError({"requested_in_encounter": "Invalid encounter ID"})

        self.validated_data.pop("requested_in_facility_id", None)
        self.validated_data.pop("requested_in_encounter_id", None)

        return super().save(**kwargs)

    def get_audio(self, obj):
        audio_file_ids = obj.audio_file_ids
        if not audio_file_ids:
            return []

        audio_files = ScribeFile.objects.filter(external_id__in=audio_file_ids)

        return ScribeFileUploadUpdateSerializer(audio_files, many=True).data if audio_files else []

    def get_documents(self, obj):
        document_file_ids = obj.document_file_ids
        if not document_file_ids:
            return []

        document_files = ScribeFile.objects.filter(external_id__in=document_file_ids)

        return ScribeFileUploadUpdateSerializer(document_files, many=True).data if document_files else []
