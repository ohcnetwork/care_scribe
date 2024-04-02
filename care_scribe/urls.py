# -*- coding: utf-8 -*-

from django.urls import path
from django.shortcuts import HttpResponse
from rest_framework.routers import DefaultRouter

from care_scribe.viewsets.scribe import ScribeViewset


def healthy(request):
    return HttpResponse("Hello from scribe")


router = DefaultRouter()
router.register("scribe", ScribeViewset)

urlpatterns = [
    path("health", healthy),
] + router.urls
