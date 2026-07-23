"""DRF serializers for the review workspace."""

from rest_framework import serializers

from uncorrupt.staging.models import Flag


class FlagSerializer(serializers.ModelSerializer):
    tender_title = serializers.SerializerMethodField()
    buyer_name = serializers.SerializerMethodField()

    class Meta:
        model = Flag
        fields = [
            "id",
            "indicator_id",
            "subject_ref",
            "as_of",
            "explanation",
            "evidence_json",
            "stamp_json",
            "review_status",
            "tender_title",
            "buyer_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_tender_title(self, obj: Flag) -> str | None:
        return obj.tender_ref.title if obj.tender_ref else None

    def get_buyer_name(self, obj: Flag) -> str | None:
        return obj.tender_ref.buyer_name if obj.tender_ref else None


class ReviewUpdateSerializer(serializers.Serializer):
    review_status = serializers.ChoiceField(
        choices=["pending", "confirmed", "rejected", "escalated"],
        required=True,
    )
