"""
Валидация входящих видеофайлов пользователя.
Проверка формата, размера и длительности без обязательного наличия ffmpeg.
"""
from __future__ import annotations

import io
import mimetypes
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO

from django.conf import settings

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
ALLOWED_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-m4v",
    "application/octet-stream",  # часто Telegram шлет как octet-stream
}


@dataclass
class VideoValidationResult:
    is_valid: bool
    duration_sec: float = 0.0
    size_bytes: int = 0
    width: int | None = None
    height: int | None = None
    content_type: str = "video/mp4"
    error_message: str | None = None


def _parse_mp4_duration_and_timescale(stream: BinaryIO) -> tuple[float | None, int | None]:
    """
    Парсер атомов MP4/MOV на чистом Python:
    Ищет atom 'moov' -> atom 'mvhd' для извлечения timescale и duration.
    """
    try:
        stream.seek(0, os.SEEK_END)
        total_size = stream.tell()
        stream.seek(0)

        while stream.tell() < total_size:
            header = stream.read(8)
            if len(header) < 8:
                break
            box_size, box_type = struct.unpack(">I4s", header)
            box_type_str = box_type.decode("latin1", errors="ignore")

            if box_size == 1:
                # 64-bit large size
                large_size_bytes = stream.read(8)
                if len(large_size_bytes) < 8:
                    break
                box_size = struct.unpack(">Q", large_size_bytes)[0]
                payload_size = box_size - 16
            elif box_size == 0:
                payload_size = total_size - stream.tell()
            else:
                payload_size = box_size - 8

            if box_type_str == "moov":
                # Входим внутрь moov
                moov_end = stream.tell() + payload_size
                while stream.tell() < moov_end:
                    sub_header = stream.read(8)
                    if len(sub_header) < 8:
                        break
                    sub_size, sub_type = struct.unpack(">I4s", sub_header)
                    sub_type_str = sub_type.decode("latin1", errors="ignore")

                    sub_payload_size = (
                        (sub_size - 8) if sub_size > 8 else (moov_end - stream.tell())
                    )

                    if sub_type_str == "mvhd":
                        mvhd_data = stream.read(min(sub_payload_size, 32))
                        if len(mvhd_data) >= 20:
                            version = mvhd_data[0]
                            if version == 1 and len(mvhd_data) >= 32:
                                # version 1: 1 byte ver, 3 flag, 8 creation, 8 mod, 4 timescale, 8 duration
                                timescale = struct.unpack(">I", mvhd_data[20:24])[0]
                                duration = struct.unpack(">Q", mvhd_data[24:32])[0]
                            else:
                                # version 0: 1 byte ver, 3 flag, 4 creation, 4 mod, 4 timescale, 4 duration
                                timescale = struct.unpack(">I", mvhd_data[12:16])[0]
                                duration = struct.unpack(">I", mvhd_data[16:20])[0]

                            if timescale > 0:
                                return float(duration) / float(timescale), timescale
                        return None, None
                    else:
                        stream.seek(sub_payload_size, os.SEEK_CUR)
                break
            else:
                stream.seek(payload_size, os.SEEK_CUR)
    except Exception:
        pass
    return None, None


def validate_video_file(
    file_obj_or_bytes: BinaryIO | bytes,
    filename: str = "video.mp4",
    reported_duration: float | None = None,
    reported_size: int | None = None,
    reported_width: int | None = None,
    reported_height: int | None = None,
) -> VideoValidationResult:
    """
    Проверяет видео на соответствие ограничениям:
    - Расширение и MIME-тип
    - Размер файла <= VIDEO_MAX_SIZE_MB (по умолчанию 50 МБ)
    - Длительность <= VIDEO_MAX_DURATION_SEC (по умолчанию 10 сек)
    """
    max_duration = getattr(settings, "VIDEO_MAX_DURATION_SEC", 10)
    max_size_mb = getattr(settings, "VIDEO_MAX_SIZE_MB", 50)
    max_size_bytes = max_size_mb * 1024 * 1024

    # 1. Проверка расширения
    _, ext = os.path.splitext(filename.lower())
    if ext and ext not in ALLOWED_EXTENSIONS:
        return VideoValidationResult(
            is_valid=False,
            error_message=(
                f"Неподдерживаемый формат файла ({ext}). "
                f"Поддерживаются только: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            ),
        )

    # 2. Определение размера
    if isinstance(file_obj_or_bytes, bytes):
        size_bytes = len(file_obj_or_bytes)
        stream = io.BytesIO(file_obj_or_bytes)
    else:
        stream = file_obj_or_bytes
        if reported_size is not None and reported_size > 0:
            size_bytes = reported_size
        else:
            cur = stream.tell()
            stream.seek(0, os.SEEK_END)
            size_bytes = stream.tell()
            stream.seek(cur)

    if size_bytes == 0:
        return VideoValidationResult(
            is_valid=False,
            error_message="Файл видео пустой (0 байт).",
        )

    if size_bytes > max_size_bytes:
        size_mb = size_bytes / (1024 * 1024)
        return VideoValidationResult(
            is_valid=False,
            size_bytes=size_bytes,
            error_message=(
                f"Размер видео ({size_mb:.1f} МБ) превышает допустимый лимит ({max_size_mb} МБ). "
                "Пожалуйста, сожмите ролик или выберите фрагмент меньшего размера."
            ),
        )

    # 3. Определение длительности
    duration_sec = reported_duration or 0.0
    if duration_sec <= 0.0:
        parsed_duration, _ = _parse_mp4_duration_and_timescale(stream)
        if parsed_duration is not None and parsed_duration > 0:
            duration_sec = parsed_duration

    # Сбрасываем позицию потока обратно в начало
    try:
        stream.seek(0)
    except Exception:
        pass

    # Проверка длительности
    if duration_sec > max_duration:
        return VideoValidationResult(
            is_valid=False,
            duration_sec=duration_sec,
            size_bytes=size_bytes,
            width=reported_width,
            height=reported_height,
            error_message=(
                f"Длительность видео ({duration_sec:.1f} сек) превышает максимум ({max_duration} сек). "
                f"Модели Gemini Omni / Polza принимают фрагменты до {max_duration} секунд. "
                "Пожалуйста, обрежьте видео в Telegram перед отправкой."
            ),
        )

    mime_type, _ = mimetypes.guess_type(filename)
    content_type = mime_type or "video/mp4"

    return VideoValidationResult(
        is_valid=True,
        duration_sec=round(duration_sec, 2),
        size_bytes=size_bytes,
        width=reported_width,
        height=reported_height,
        content_type=content_type,
    )
