from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
    DestroyModelMixin
)
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from care_scribe.models.scribe_quota import ScribeQuota
from care_scribe.serializers.scribe_quota import ScribeQuotaSerializer

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_framework_filters
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters import rest_framework as filters

class ScribeQuotaFilter(filters.FilterSet):

    facility = filters.CharFilter(
        field_name="facility__name",
        lookup_expr="icontains",
        label="Facility Name",
    )
    user = filters.CharFilter(
        field_name="user__username",
        lookup_expr="icontains",
        label="Username",
    )


class ScribeQuotaViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet,
    DestroyModelMixin
):
    queryset = ScribeQuota.objects.all()
    serializer_class = ScribeQuotaSerializer
    lookup_field = "external_id"
    permission_classes = [IsAdminUser]
    permission_action_classes = {
        "my-quota": [IsAuthenticated(),]
    }
    filter_backends = [
        DjangoFilterBackend,
        rest_framework_filters.OrderingFilter,
    ]
    filterset_class = ScribeQuotaFilter
    pagination_class = LimitOffsetPagination

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=["get"], url_path="my-quota")
    def my_quota(self, request):
        user = request.user
        facility_id = request.query_params.get("facility_id", None)
        quota = ScribeQuota.objects.filter(user=user).first()

        if not quota and facility_id:
            quota = ScribeQuota.objects.filter(facility__external_id=facility_id).first()

        if not quota:
            return Response({"detail": "No quota found for the user or facility."}, status=404)

        serializer = self.get_serializer(quota)
        return Response(serializer.data)
