from rest_framework import serializers

class ScribeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Scribe
        fields = [
            "external_id",
            "requested_by",
            "transcript",
            "ai_response",
            "status",
            "form_data",
            "audio_file_ids",
        ]
        read_only_fields = [
            "external_id",
            "requested_by",
            "ai_response",
            "audio_file_ids",
        ]