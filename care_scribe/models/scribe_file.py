from django.db import models
from care.facility.models.file_upload import FileUpload


class ScribeFile(FileUpload):
    class FileType(models.IntegerChoices):
        SCRIBE = 1