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

    def get_queryset(self):
        user = self.request.user
        queryset = self.queryset
        if not user.is_superuser:
            queryset = queryset.filter(requested_by=user)
        return queryset

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        if instance.status == Scribe.Status.READY:
            process_ai_form_fill.delay(instance.external_id)