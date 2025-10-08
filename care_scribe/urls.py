from rest_framework.routers import DefaultRouter

from care_scribe.viewsets.scribe_quota import ScribeQuotaViewSet
from care_scribe.viewsets.scribe import ScribeViewset
from care_scribe.viewsets.scribe_file import FileUploadViewSet

router = DefaultRouter()
router.register("scribe", ScribeViewset)
router.register("quota", ScribeQuotaViewSet)
router.register("scribe_file", FileUploadViewSet)

urlpatterns = router.urls
