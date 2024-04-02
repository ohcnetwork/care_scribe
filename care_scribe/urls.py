# -*- coding: utf-8 -*-

from django.urls import path

from django.shortcuts import HttpResponse
from care_scribe.viewsets.scribe import ScribeViewset


def healthy(request):
    return HttpResponse("Hello from scribe")


urlpatterns = [
    path("care_scribe/health", healthy),
    path("care_scribe/scribe", ScribeViewset),
]
