"""
Сбор событий продуктовой воронки для проверки гипотез VideoLab AI (ТЗ раздел 7).
"""
from __future__ import annotations

import logging
from typing import Any

from apps.videos.models import VideoAnalyticsEvent

logger = logging.getLogger(__name__)


def track_event(
    event_name: str,
    *,
    user=None,
    video_job=None,
    **metadata: Any,
) -> VideoAnalyticsEvent:
    """
    Записывает событие воронки в базу и лог.
    """
    try:
        event = VideoAnalyticsEvent.objects.create(
            user=user,
            video_job=video_job,
            event_name=event_name,
            metadata=metadata,
        )
        logger.info(
            "VideoFunnel [%s] user=%s job=%s meta=%s",
            event_name,
            user.pk if user else None,
            video_job.pk if video_job else None,
            metadata,
        )
        return event
    except Exception:
        logger.exception("Не удалось сохранить событие аналитики %s", event_name)
        return None
