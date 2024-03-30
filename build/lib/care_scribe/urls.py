# -*- coding: utf-8 -*-

from django.urls import path

from django.shortcuts import HttpResponse

def healthy(request):
    return HttpResponse("Hello from scribe")

urlpatterns = [
    path("care_scribe/health", healthy ),
]
