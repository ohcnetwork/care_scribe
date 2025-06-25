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
from django_filters import rest_framework as filters
from django.db import transaction


class ScribeFilter(filters.FilterSet):
    status = filters.ChoiceFilter(
        field_name="status",
        choices=Scribe.Status.choices,
        label="Status",
    )
    facility = filters.CharFilter(
        field_name="requested_in_facility__name",
        lookup_expr="icontains",
        label="Facility Name",
    )
    patient = filters.CharFilter(
        field_name="requested_in_encounter__patient__name",
        lookup_expr="icontains",
        label="Patient Name",
    )
    encounter_id = filters.CharFilter(
        field_name="requested_in_encounter__external_id",
        lookup_expr="exact",
        label="Encounter ID",
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
    ]
    filterset_class = ScribeFilter
    pagination_class = LimitOffsetPagination

    def get_queryset(self):
        user = self.request.user
        return self.queryset.filter(requested_by=user).select_related("requested_in_facility", "requested_in_encounter__patient")

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        if instance.status == Scribe.Status.READY:
            transaction.on_commit(lambda: process_ai_form_fill.delay(instance.external_id))
