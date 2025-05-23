from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from care_scribe.models.scribe import Scribe
from care_scribe.serializers.scribe import ScribeSerializer
from care_scribe.tasks.scribe import process_ai_form_fill

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_framework_filters
from rest_framework.pagination import LimitOffsetPagination
from django.db.models import Q
from rest_framework.filters import BaseFilterBackend

class ScribeSearchFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        search = request.query_params.get("search")
        if not search:
            return queryset

        return queryset.filter(
            Q(requested_in_facility__name__icontains=search) |
            Q(requested_in_encounter__patient__name__icontains=search) |
            Q(requested_in_encounter__external_id__icontains=search) |
            Q(transcript__icontains=search)
        )


class ScribeViewset(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet,
):
    queryset = Scribe.objects.all().order_by("-created_date")
    serializer_class = ScribeSerializer
    lookup_field = "external_id"
    permission_classes = [IsAuthenticated]
    filter_backends = [
        DjangoFilterBackend,
        rest_framework_filters.OrderingFilter,
        ScribeSearchFilter,
    ]
    pagination_class = LimitOffsetPagination
    filterset_fields = [
        "status"
    ]

    def get_queryset(self):
        user = self.request.user
        return self.queryset.filter(requested_by=user).select_related("requested_in_facility", "requested_in_encounter__patient")

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        if instance.status == Scribe.Status.READY:
            process_ai_form_fill.delay(instance.external_id)
