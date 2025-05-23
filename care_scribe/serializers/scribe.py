from rest_framework import serializers
from care.facility.models.facility import Facility
from care.emr.models.encounter import Encounter
from care_scribe.models.scribe import Scribe


class ScribeSerializer(serializers.ModelSerializer):

    requested_in_facility_id = serializers.CharField(write_only=True, required=True)
    requested_in_encounter_id = serializers.CharField(write_only=True, required=False)

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
            "audio_file_ids",
            "prompt",
            "text",
            "meta",
            "created_date",
            "modified_date",
            "document_file_ids"
        ]
        read_only_fields = [
            "external_id",
            "requested_by",
            "requested_in_facility",
            "ai_response",
            "audio_file_ids",
            "created_date",
            "modified_date",
            "document_file_ids"
        ]

    def save(self, **kwargs):
        facility_id = self.validated_data.get("requested_in_facility_id", None)
        encounter_id = self.validated_data.get("requested_in_encounter_id", None)
        self.validated_data["requested_in_facility"] = Facility.objects.filter(external_id=facility_id).first()
        self.validated_data["requested_in_encounter"] = Encounter.objects.filter(external_id=encounter_id).first()

        # TODO : Check if the user has access to the facility. This is not a very huge concern rn, but still should be done

        if not self.validated_data["requested_in_facility"]:
            raise serializers.ValidationError(
                {"requested_in_facility": "Invalid facility ID"}
            )
            
        if not self.validated_data["requested_in_encounter"]:
            raise serializers.ValidationError(
                {"requested_in_encounter": "Invalid encounter ID"}
            )
        
        self.validated_data.pop("requested_in_facility_id", None)
        self.validated_data.pop("requested_in_encounter_id", None)

        return super().save(**kwargs)
