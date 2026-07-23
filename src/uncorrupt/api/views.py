"""DRF views for the flag review workspace."""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from uncorrupt.api.serializers import FlagSerializer, ReviewUpdateSerializer
from uncorrupt.staging.models import Flag


class FlagViewSet(viewsets.ReadOnlyModelViewSet):
    """Browse and review anomaly flags."""

    queryset = Flag.objects.select_related("tender_ref").all()
    serializer_class = FlagSerializer
    filterset_fields = ["indicator_id", "review_status", "as_of"]
    ordering_fields = ["as_of", "created_at", "indicator_id"]

    @action(detail=True, methods=["post"])
    def review(self, request: Request, pk: int) -> Response:
        """Analyst confirms/rejects/escalates a flag."""
        flag = self.get_object()
        serializer = ReviewUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        flag.review_status = serializer.validated_data["review_status"]
        flag.save()
        return Response(FlagSerializer(flag).data)
