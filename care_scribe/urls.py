# -*- coding: utf-8 -*-

from django.urls import path
from django.shortcuts import HttpResponse
from rest_framework.routers import DefaultRouter

from care_scribe.viewsets.scribe import ScribeViewset
from care_scribe.viewsets.scribe_file import FileUploadViewSet


def healthy(request):
    return HttpResponse("Hello from scribe")


router = DefaultRouter()
router.register("scribe", ScribeViewset)
router.register("scribe_file", FileUploadViewSet)

urlpatterns = [
    path("health", healthy),
] + router.urls
