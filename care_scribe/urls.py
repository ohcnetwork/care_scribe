from django.shortcuts import HttpResponse
from django.urls import path
from rest_framework.routers import DefaultRouter

from care_scribe.viewsets.scribe_quota import ScribeQuotaViewSet
from care_scribe.viewsets.scribe import ScribeViewset
from care_scribe.viewsets.scribe_file import FileUploadViewSet


def healthy(request):
    return HttpResponse("Hello from scribe")


router = DefaultRouter()
router.register("scribe", ScribeViewset)
router.register("quota", ScribeQuotaViewSet)
router.register("scribe_file", FileUploadViewSet)

urlpatterns = [
    path("health", healthy),
] + router.urls
