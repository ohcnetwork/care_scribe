import enum
from care.facility.api.serializers.file_upload import FileUploadCreateSerializer
from care_scribe.models.scribe_file import ScribeFileUpload
from config.serializers import ChoiceField


class ScribeFileUploadCreateSerializer(FileUploadCreateSerializer):
    file_type = ChoiceField(choices=ScribeFileUpload.FileType.choices)

    def create(self, validated_data):
        # TODO: override check_permissions here
        pass
