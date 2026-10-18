"""
Хранилище видеофайлов и генерация presigned-ссылок для VideoLab AI.
Использует Yandex Cloud Object Storage (S3-совместимый) в приватном режиме,
с безопасным фолбэком на локальный MEDIA_ROOT для локальной разработки.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import boto3
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

PRESIGNED_URL_EXPIRES_IN = 60 * 60 * 24 * 3  # 3 дня


def is_s3_configured() -> bool:
    return bool(
        getattr(settings, "YC_S3_ACCESS_KEY_ID", "")
        and getattr(settings, "YC_S3_SECRET_ACCESS_KEY", "")
        and getattr(settings, "YC_S3_BUCKET_NAME", "")
    )


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.YC_S3_ENDPOINT_URL,
        aws_access_key_id=settings.YC_S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.YC_S3_SECRET_ACCESS_KEY,
        region_name=getattr(settings, "YC_S3_REGION", "ru-central1"),
    )


def upload_video_bytes(data: bytes, key: str, content_type: str = "video/mp4") -> str:
    """
    Загружает байты видео в приватный бакет S3 или в MEDIA_ROOT при отсутствии S3.
    Возвращает s3_key или относительный путь.
    """
    if is_s3_configured():
        client = _get_s3_client()
        client.put_object(
            Bucket=settings.YC_S3_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    # Локальный фолбэк
    local_path = Path(settings.MEDIA_ROOT) / key
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return key


def generate_video_presigned_url(
    key: str, expires_in: int = PRESIGNED_URL_EXPIRES_IN, download_name: str | None = None
) -> str:
    """
    Генерирует временную подписанную ссылку на скачивание/просмотр видео.
    """
    if not key:
        return ""

    if is_s3_configured():
        client = _get_s3_client()
        params = {"Bucket": settings.YC_S3_BUCKET_NAME, "Key": key}
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
        try:
            return client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires_in
            )
        except Exception:
            logger.exception("Не удалось сгенерировать presigned URL для видеоключа %s", key)
            raise

    # Локальный фолбэк
    base = getattr(settings, "FRONTEND_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/media/{key}"


def download_video_bytes(key: str) -> bytes:
    """
    Скачивает байты видео из S3 или локального диска.
    """
    if is_s3_configured():
        client = _get_s3_client()
        obj = client.get_object(Bucket=settings.YC_S3_BUCKET_NAME, Key=key)
        return obj["Body"].read()

    local_path = Path(settings.MEDIA_ROOT) / key
    with open(local_path, "rb") as f:
        return f.read()


def upload_video_from_url(source_url: str, job_id: str) -> tuple[str, str]:
    """
    Скачивает сгенерированное видео по URL (отданному Polza.ai) и сохраняет в хранилище.
    Возвращает (s3_key, presigned_url).
    """
    resp = requests.get(source_url, timeout=120, stream=True)
    resp.raise_for_status()
    video_bytes = resp.content

    key = f"video_results/{job_id}/{uuid.uuid4()}.mp4"
    upload_video_bytes(video_bytes, key, content_type="video/mp4")
    url = generate_video_presigned_url(key)
    return key, url
