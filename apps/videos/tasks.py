"""
Фоновые задачи Celery для асинхронной обработки видео через Polza.ai.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.billing.services import refund_generation
from apps.videos.models import UploadedVideo, VideoJob, VideoResult
from apps.videos.notifications import notify_video_job_done, notify_video_job_failed
from apps.videos.services.analytics import track_event
from apps.videos.services.polza_video import (
    PolzaVideoError,
    process_video_edit,
    upload_video_to_polza_storage,
)
from apps.videos.services.cost_calculator import (
    DEFAULT_PRICING_VERSION,
    calculate_job_financials,
    extract_polza_cost_with_source,
)
from apps.videos.services.video_storage import (
    generate_video_presigned_url,
    is_s3_configured,
    upload_video_bytes,
    upload_video_from_url,
)

logger = logging.getLogger(__name__)


def _categorize_failure(error_message: str) -> str:
    """Определяет категорию сбоя для финансового учёта потерь и технической аналитики."""
    msg = (error_message or "").lower()
    if any(k in msg for k in ("timeout", "таймаут", "время ожидания", "превышен")):
        return VideoJob.FailureCategory.TIMEOUT
    if any(k in msg for k in ("safety", "policy", "безопасн", "модерац", "цензур", "заблокирован")):
        return VideoJob.FailureCategory.CONTENT_POLICY
    if any(k in msg for k in ("network", "connection", "соединени", "connect", "dns", "подключени")):
        return VideoJob.FailureCategory.NETWORK_ERROR
    if any(k in msg for k in ("polza", "upstream", "провайдер", "500", "502", "503", "504", "gemini")):
        return VideoJob.FailureCategory.UPSTREAM_ERROR
    return VideoJob.FailureCategory.INTERNAL_ERROR


@shared_task(bind=True, max_retries=0)
def process_video_job_task(self, job_id: str) -> None:
    """
    Асинхронный пайплайн обработки видео:
    1. Перевод в статус PROCESSING.
    2. Подготовка ссылки на исходный ролик (Polza Storage / S3 presigned).
    3. Отправка в Polza.ai и поллинг до завершения.
    4. Сохранение результата в S3 и создание VideoResult.
    5. Перевод в DONE и отправка ролика в Telegram.
    6. При сбое — статус FAILED, фиксация COGS потерь и автоматический возврат кредита.
    """
    try:
        job = VideoJob.objects.select_related("user").get(id=job_id)
    except VideoJob.DoesNotExist:
        logger.error("VideoJob %s не найден", job_id)
        return

    job.status = VideoJob.Status.PROCESSING
    job.save(update_fields=["status", "updated_at"])

    started = timezone.now()
    track_event(
        "generation_started",
        user=job.user,
        video_job=job,
        category=job.preset_category,
        model=job.polza_model,
    )

    uploaded = getattr(job, "uploaded_video", None)
    if not uploaded or not uploaded.file:
        error_msg = "Файл исходного видео не найден"
        _handle_job_failure(job, error_msg)
        return

    try:
        # 1. Получаем доступный для Polza.ai URL исходного видео
        source_url = uploaded.polza_file_url
        if not source_url:
            # Если есть S3 с публичным доступом или presigned URL
            if is_s3_configured():
                if not uploaded.s3_key:
                    with uploaded.file.open("rb") as f:
                        s3_key = f"user_videos/{job.id}/{os.path.basename(uploaded.file.name)}"
                        upload_video_bytes(f.read(), s3_key, uploaded.content_type)
                        uploaded.s3_key = s3_key
                        uploaded.save(update_fields=["s3_key"])
                source_url = generate_video_presigned_url(uploaded.s3_key)

            # Если S3 нет или Polza требует прямой загрузки через свой Storage
            if not source_url or "localhost" in source_url:
                with uploaded.file.open("rb") as f:
                    source_url = upload_video_to_polza_storage(
                        f.read(),
                        filename=os.path.basename(uploaded.file.name),
                        mime_type=uploaded.content_type,
                    )
                uploaded.polza_file_url = source_url
                uploaded.save(update_fields=["polza_file_url"])

        # 2. Вызов Polza.ai Media API
        def on_task_created(media_id: str):
            job.polza_media_id = media_id
            job.polza_task_id = media_id
            job.save(update_fields=["polza_media_id", "polza_task_id", "updated_at"])

        result_payload = process_video_edit(
            prompt=job.final_prompt,
            source_video_url=source_url,
            aspect_ratio=job.aspect_ratio,
            resolution=job.resolution,
            duration=str(int(job.duration_sec or 6)),
            model=job.polza_model,
            on_task_created=on_task_created,
        )

        job.polza_media_id = result_payload.media_id
        job.polza_task_id = result_payload.media_id

        # 3. Сохранение сгенерированного ролика в хранилище
        s3_key, file_url = upload_video_from_url(result_payload.video_url, str(job.id))

        VideoResult.objects.create(
            video_job=job,
            s3_key=s3_key,
            file_url=file_url,
            duration_sec=job.duration_sec,
        )

        # 4. Финансовый расчёт себестоимости и маржи (Проблемы № 1, 2, 3)
        financials = calculate_job_financials(
            job,
            raw_payload=result_payload.raw_response,
            duration_sec=job.duration_sec,
        )
        job.polza_cost_rub = financials["polza_cost_rub"]
        job.cost_source = financials["cost_source"]
        job.pricing_version = financials["pricing_version"]
        job.cost_api_rub = financials["polza_cost_rub"]
        job.infra_cost_rub = financials["infra_cost_rub"]
        job.payment_fee_rub = financials["payment_fee_rub"]
        job.tax_rub = financials["tax_rub"]
        job.contribution_profit_rub = financials["contribution_profit_rub"]
        job.contribution_margin_percent = financials["contribution_margin_percent"]
        job.gross_profit_rub = financials["gross_profit_rub"]
        job.gross_margin_percent = financials["gross_margin_percent"]

        # 5. Успешное завершение
        finished = timezone.now()
        execution_sec = int((finished - started).total_seconds())
        job.status = VideoJob.Status.DONE
        job.execution_time_sec = execution_sec
        job.error_message = ""
        job.save(
            update_fields=[
                "status",
                "execution_time_sec",
                "polza_media_id",
                "polza_task_id",
                "error_message",
                "polza_cost_rub",
                "cost_source",
                "pricing_version",
                "cost_api_rub",
                "infra_cost_rub",
                "payment_fee_rub",
                "tax_rub",
                "contribution_profit_rub",
                "contribution_margin_percent",
                "gross_profit_rub",
                "gross_margin_percent",
                "updated_at",
            ]
        )

        track_event(
            "generation_succeeded",
            user=job.user,
            video_job=job,
            duration_s=execution_sec,
            media_id=job.polza_media_id,
            contribution_profit_rub=float(job.contribution_profit_rub),
            cost_source=job.cost_source,
        )

        # 6. Уведомление пользователя в Telegram
        notify_video_job_done(job)

    except PolzaVideoError as exc:
        logger.warning("PolzaVideoError для задачи %s: %s", job_id, exc)
        if exc.media_id and not job.polza_task_id:
            job.polza_task_id = exc.media_id
            job.polza_media_id = exc.media_id
        _handle_job_failure(job, str(exc), raw_payload=exc.raw_payload)

    except Exception as exc:
        logger.exception("Неожиданная ошибка генерации для задачи %s", job_id)
        _handle_job_failure(job, f"Внутренняя ошибка сервиса: {exc}")


def _handle_job_failure(
    job: VideoJob,
    error_message: str,
    raw_payload: dict | None = None,
) -> None:
    """
    Фиксирует статус FAILED, причину сбоя, рассчитывает прямые переменные потери (COGS)
    от технического сбоя провайдера (Проблема № 4), оформляет автоматический возврат
    кредита пользователю (ТЗ 3.7) и отправляет уведомление.
    """
    job.status = VideoJob.Status.FAILED
    job.error_message = error_message

    failure_cat = _categorize_failure(error_message)
    job.failure_reason_category = failure_cat

    # Автоматический возврат списанных кредитов по матрице Tiered Billing
    refund_amount = getattr(job, "credits_cost", Decimal("1.0")) or Decimal("1.0")
    job.refunded_credits = refund_amount
    refund_generation(
        user=job.user,
        video_job=job,
        amount=refund_amount,
        reason=error_message,
    )

    # Проблема № 4: Полный возврат кредита не означает, что расхода не было.
    # Если задача успела уйти в Polza, провайдер списал деньги / израсходовал GPU.
    infra_cost = getattr(settings, "INFRASTRUCTURE_COST_RUB", Decimal("10.00"))
    has_contacted_provider = bool(
        job.polza_media_id
        or job.polza_task_id
        or failure_cat in (VideoJob.FailureCategory.TIMEOUT, VideoJob.FailureCategory.UPSTREAM_ERROR)
    )

    if has_contacted_provider:
        cost, cost_source = extract_polza_cost_with_source(
            raw_payload, resolution=job.resolution, duration_sec=job.duration_sec
        )
        job.polza_cost_rub = cost
        job.cost_source = cost_source
        job.pricing_version = getattr(settings, "POLZA_PRICING_VERSION", DEFAULT_PRICING_VERSION)
        job.infra_cost_rub = infra_cost
        failure_cogs = cost + infra_cost
        job.failure_cogs_rub = failure_cogs
        job.provider_refunded = False
        # Выручка после 100% возврата = 0, маржинальная прибыль отрицательная (прямой убыток)
        job.contribution_profit_rub = -failure_cogs
        job.gross_profit_rub = -failure_cogs
        job.contribution_margin_percent = Decimal("-100.00") if failure_cogs > 0 else Decimal("0.00")
        job.gross_margin_percent = job.contribution_margin_percent
    else:
        job.polza_cost_rub = Decimal("0.00")
        job.infra_cost_rub = Decimal("0.00")
        job.failure_cogs_rub = Decimal("0.00")
        job.provider_refunded = False
        job.contribution_profit_rub = Decimal("0.00")
        job.gross_profit_rub = Decimal("0.00")
        job.contribution_margin_percent = Decimal("0.00")
        job.gross_margin_percent = Decimal("0.00")

    job.cost_api_rub = job.polza_cost_rub
    job.save(
        update_fields=[
            "status",
            "error_message",
            "failure_reason_category",
            "refunded_credits",
            "polza_cost_rub",
            "cost_source",
            "pricing_version",
            "infra_cost_rub",
            "failure_cogs_rub",
            "provider_refunded",
            "contribution_profit_rub",
            "contribution_margin_percent",
            "gross_profit_rub",
            "gross_margin_percent",
            "cost_api_rub",
            "updated_at",
        ]
    )

    track_event(
        "generation_failed",
        user=job.user,
        video_job=job,
        error=error_message[:300],
        failure_category=failure_cat,
        failure_cogs_rub=float(job.failure_cogs_rub),
    )
    track_event(
        "credit_refunded",
        user=job.user,
        video_job=job,
        amount=float(refund_amount),
    )

    notify_video_job_failed(job, error_message)


@shared_task
def cleanup_expired_videos_task() -> int:
    """
    Периодическая очистка пользовательских видеофайлов старше срока хранения (ТЗ раздел 6).
    """
    retention_hours = getattr(settings, "VIDEO_RETENTION_HOURS", 48)
    cutoff = timezone.now() - timedelta(hours=retention_hours)

    old_jobs = VideoJob.objects.filter(created_at__lt=cutoff).exclude(status=VideoJob.Status.PROCESSING)
    deleted_count = 0

    for job in old_jobs:
        if hasattr(job, "uploaded_video"):
            try:
                if job.uploaded_video.file:
                    job.uploaded_video.file.delete(save=False)
                job.uploaded_video.delete()
                deleted_count += 1
            except Exception:
                logger.exception("Ошибка удаления старого видео для задачи %s", job.id)

    return deleted_count
