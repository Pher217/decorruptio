"""API URL routes."""

from rest_framework.routers import DefaultRouter

from uncorrupt.api.views import FlagViewSet

router = DefaultRouter()
router.register(r"flags", FlagViewSet, basename="flag")

urlpatterns = router.urls
