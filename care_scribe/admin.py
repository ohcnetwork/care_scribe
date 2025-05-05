from django.contrib import admin

from care_scribe.models.scribe import Scribe
from care_scribe.models.scribe_file import ScribeFile


@admin.register(Scribe)
class ScribeAdmin(admin.ModelAdmin):
    search_fields = ["requested_by__username", "transcript", "status"]


@admin.register(ScribeFile)
class ScribeFileAdmin(admin.ModelAdmin):
    search_fields = ["file_type", "uploaded_by__username"]