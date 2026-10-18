"""
Клиент для работы с Polza.ai Media API (генерация и редактирование видео).

Официальная спецификация Polza.ai:
- POST https://polza.ai/api/v1/media
  {
    "model": "google/gemini-omni-1.1-flash",  // или "gemini-omni-video"
    "input": {
      "prompt": "...",
      "aspect_ratio": "16:9" | "9:16",
      "resolution": "720p" | "1080p" | "4k",
      "duration": "4" | "6" | "8" | "10",
      "videos": [
        {"type": "url", "data": "https://..."}
      ]
    },
    "async": true
  }
  -> {"id": "gen_...", "status": "pending"}

- GET https://polza.ai/api/v1/media/{id}
  -> {"id": "gen_...", "status": "completed"|"failed"|"cancelled", "data": {...}}

- POST https://polza.ai/api/v1/storage/upload (для приватных видео)
  Multipart form с файлом
  -> {"url": "https://cdn.polza.ai/files/..."}
"""
from __future__ import annotations

import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MEDIA_ENDPOINT = "/media"
STORAGE_UPLOAD_ENDPOINT = "/storage/upload"
DEFAULT_POLL_INTERVAL_SEC = 5
DEFAULT_POLL_TIMEOUT_SEC = 300

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class PolzaVideoError(Exception):
    """Базовое исключение при ошибке вызова Polza.ai API."""
    def __init__(self, message: str, raw_payload: Any = None, media_id: str | None = None):
        super().__init__(message)
        self.raw_payload = raw_payload
        self.media_id = media_id


@dataclass
class VideoGenerationResult:
    video_url: str
    media_id: str
    raw_response: dict
    duration_sec: float | None = None


def _headers() -> dict[str, str]:
    api_key = getattr(settings, "POLZA_API_KEY", "")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return getattr(settings, "POLZA_BASE_URL", "https://polza.ai/api/v1").rstrip("/")


def upload_video_to_polza_storage(
    file_bytes_or_path: bytes | str, filename: str = "video.mp4", mime_type: str = "video/mp4"
) -> str:
    """
    Загружает файл в хранилище Polza.ai (POST /api/v1/storage/upload).
    Возвращает постоянный/временный CDN URL для передачи в media API.
    """
    api_key = getattr(settings, "POLZA_API_KEY", "")
    if not api_key:
        raise PolzaVideoError("POLZA_API_KEY не задан в конфигурации .env")

    url = f"{_base_url()}{STORAGE_UPLOAD_ENDPOINT}"
    headers = {"Authorization": f"Bearer {api_key}"}

    if isinstance(file_bytes_or_path, str):
        with open(file_bytes_or_path, "rb") as f:
            files = {"file": (filename, f, mime_type)}
            data = {"storagePolicy": "TEMP_UPLOAD"}
            resp = requests.post(url, headers=headers, files=files, data=data, timeout=120)
    else:
        files = {"file": (filename, file_bytes_or_path, mime_type)}
        data = {"storagePolicy": "TEMP_UPLOAD"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=120)

    try:
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.exception("Polza Storage upload error: %s", resp.text if resp else "")
        raise PolzaVideoError(f"Ошибка загрузки видео в Polza Storage: {exc}") from exc

    payload = resp.json()
    cdn_url = payload.get("url")
    if not cdn_url:
        raise PolzaVideoError(f"Polza Storage не вернул url в ответе: {payload}")
    return cdn_url


def create_video_task(
    prompt: str,
    video_source_url: str,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    duration: str = "6",
    model: str | None = None,
) -> dict:
    """
    Отправляет POST /media для запуска задачи редактирования видео (video-to-video).
    """
    api_key = getattr(settings, "POLZA_API_KEY", "")
    if not api_key:
        raise PolzaVideoError("POLZA_API_KEY не задан в конфигурации .env")

    model_name = model or getattr(
        settings, "POLZA_VIDEO_MODEL", "google/gemini-omni-1.1-flash"
    )

    payload = {
        "model": model_name,
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "duration": str(duration),
            "videos": [{"type": "url", "data": video_source_url}],
        },
        "async": True,
    }

    url = f"{_base_url()}{MEDIA_ENDPOINT}"
    logger.info("Отправка video-to-video задачи в Polza.ai (%s): %s", model_name, payload)

    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        err_body = ""
        try:
            err_body = exc.response.text if exc.response is not None else ""
        except Exception:
            pass
        logger.exception("Ошибка Polza.ai POST /media: %s", err_body)
        raise PolzaVideoError(f"Ошибка создания задачи видео в Polza.ai: {exc} | {err_body}") from exc

    data = resp.json()
    logger.debug("Polza.ai POST /media ответ: %s", data)
    return data


def poll_video_task(
    media_id: str,
    timeout_sec: int | None = None,
    poll_interval_sec: int | None = None,
) -> dict:
    """
    Периодически опрашивает GET /media/{media_id} до завершения (completed, failed, cancelled)
    или истечения таймаута.
    """
    timeout = timeout_sec or getattr(
        settings, "POLZA_VIDEO_TIMEOUT_SEC", DEFAULT_POLL_TIMEOUT_SEC
    )
    interval = poll_interval_sec or getattr(
        settings, "POLZA_VIDEO_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC
    )
    deadline = time.monotonic() + timeout

    url = f"{_base_url()}{MEDIA_ENDPOINT}/{media_id}"

    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, headers=_headers(), timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("Ошибка опроса статуса Polza.ai (%s): %s", media_id, exc)
            time.sleep(interval)
            continue

        status = data.get("status")
        logger.debug("Polza.ai GET /media/%s статус: %s", media_id, status)

        if status in TERMINAL_STATUSES:
            return data

        time.sleep(interval)

    raise PolzaVideoError(
        f"Превышен таймаут ожидания обработки видео Polza.ai ({timeout}с, media_id={media_id})",
        media_id=media_id,
    )


def extract_video_result(status_payload: dict) -> VideoGenerationResult:
    """
    Извлекает URL готового видео из ответа Polza.ai со статусом 'completed'.
    """
    status = status_payload.get("status")
    media_id = status_payload.get("id", "")

    if status == "failed":
        error = status_payload.get("error") or {}
        message = (
            error.get("message")
            if isinstance(error, dict)
            else str(error)
        ) or "Неизвестная ошибка Polza.ai"
        raise PolzaVideoError(
            f"Polza.ai ошибка генерации: {message}",
            raw_payload=status_payload,
            media_id=media_id,
        )

    if status == "cancelled":
        raise PolzaVideoError(
            "Генерация видео была отменена Polza.ai",
            raw_payload=status_payload,
            media_id=media_id,
        )

    data = status_payload.get("data")
    if not data:
        raise PolzaVideoError(
            "Polza.ai вернул статус completed без поля data",
            raw_payload=status_payload,
            media_id=media_id,
        )

    # 1. data — строка URL
    if isinstance(data, str) and data.startswith("http"):
        return VideoGenerationResult(
            video_url=data, media_id=media_id, raw_response=status_payload
        )

    # 2. data — словарь с полем url / video_url / videos
    if isinstance(data, dict):
        if data.get("url"):
            return VideoGenerationResult(
                video_url=data["url"], media_id=media_id, raw_response=status_payload
            )
        if data.get("video_url"):
            return VideoGenerationResult(
                video_url=data["video_url"], media_id=media_id, raw_response=status_payload
            )
        videos = data.get("videos") or data.get("output")
        if isinstance(videos, list) and videos:
            first = videos[0]
            if isinstance(first, str) and first.startswith("http"):
                return VideoGenerationResult(
                    video_url=first, media_id=media_id, raw_response=status_payload
                )
            if isinstance(first, dict) and first.get("url"):
                return VideoGenerationResult(
                    video_url=first["url"], media_id=media_id, raw_response=status_payload
                )

    # 3. data — список
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, str) and first.startswith("http"):
            return VideoGenerationResult(
                video_url=first, media_id=media_id, raw_response=status_payload
            )
        if isinstance(first, dict) and first.get("url"):
            return VideoGenerationResult(
                video_url=first["url"], media_id=media_id, raw_response=status_payload
            )

    raise PolzaVideoError(
        f"Не удалось извлечь URL готового видео из ответа Polza: {data!r}",
        raw_payload=status_payload,
    )


def process_video_edit(
    prompt: str,
    source_video_url: str,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    duration: str = "6",
    model: str | None = None,
    on_task_created: Any = None,
) -> VideoGenerationResult:
    """
    Полный синхронный пайплайн: создание задачи + поллинг + извлечение результата.
    Используется внутри Celery воркера.
    """
    created = create_video_task(
        prompt=prompt,
        video_source_url=source_video_url,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        duration=duration,
        model=model,
    )

    media_id = created.get("id") or ""
    if on_task_created and media_id:
        try:
            on_task_created(media_id)
        except Exception:
            logger.exception("Ошибка вызова on_task_created для media_id=%s", media_id)

    if created.get("status") in TERMINAL_STATUSES:
        final_payload = created
    else:
        if not media_id:
            raise PolzaVideoError(f"Polza.ai не вернул id задачи: {created!r}", raw_payload=created)
        final_payload = poll_video_task(media_id)

    return extract_video_result(final_payload)
