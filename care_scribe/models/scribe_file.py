from django.db import models
from care.facility.models.file_upload import BaseFileUpload
from django.contrib.auth import get_user_model

User = get_user_model()

class ScribeFile(BaseFileUpload):
    class FileType(models.IntegerChoices):
        OTHER = 0
        SCRIBE = 1

    file_type = models.IntegerField(choices=FileType.choices, default=FileType.SCRIBE)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )