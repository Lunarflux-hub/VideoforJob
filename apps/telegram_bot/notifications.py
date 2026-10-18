"""
Уведомление пользователя в Telegram о результате генерации.

Вызывается напрямую из apps.photos.tasks.generate_photo_task (Celery-воркер),
а не через polling из процесса бота — так уведомление доходит даже если
контейнер бота в этот момент перезапускается: это просто HTTP-запрос к
Telegram Bot API, не завязанный на его long-polling соединение.
"""
from __future__ import annotations

import logging
import socket

import requests
import urllib3.util.connection as urllib3_connection
from django.conf import settings

logger = logging.getLogger(__name__)

def _call(method: str, **params) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    url = _API_BASE.format(token=settings.TELEGRAM_BOT_TOKEN, method=method)

    original_allowed_gai_family = urllib3_connection.allowed_gai_family
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET6
    try:
        resp = requests.post(url, data=params, timeout=15)
        if not resp.ok:
            logger.warning("Telegram API %s вернул %s: %s", method, resp.status_code, resp.text)
    except requests.RequestException:
        logger.exception("Не удалось вызвать Telegram API %s", method)
    finally:
        urllib3_connection.allowed_gai_family = original_allowed_gai_family


def notify_payment_result(payment) -> None:
    """
    Уведомление в Telegram об успешном пополнении баланса. Вызывается из
    apps.billing.tasks.notify_payment_telegram (Celery-задача, поставленная в
    очередь из apps.billing.views.payanyway_result — сам server-to-server
    callback от PayAnyWay не должен ждать Telegram API). Тихо ничего не
    делает, если платёж пришёл не из бота (нет TelegramProfile) — значит,
    пользователь пополнял баланс с сайта.
    """
    profile = getattr(payment.user, "telegram_profile", None) if payment.user_id else None
    if profile is None:
        return

    from apps.billing.services import get_balance  # локальный импорт — избегаем цикла apps

    balance = get_balance(payment.user)
    _call(
        "sendMessage",
        chat_id=profile.telegram_id,
        text=(
            f"✅ Оплата получена! Начислено {payment.generations_granted} ген.\n"
            f"Текущий баланс: {balance} ген."
        ),
    )
