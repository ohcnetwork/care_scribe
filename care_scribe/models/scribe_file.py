from django.db import models
from care.facility.models.file_upload import BaseFileUpload


class ScribeFile(BaseFileUpload):
    class FileType(models.IntegerChoices):
        OTHER = 0
        SCRIBE = 1

    test = models.CharField(max_length=100, null=True, blank=True)
    file_type = models.IntegerField(choices=FileType.choices, default=FileType.SCRIBE)
