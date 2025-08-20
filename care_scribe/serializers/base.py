from rest_framework import serializers

from care.facility.models.facility import Facility

class FacilitySerializer(serializers.ModelSerializer):

    id = serializers.UUIDField(source="external_id", read_only=True)

    class Meta:
        model = Facility
        fields = ("id", "name")
