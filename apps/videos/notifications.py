"""
Уведомления пользователя о статусе и результатах обработки видео в Telegram.
Работает напрямую через HTTP API Telegram из фонового Celery воркера.
"""
from __future__ import annotations

import json
import logging
import socket
from typing import Any

import requests
import urllib3.util.connection as urllib3_connection
from django.conf import settings

from apps.billing.services import get_balance

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _call_telegram(method: str, data: dict[str, Any] | None = None, files: dict | None = None) -> dict | None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — отправка уведомления пропущена")
        return None

    url = _API_BASE.format(token=token, method=method)
    force_ipv6 = getattr(settings, "TELEGRAM_FORCE_IPV6", False)

    def _do_post():
        return requests.post(url, data=data, files=files, timeout=40)

    if force_ipv6:
        original_allowed_gai_family = urllib3_connection.allowed_gai_family
        urllib3_connection.allowed_gai_family = lambda: socket.AF_INET6
        try:
            resp = _do_post()
        finally:
            urllib3_connection.allowed_gai_family = original_allowed_gai_family
    else:
        try:
            resp = _do_post()
        except (requests.ConnectionError, requests.Timeout):
            # Попытка фолбэка на IPv6, если IPv4 завис/заблокирован на хостинге
            original_allowed_gai_family = urllib3_connection.allowed_gai_family
            urllib3_connection.allowed_gai_family = lambda: socket.AF_INET6
            try:
                resp = _do_post()
            finally:
                urllib3_connection.allowed_gai_family = original_allowed_gai_family

    if not resp.ok:
        logger.warning("Telegram Bot API %s returned %s: %s", method, resp.status_code, resp.text)
        return None

    return resp.json()


def _result_keyboard_markup(job_id: str) -> str:
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Результат подходит", "callback_data": f"video:rate:{job_id}:good"},
                {"text": "❌ Не подходит", "callback_data": f"video:rate:{job_id}:bad"},
            ],
            [
                {"text": "🔄 Создать ещё вариант (1 кред.)", "callback_data": f"video:retry:{job_id}"},
            ],
            [
                {"text": "📁 Мои обработки", "callback_data": "video:history"},
                {"text": "⬅️ В меню", "callback_data": "menu:home"},
            ],
        ]
    }
    return json.dumps(keyboard)


def _failure_keyboard_markup() -> str:
    keyboard = {
        "inline_keyboard": [
            [{"text": "🎬 Попробовать снова", "callback_data": "video:new"}],
            [
                {"text": "💬 Поддержка", "callback_data": "menu:support"},
                {"text": "⬅️ В меню", "callback_data": "menu:home"},
            ],
        ]
    }
    return json.dumps(keyboard)


def notify_video_job_done(job) -> None:
    """
    Отправляет готовое видео и кнопки оценки пользователю.
    """
    chat_id = job.telegram_chat_id
    if not chat_id and hasattr(job.user, "telegram_profile"):
        chat_id = job.user.telegram_profile.telegram_id

    if not chat_id:
        logger.info("У заказа %s нет telegram_chat_id — уведомление не отправлено", job.pk)
        return

    result = getattr(job, "result", None)
    if not result:
        _call_telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "✅ Обработка завершена, но файл результата не найден. Обратитесь в поддержку.",
                "parse_mode": "HTML",
                "reply_markup": _failure_keyboard_markup(),
            },
        )
        return

    caption = (
        f"🎬 <b>Ваше видео готово!</b>\n\n"
        f"📝 <b>Задача:</b> <i>{job.user_prompt[:150]}</i>\n\n"
        f"Оцените качество генерации ниже:"
    )

    markup = _result_keyboard_markup(str(job.id))

    # Сначала пытаемся отправить как sendVideo по URL
    sent = _call_telegram(
        "sendVideo",
        {
            "chat_id": chat_id,
            "video": result.file_url,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": markup,
        },
    )

    if sent and sent.get("ok"):
        try:
            tg_result = sent.get("result", {})
            video_info = tg_result.get("video") or {}
            file_id = video_info.get("file_id")
            if file_id:
                result.telegram_file_id = file_id
                result.save(update_fields=["telegram_file_id"])
        except Exception:
            pass
        return

    # Если sendVideo не удался (например, файл > 20 МБ для прямой загрузки по URL),
    # отправляем сообщение со ссылкой на скачивание
    fallback_text = (
        f"🎬 <b>Ваше видео готово!</b>\n\n"
        f"📝 <b>Задача:</b> <i>{job.user_prompt[:150]}</i>\n\n"
        f"📥 <a href=\"{result.file_url}\"><b>Нажмите сюда, чтобы открыть и скачать видео</b></a>\n\n"
        f"<i>Ссылка действительна в течение 3 дней.</i>\n\n"
        f"Оцените качество генерации:"
    )
    _call_telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": fallback_text,
            "parse_mode": "HTML",
            "reply_markup": markup,
            "disable_web_page_preview": False,
        },
    )


def notify_video_job_failed(job, error_text: str) -> None:
    """
    Уведомляет о технической ошибке и автоматическом возврате кредита.
    """
    chat_id = job.telegram_chat_id
    if not chat_id and hasattr(job.user, "telegram_profile"):
        chat_id = job.user.telegram_profile.telegram_id

    if not chat_id:
        return

    current_balance = get_balance(job.user)
    text = (
        f"⚠️ <b>Не удалось обработать видео</b>\n\n"
        f"Причина: <i>{error_text}</i>\n\n"
        f"💰 <b>Кредит за обработку автоматически возвращён на ваш баланс.</b>\n"
        f"Текущий баланс: <b>{current_balance} генераций</b>.\n\n"
        f"Вы можете попробовать запустить ролик ещё раз с другим описанием."
    )
    _call_telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": _failure_keyboard_markup(),
        },
    )
