from rest_framework import serializers
from care_scribe.models.scribe_file import ScribeFile


class ScribeFileUploadCreateSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="external_id", read_only=True)
    file_type = serializers.ChoiceField(choices=ScribeFile.FileType.choices)
    file_category = serializers.ChoiceField(choices=ScribeFile.FileCategoryChoices, required=False)

    signed_url = serializers.CharField(read_only=True)
    associating_id = serializers.CharField(write_only=True)
    internal_name = serializers.CharField(read_only=True)
    original_name = serializers.CharField(write_only=True)
    mime_type = serializers.CharField(write_only=True)

    def create(self, validated_data):
        # TODO: override check_permissions here
        pass

    class Meta:
        model = ScribeFile
        fields = (
            "id",
            "file_type",
            "file_category",
            "name",
            "associating_id",
            "signed_url",
            "internal_name",
            "original_name",
            "mime_type",
        )
        write_only_fields = ("associating_id",)
