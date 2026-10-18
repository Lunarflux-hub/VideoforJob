import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from django.conf import settings

from .handlers import (
    balance,
    billing,
    profile,
    start,
    support,
    video,
)

logger = logging.getLogger(__name__)


def build_bot() -> Bot:
    session = AiohttpSession()
    if getattr(settings, "TELEGRAM_FORCE_IPV6", False):
        session._connector_init["family"] = socket.AF_INET6

    return Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher() -> Dispatcher:
    storage = None
    if getattr(settings, "REDIS_URL", None):
        try:
            import redis
            client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5)
            client.ping()
            storage = RedisStorage.from_url(settings.REDIS_URL)
            logger.info("FSM storage: Redis (%s)", settings.REDIS_URL)
        except Exception as exc:
            logger.info("Redis недоступен (%s), используем MemoryStorage: %s", settings.REDIS_URL, exc)
            storage = MemoryStorage()
    else:
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)

    # Порядок регистрации роутеров:
    # 1. start (команды /start, /cancel)
    # 2. video (основной сценарий VideoLab AI)
    # 3. биллинг, баланс, профиль, поддержка
    dp.include_router(start.router)
    dp.include_router(video.router)
    dp.include_router(balance.router)
    dp.include_router(billing.router)
    dp.include_router(profile.router)
    dp.include_router(support.router)

    return dp
