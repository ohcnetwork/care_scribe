from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
    DestroyModelMixin
)
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from care_scribe.care_scribe.models.scribe_question import ScribeQuestionnaireInstruction
from care_scribe.care_scribe.serializers.scribe_question import ScribeQuestionnaireInstructionsSerializer

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rest_framework_filters
from rest_framework.pagination import LimitOffsetPagination
from django_filters import rest_framework as filters

class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    pass

class ScribeQuestionnaireInstructionsFilter(filters.FilterSet):
    questionnaire_ids = CharInFilter(method="filter_questionnaire_ids")
    questionnaire_title = filters.CharFilter(
        field_name="questionnaire__title",
        lookup_expr="icontains",
        label="Questionnaire Title",
    )

    def filter_questionnaire_ids(self, qs, name, value):
        # value is a list parsed from CSV or repeated params
        return qs.filter(questionnaire__external_id__in=value)

    class Meta:
        model = ScribeQuestionnaireInstruction
        fields = ["questionnaire_ids"]

class ScribeQuestionnaireInstructionsViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet,
    DestroyModelMixin
):
    queryset = ScribeQuestionnaireInstruction.objects.all().prefetch_related("questionnaire")
    serializer_class = ScribeQuestionnaireInstructionsSerializer
    lookup_field = "external_id"
    permission_classes = [IsAdminUser]
    filter_backends = [
        DjangoFilterBackend,
        rest_framework_filters.OrderingFilter,
    ]
    filterset_class = ScribeQuestionnaireInstructionsFilter
    pagination_class = LimitOffsetPagination
    permission_action_classes = {
        "list" : [IsAuthenticated()],
        "retrieve" : [IsAuthenticated()],
    }
