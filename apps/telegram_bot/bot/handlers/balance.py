from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User
from asgiref.sync import sync_to_async

from decimal import Decimal

from .. import keyboards, services
from ..ui import render, start_wizard

router = Router(name="balance")


async def _balance_text(tg_user: User) -> str:
    profile = await sync_to_async(services.get_or_create_profile)(tg_user.id, tg_user.username or "")
    balance = await sync_to_async(services.get_user_balance)(profile.user)
    dec_bal = Decimal(str(balance))
    bal_str = f"{dec_bal:.1f}" if dec_bal % 1 != 0 else f"{int(dec_bal)}"
    return (
        f"💰 <b>Ваш баланс:</b> <b>{bal_str} кредитов</b>\n\n"
        "📊 <b>Матрица списания (Tiered Billing):</b>\n"
        "• ⚡ <b>Черновой режим (до 65 ₽ себестоимость):</b> <code>0.5 кр.</code>\n"
        "• 🌟 <b>Стандарт 720p (66–115 ₽):</b> <code>1.0 кр.</code>\n"
        "• 💎 <b>Full HD 1080p (116–160 ₽):</b> <code>1.5 кр.</code>\n"
        "• 🎬 <b>Ultra 4K (161–205 ₽):</b> <code>2.0 кр.</code>\n\n"
        "💡 <i>Списание рассчитывается прозрачно перед каждым запуском. "
        "При технической ошибке кредиты возвращаются на баланс моментально.</i>"
    )


@router.message(Command("balance"))
async def show_balance(message: Message, state: FSMContext) -> None:
    text = await _balance_text(message.from_user)
    await start_wizard(message, state, text, keyboards.balance_keyboard())


@router.callback_query(F.data == keyboards.MENU_BALANCE_CB)
async def show_balance_cb(callback: CallbackQuery, state: FSMContext) -> None:
    text = await _balance_text(callback.from_user)
    await render(callback, state, text, keyboards.balance_keyboard())
