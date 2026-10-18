from django.contrib import admin
from django.utils.html import format_html

from apps.videos.models import (
    UploadedVideo,
    VideoAnalyticsEvent,
    VideoJob,
    VideoPreset,
    VideoResult,
)


@admin.register(VideoPreset)
class VideoPresetAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "slug", "is_active", "sort_order")
    list_filter = ("category", "is_active")
    search_fields = ("title", "slug", "prompt_template")
    prepopulated_fields = {"slug": ("title",)}


class UploadedVideoInline(admin.StackedInline):
    model = UploadedVideo
    extra = 0
    readonly_fields = ("created_at", "size_bytes", "duration_sec", "width", "height")


class VideoResultInline(admin.StackedInline):
    model = VideoResult
    extra = 0
    readonly_fields = ("created_at", "file_url_link")

    def file_url_link(self, obj):
        if obj.file_url:
            return format_html('<a href="{}" target="_blank">Открыть результат</a>', obj.file_url)
        return "—"
    file_url_link.short_description = "Ссылка на видео"


@admin.register(VideoJob)
class VideoJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "status",
        "resolution",
        "duration_sec",
        "cost_rub",
        "polza_cost_rub",
        "cost_source_badge",
        "cost_verified_icon",
        "contribution_profit_display",
        "margin_display",
        "failure_info",
        "created_at",
    )
    list_filter = (
        "status",
        "cost_source",
        "cost_verified",
        "failure_reason_category",
        "provider_refunded",
        "resolution",
        "preset_category",
        "user_rating",
        "created_at",
    )
    search_fields = ("id", "user__username", "user_prompt", "polza_media_id", "polza_task_id")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "execution_time_sec",
        "polza_media_id",
        "polza_task_id",
        "pricing_version",
        "cost_source",
        "cost_verified",
        "polza_cost_rub",
        "infra_cost_rub",
        "payment_fee_rub",
        "tax_rub",
        "contribution_profit_rub",
        "contribution_margin_percent",
        "gross_profit_rub",
        "gross_margin_percent",
        "failure_cogs_rub",
        "failure_reason_category",
        "refunded_credits",
        "provider_refunded",
    )

    @admin.display(description="Источник себестоимости")
    def cost_source_badge(self, obj):
        if obj.cost_source == VideoJob.CostSource.PROVIDER_ACTUAL:
            return format_html('<span style="color:#007700; font-weight:bold;">● Polza API Fact</span>')
        return format_html('<span style="color:#885500;">~ Оценка каталога</span>')

    @admin.display(description="Сверка", boolean=True)
    def cost_verified_icon(self, obj):
        return obj.cost_verified

    @admin.display(description="Марж. прибыль (₽)")
    def contribution_profit_display(self, obj):
        val = obj.contribution_profit_rub
        if val > 0:
            return format_html('<span style="color:#008800; font-weight:bold;">+{} ₽</span>', val)
        elif val < 0:
            return format_html('<span style="color:#cc0000; font-weight:bold;">{} ₽ (убыток)</span>', val)
        return "0.00 ₽"

    @admin.display(description="Маржа %")
    def margin_display(self, obj):
        pct = obj.contribution_margin_percent
        if pct >= 50:
            color = "#008800"
        elif pct >= 25:
            color = "#cc8800"
        elif pct > 0:
            color = "#cc5500"
        else:
            color = "#cc0000"
        return format_html('<b style="color:{};">{}%</b>', color, pct)

    @admin.display(description="Сбои и потери")
    def failure_info(self, obj):
        if obj.status == VideoJob.Status.FAILED:
            cat = obj.get_failure_reason_category_display()
            cogs = obj.failure_cogs_rub
            return format_html('<span style="color:#cc0000;">{}: -{} ₽</span>', cat, cogs)
        return "—"

    inlines = [UploadedVideoInline, VideoResultInline]

    actions = [
        "action_mark_cost_verified",
        "action_mark_provider_refunded",
        "action_retry_job",
        "action_manual_refund",
    ]

    @admin.action(description="Подтвердить себестоимость сверкой (cost_verified = True)")
    def action_mark_cost_verified(self, request, queryset):
        count = queryset.update(cost_verified=True)
        self.message_user(request, f"Успешно подтверждена себестоимость для {count} задач.")

    @admin.action(description="Отметить компенсацию сбоя провайдером (provider_refunded = True)")
    def action_mark_provider_refunded(self, request, queryset):
        count = queryset.update(provider_refunded=True)
        self.message_user(request, f"Помечено как компенсированные провайдером: {count} задач.")

    @admin.action(description="Повторно отправить задачу в Celery")
    def action_retry_job(self, request, queryset):
        from apps.videos.tasks import process_video_job_task
        for job in queryset:
            process_video_job_task.delay(str(job.id))
        self.message_user(request, f"Запущено повторно {queryset.count()} задач.")

    @admin.action(description="Оформить ручной возврат 1 кредита")
    def action_manual_refund(self, request, queryset):
        from apps.billing.services import refund_generation
        for job in queryset:
            refund_generation(job.user, video_job=job, amount=1, reason="Ручной возврат из админки")
        self.message_user(request, f"Возвращен 1 кредит для {queryset.count()} задач.")


@admin.register(UploadedVideo)
class UploadedVideoAdmin(admin.ModelAdmin):
    list_display = ("video_job", "duration_sec", "size_bytes", "content_type", "created_at")
    search_fields = ("video_job__id",)


@admin.register(VideoResult)
class VideoResultAdmin(admin.ModelAdmin):
    list_display = ("video_job", "file_url", "telegram_file_id", "created_at")
    search_fields = ("video_job__id", "telegram_file_id")


@admin.register(VideoAnalyticsEvent)
class VideoAnalyticsEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event_name", "user", "video_job", "metadata")
    list_filter = ("event_name", "created_at")
    search_fields = ("event_name", "user__username")
    readonly_fields = ("created_at",)
