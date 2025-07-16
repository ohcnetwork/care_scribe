from rest_framework import serializers

from care.facility.models.facility import Facility
from care.users.api.serializers.user import FacilityBareMinimumSerializer
from care.users.models import User
from care_scribe.care_scribe.models.scribe_quota import ScribeQuota
from care_scribe.care_scribe.serializers.scribe import ScribeUserSerializer

class ScribeQuotaSerializer(serializers.ModelSerializer):
    facility = FacilityBareMinimumSerializer(read_only=True)
    facility_external_id = serializers.CharField(write_only=True, required=False)
    user_external_id = serializers.CharField(write_only=True, required=False)
    user = ScribeUserSerializer(read_only=True)
    created_by = ScribeUserSerializer(read_only=True)

    class Meta:
        model = ScribeQuota
        fields = (
            "external_id",
            "user",
            "facility",
            "tokens",
            "used",
            "allow_ocr",
            "created_by",
            "created_date",
            "modified_date",
            "facility_external_id",
            "user_external_id",
        )

        read_only_fields = ("external_id", "used", "created_by", "created_date", "modified_date")

    def validate(self, attrs):
        facility_external_id = attrs.pop("facility_external_id", None)
        user_external_id = attrs.pop("user_external_id", None)

        if not self.instance and not (facility_external_id or user_external_id):
            raise serializers.ValidationError("Either 'facility_external_id' or 'user_external_id' must be provided.")

        # check if a quota already exists for the user or facility
        if user_external_id:
            if ScribeQuota.objects.filter(user__external_id=user_external_id).exists():
                raise serializers.ValidationError(f"A ScribeQuota already exists for this user.")
            user = User.objects.filter(external_id=user_external_id).first()
            if not user:
                raise serializers.ValidationError(f"User does not exist.")
            attrs["user"] = user

        if facility_external_id:
            if ScribeQuota.objects.filter(facility__external_id=facility_external_id).exists():
                raise serializers.ValidationError(f"A ScribeQuota already exists for this facility.")
            facility = Facility.objects.filter(external_id=facility_external_id).first()
            if not facility:
                raise serializers.ValidationError(f"Facility does not exist.")
            attrs["facility"] = facility

        return super().validate(attrs)
