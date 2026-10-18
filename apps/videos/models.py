import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


def uploaded_video_path(instance, filename: str) -> str:
    job_id = instance.video_job_id or "common"
    return f"uploads/videos/{job_id}/{filename}"


class VideoPreset(models.Model):
    """
    Готовые шаблоны задач редактирования для пользователя.
    """

    class Category(models.TextChoices):
        LIGHTING = "lighting", "Атмосфера и освещение"
        EFFECT = "effect", "Визуальный эффект или объект"
        STYLE = "style", "Стиль сцены"
        CUSTOM = "custom", "Свой вариант"

    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.LIGHTING, db_index=True
    )
    title = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    prompt_template = models.TextField(
        help_text="Шаблон промпта для передачи в модель video-to-video"
    )
    example_input_url = models.CharField(
        max_length=500, blank=True, help_text="Ссылка на исходный ролик примера"
    )
    example_output_url = models.CharField(
        max_length=500, blank=True, help_text="Ссылка на обработанный ролик примера"
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Шаблон редактирования видео"
        verbose_name_plural = "Шаблоны редактирования видео"

    def __str__(self):
        return f"{self.get_category_display()}: {self.title}"


class VideoJob(models.Model):
    """
    Заказ на редактирование видео через Polza.ai (Gemini Omni / video-to-video).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        PROCESSING = "processing", "Обрабатывается"
        DONE = "done", "Готово"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменено"

    class Purpose(models.TextChoices):
        SMM = "smm", "Соцсети / Reels / TikTok"
        BUSINESS = "business", "Реклама товаров / Малый бизнес"
        PERSONAL = "personal", "Личный контент"
        OTHER = "other", "Другое"

    class Rating(models.TextChoices):
        POSITIVE = "positive", "Подходит"
        NEGATIVE = "negative", "Не подходит"

    class CostSource(models.TextChoices):
        PROVIDER_ACTUAL = "provider_actual", "Фактический отчёт Polza API"
        PRICING_ESTIMATE = "pricing_estimate", "Оценка по каталогу Polza"

    class FailureCategory(models.TextChoices):
        NONE = "none", "Нет сбоя"
        TIMEOUT = "timeout", "Таймаут провайдера (GPU Timeout)"
        UPSTREAM_ERROR = "upstream_error", "Ошибка модели Polza/Gemini"
        CONTENT_POLICY = "content_policy", "Отклонено политикой безопасности"
        NETWORK_ERROR = "network_error", "Сетевой сбой / недоступность API"
        INTERNAL_ERROR = "internal_error", "Внутренняя ошибка сервиса"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )
    telegram_chat_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    telegram_message_id = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    preset_category = models.CharField(
        max_length=20,
        choices=VideoPreset.Category.choices,
        default=VideoPreset.Category.CUSTOM,
    )
    preset = models.ForeignKey(
        VideoPreset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )
    user_prompt = models.TextField(help_text="Инструкция, введенная пользователем")
    final_prompt = models.TextField(help_text="Итоговый промпт, отправленный в Polza API")
    video_purpose = models.CharField(
        max_length=20, choices=Purpose.choices, default=Purpose.OTHER, blank=True
    )

    aspect_ratio = models.CharField(max_length=10, default="16:9")
    resolution = models.CharField(max_length=10, default="720p")

    # Интеграция с Polza Media API
    polza_media_id = models.CharField(max_length=100, blank=True, db_index=True)
    polza_task_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Идентификатор задачи провайдера (task_id / operation_id)",
    )
    polza_model = models.CharField(max_length=100, default="google/gemini-omni-1.1-flash")

    # Экономика и себестоимость
    credits_cost = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("1.00"),
        help_text="Списано кредитов по матрице Tiered Billing",
    )
    cost_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("299.00"),
        help_text="Цена операции для пользователя в рублях",
    )
    polza_cost_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Фактическое списание Polza API в RUB",
    )
    cost_source = models.CharField(
        max_length=20,
        choices=CostSource.choices,
        default=CostSource.PRICING_ESTIMATE,
        db_index=True,
        help_text="Источник себестоимости: фактический отчёт API или оценка по матрице",
    )
    cost_verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Подтверждена ли себестоимость сверкой с отчётом/балансом провайдера",
    )
    pricing_version = models.CharField(
        max_length=50,
        default="v1.1-2026-10",
        help_text="Версия тарифной матрицы каталога, использованная в расчёте",
    )
    infra_cost_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("10.00"),
        help_text="Инфраструктурные затраты (S3, сервер)",
    )
    payment_fee_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Комиссия платёжного шлюза (эквайринг)",
    )
    tax_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Расчётный налог согласно выбранному налоговому режиму",
    )
    contribution_profit_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Маржинальная прибыль заказа = Выручка - Переменные расходы (Polza, Инфра, Эквайринг, Налог)",
    )
    contribution_margin_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Маржинальность после переменных расходов (%)",
    )
    gross_profit_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Маржинальная прибыль по операции в RUB (алиас)",
    )
    gross_margin_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Маржинальность % (алиас)",
    )
    cost_api_usd = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("0.0000"),
        help_text="Себестоимость вызова Polza API в USD (при наличии)",
    )
    cost_api_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Алиас для обратной совместимости",
    )

    # Учёт технических сбоев и возвратов
    refunded_credits = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Количество кредитов, возвращенных клиенту при сбое",
    )
    provider_refunded = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Компенсировал ли провайдер списание за сбойную задачу",
    )
    failure_cogs_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Прямой некомпенсированный убыток сервиса при сбое (Polza + Infra)",
    )
    failure_reason_category = models.CharField(
        max_length=30,
        choices=FailureCategory.choices,
        default=FailureCategory.NONE,
        db_index=True,
        help_text="Категория причины сбоя для финансовой и технической аналитики",
    )

    attempt_number = models.PositiveIntegerField(
        default=1,
        help_text="Номер попытки генерации",
    )
    parent_job = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="retries",
        help_text="Родительский заказ, если это повторная попытка",
    )

    duration_sec = models.FloatField(
        null=True, blank=True, help_text="Длительность исходного ролика в секундах"
    )
    execution_time_sec = models.IntegerField(
        null=True, blank=True, help_text="Время генерации в секундах"
    )
    error_message = models.TextField(blank=True)

    # Обратная связь пользователя
    user_rating = models.CharField(
        max_length=10, choices=Rating.choices, null=True, blank=True
    )
    user_feedback = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заказ на видео (VideoJob)"
        verbose_name_plural = "Заказы на видео (VideoJobs)"
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"VideoJob {self.id} [{self.status}] — {self.user}"

    def save(self, *args, **kwargs):
        if not self.polza_task_id and self.polza_media_id:
            self.polza_task_id = self.polza_media_id
        if self.contribution_profit_rub and not self.gross_profit_rub:
            self.gross_profit_rub = self.contribution_profit_rub
        elif self.gross_profit_rub and not self.contribution_profit_rub:
            self.contribution_profit_rub = self.gross_profit_rub
        if self.contribution_margin_percent and not self.gross_margin_percent:
            self.gross_margin_percent = self.contribution_margin_percent
        elif self.gross_margin_percent and not self.contribution_margin_percent:
            self.contribution_margin_percent = self.gross_margin_percent
        super().save(*args, **kwargs)


class UploadedVideo(models.Model):
    """
    Сведения об исходном видеофайле пользователя.
    """

    video_job = models.OneToOneField(
        VideoJob, on_delete=models.CASCADE, related_name="uploaded_video"
    )
    file = models.FileField(upload_to=uploaded_video_path)
    s3_key = models.CharField(max_length=500, blank=True)
    duration_sec = models.FloatField(default=0.0)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    content_type = models.CharField(max_length=100, default="video/mp4")
    polza_file_url = models.CharField(
        max_length=1000,
        blank=True,
        help_text="URL файла в Polza Storage или временный S3 URL",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Исходное видео"
        verbose_name_plural = "Исходные видео"

    def __str__(self):
        return f"UploadedVideo для {self.video_job_id} ({self.duration_sec:.1f}с)"


class VideoResult(models.Model):
    """
    Результат обработки видео нейросетью.
    """

    video_job = models.OneToOneField(
        VideoJob, on_delete=models.CASCADE, related_name="result"
    )
    s3_key = models.CharField(max_length=500, blank=True)
    file_url = models.URLField(
        max_length=1000, help_text="Временный presigned URL или прямой URL"
    )
    telegram_file_id = models.CharField(
        max_length=255, blank=True, help_text="File ID отправленного в Telegram ролика"
    )
    duration_sec = models.FloatField(null=True, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Результат видео"
        verbose_name_plural = "Результаты видео"

    def __str__(self):
        return f"VideoResult для {self.video_job_id}"


class VideoAnalyticsEvent(models.Model):
    """
    Журнал событий воронки для проверки продуктовых гипотез (ТЗ раздел 7).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="video_analytics_events",
    )
    video_job = models.ForeignKey(
        VideoJob,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analytics_events",
    )
    event_name = models.CharField(max_length=64, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Событие аналитики видео"
        verbose_name_plural = "События аналитики видео"
        indexes = [
            models.Index(fields=["event_name", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} | {self.event_name} | {self.user}"
