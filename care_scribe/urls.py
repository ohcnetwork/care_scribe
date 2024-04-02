# -*- coding: utf-8 -*-

from django.urls import path
from django.shortcuts import HttpResponse
from care_scribe.viewsets.scribe_file import FileUploadViewSet
from rest_framework.routers import DefaultRouter

from care_scribe.viewsets.scribe import ScribeViewset


def healthy(request):
    return HttpResponse("Hello from scribe")


router = DefaultRouter()
router.register("scribe", ScribeViewset)
router.register("scribe_file", FileUploadViewSet)

urlpatterns = [
    path("health", healthy),
] + router.urls
