from django.utils.html import strip_tags
from rest_framework import serializers

from .models import SupportTicket


class SupportTicketCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ["email", "message", "video_job"]
        extra_kwargs = {"video_job": {"required": False, "allow_null": True}}

    def validate_message(self, value):
        cleaned = strip_tags(value).strip()
        if len(cleaned) < 5:
            raise serializers.ValidationError(
                "Сообщение должно содержать минимум 5 символов"
            )
        return cleaned

    def validate_email(self, value):
        return value.strip().lower()


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ["id", "email", "message", "video_job", "status", "created_at"]
        read_only_fields = fields
