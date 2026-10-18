from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async
from django.conf import settings

from apps.videos.services.analytics import track_event

from .. import keyboards, services
from ..ui import finish_flow, render, return_to_parent, start_wizard, start_wizard_photo

router = Router(name="start")

SUPPORT_LINE = (
    "☁️ <i>Если у вас есть вопросы или возникла проблема — обратитесь в нашу поддержку.</i>"
)

WELCOME_IMAGE_PATH = Path(__file__).resolve().parents[2] / "assets" / "welcome.png"


def _welcome_text() -> str:
    offer_url = f"{settings.FRONTEND_URL}/policy/?doc=offer"
    price_single = getattr(settings, "VIDEO_PRICE_SINGLE", 299)
    retention_hours = getattr(settings, "VIDEO_RETENTION_HOURS", 48)

    return (
        "👋 <b>Добро пожаловать в VideoLab AI!</b>\n\n"
        "🎬 <b>Быстрое редактирование видео с помощью нейросетей:</b>\n"
        "Загрузите короткий ролик (до 10 секунд) → опишите желаемые изменения → получите результат.\n\n"
        "💡 <b>Нейросеть Gemini Omni умеет:</b>\n"
        "• Менять освещение, закатный/ночной свет и атмосферу\n"
        "• Добавлять кинематографичные эффекты, дым, неон, сияние\n"
        "• Стилизовать сцену (винтажная 35мм плёнка, коммерческий глянец)\n"
        "• Сохранять оригинальное движение персонажа и камеры\n\n"
        f"💰 <b>Стоимость:</b> от {price_single:.0f} ₽ за обработку (пакеты выгоднее)\n"
        f"🔒 <b>Конфиденциальность:</b> приватное хранение, автоматическое удаление файлов через {retention_hours} ч.\n\n"
        f'Перед использованием ознакомьтесь с <a href="{offer_url}">правилами сервиса</a>.'
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    profile = await sync_to_async(services.get_or_create_profile)(
        message.from_user.id, message.from_user.username or ""
    )

    await sync_to_async(track_event)("bot_started", user=profile.user)

    text = f"{_welcome_text()}\n\nВыберите действие:\n\n{SUPPORT_LINE}"
    if WELCOME_IMAGE_PATH.exists():
        await start_wizard_photo(message, state, WELCOME_IMAGE_PATH, text, keyboards.main_menu())
    else:
        await start_wizard(message, state, text, keyboards.main_menu())


@router.callback_query(F.data == keyboards.MENU_HOME_CB)
async def go_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render(callback, state, "Выберите действие:", keyboards.main_menu())


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext) -> None:
    text = "Отменено. Что дальше?"
    handled = await return_to_parent(event, state, text, keyboards.main_menu())
    if not handled:
        await render(event, state, text, keyboards.main_menu())
    await finish_flow(state)
