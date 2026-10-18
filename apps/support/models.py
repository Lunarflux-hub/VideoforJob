import uuid
from django.db import models


class SupportTicket(models.Model):
    """
    Обращение пользователя в поддержку (из Telegram-бота или веб-формы).
    """

    class Status(models.TextChoices):
        NEW = "new", "Новое"
        SENT = "sent", "Письмо отправлено"
        FAILED = "failed", "Ошибка отправки"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    message = models.TextField()

    video_job = models.ForeignKey(
        "videos.VideoJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets",
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    error_message = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Обращение в поддержку"
        verbose_name_plural = "Обращения в поддержку"

    def __str__(self):
        return f"Обращение {self.id} от {self.email} ({self.status})"
