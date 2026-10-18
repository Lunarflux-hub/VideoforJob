"""
Клавиатуры бота. Наборы значений для одежды/фона зеркалят choices из
apps/photos/serializers.py::OrderCreateSerializer — при изменении списка
там нужно поправить и здесь.
"""
from decimal import Decimal
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from django.conf import settings

MAIN_MENU_NEW_ORDER = "🖼 Новая генерация"
MAIN_MENU_HISTORY = "📜 История заказов"
MAIN_MENU_BALANCE = "💰 Баланс"
MAIN_MENU_PROFILE = "👤 Профиль"
MAIN_MENU_SUPPORT = "🆘 Поддержка"
MAIN_MENU_REVIEWS = "⭐ Отзывы"

MENU_NEW_ORDER_CB = "menu:new"
MENU_HISTORY_CB = "menu:history"
MENU_BALANCE_CB = "menu:balance"
MENU_PROFILE_CB = "menu:profile"
MENU_SUPPORT_CB = "menu:support"
MENU_HOME_CB = "menu:home"
MENU_TOPUP_CB = "menu:topup"
CHANGE_EMAIL_CB = "profile:change_email"
SUPPORT_FAQ_CB = "support:faq"
SUPPORT_WRITE_CB = "support:write"
REVIEW_RATE_CB = "review"  # review:<order_id>:<1..5>
REVIEW_SKIP_CB = "review_skip"  # review_skip:<order_id>

# value -> (иконка+подпись для кнопки, подпись без иконки для текстового резюме)
CLOTHING_LABELS = {
    "casual": "👕 Повседневная",
    "formal": "🤵 Деловая",
    "sport": "🏃 Спортивная",
    "jacket": "🧥 Пиджак",
    "shirt": "👔 Рубашка",
}

BACKGROUND_LABELS = {
    "office": "🏢 Офис",
    "nature": "🌳 Природа",
    "solid": "🎨 Однотонный фон",
    "upload": "🖼 Своё изображение",
}

BACKGROUND_COLOR_PRESETS = {
    "⬜ Белый": "#FFFFFF",
    "◽ Серый": "#808080",
    "🟦 Синий": "#0066FF",
    "⬛ Чёрный": "#000000",
}

SKIP = "skip"


MENU_VIDEO_NEW_CB = "video:new"
MENU_VIDEO_EXAMPLES_CB = "video:examples"
MENU_VIDEO_HISTORY_CB = "video:history"


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню VideoLab AI по ТЗ раздел 3.1."""
    rows = [
        [InlineKeyboardButton(text="🎬 Изменить видео", callback_data=MENU_VIDEO_NEW_CB)],
        [InlineKeyboardButton(text="👀 Посмотреть примеры", callback_data=MENU_VIDEO_EXAMPLES_CB)],
        [
            InlineKeyboardButton(text="💳 Купить обработки", callback_data=MENU_TOPUP_CB),
            InlineKeyboardButton(text="📁 Мои обработки", callback_data=MENU_VIDEO_HISTORY_CB),
        ],
        [
            InlineKeyboardButton(text="💰 Баланс", callback_data=MENU_BALANCE_CB),
            InlineKeyboardButton(text="💬 Поддержка", callback_data=MENU_SUPPORT_CB),
        ],
    ]
    if settings.TELEGRAM_REVIEWS_CHANNEL_URL:
        rows.append([InlineKeyboardButton(text=MAIN_MENU_REVIEWS, url=settings.TELEGRAM_REVIEWS_CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def video_purpose_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 Соцсети / Reels", callback_data="purpose:smm"),
                InlineKeyboardButton(text="🛍 Реклама товаров", callback_data="purpose:business"),
            ],
            [
                InlineKeyboardButton(text="👤 Личный контент", callback_data="purpose:personal"),
                InlineKeyboardButton(text="⏩ Пропустить", callback_data="purpose:skip"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)],
        ]
    )


def video_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="video:cancel")]]
    )


def video_category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌅 Атмосфера и освещение", callback_data="video_cat:lighting")],
            [InlineKeyboardButton(text="✨ Визуальный эффект / объект", callback_data="video_cat:effect")],
            [InlineKeyboardButton(text="🎨 Стилизация сцены", callback_data="video_cat:style")],
            [InlineKeyboardButton(text="✍️ Свой вариант инструкции", callback_data="video_cat:custom")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="video:cancel")],
        ]
    )


def video_presets_keyboard(presets) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.title, callback_data=f"video_preset:{p.id}")]
        for p in presets
    ]
    rows.append([InlineKeyboardButton(text="✍️ Ввести свой вариант", callback_data="video_cat:custom")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="video_cat:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def video_confirm_keyboard(
    credits_amount: Decimal | int | float = Decimal("1.0"),
    current_quality: str = "720p",
) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения с кнопками быстрого переключения качества по Матрице Tiered Billing."""
    dec_cr = Decimal(str(credits_amount))
    cr_str = f"{dec_cr:.1f}" if dec_cr % 1 != 0 else f"{int(dec_cr)}"

    tiers = [
        ("draft", "⚡ Черновой 0.5 кр"),
        ("720p", "🌟 720p 1.0 кр"),
        ("1080p", "💎 1080p 1.5 кр"),
        ("4k", "🎬 4K 2.0 кр"),
    ]
    tier_buttons = []
    for key, label in tiers:
        is_active = (key == current_quality)
        text = f"✅ {label}" if is_active else label
        tier_buttons.append(InlineKeyboardButton(text=text, callback_data=f"video:quality:{key}"))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 Запустить обработку ({cr_str} кр.)", callback_data="video:confirm_run")],
            tier_buttons[:2],
            tier_buttons[2:],
            [
                InlineKeyboardButton(text="✏️ Изменить запрос", callback_data="video:change_prompt"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="video:cancel"),
            ],
        ]
    )


def video_examples_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Изменить свое видео", callback_data=MENU_VIDEO_NEW_CB)],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data=MENU_HOME_CB)],
        ]
    )


def video_history_keyboard(jobs) -> InlineKeyboardMarkup:
    rows = []
    for job in jobs:
        status_icon = "✅" if job.status == "done" else ("⏳" if job.status in ("pending", "processing") else "❌")
        rows.append([
            InlineKeyboardButton(
                text=f"{status_icon} {job.created_at:%d.%m %H:%M} | {job.user_prompt[:25]}...",
                callback_data=f"video_job:{job.id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="🎬 Новая обработка", callback_data=MENU_VIDEO_NEW_CB)])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)]])


def balance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data=MENU_TOPUP_CB)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)],
        ]
    )


def _format_price(price) -> str:
    return f"{price:.0f} ₽" if price == price.to_integral_value() else f"{price} ₽"


def tariffs_keyboard(tariffs) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{tariff.title} — {tariff.generations} кр. — {_format_price(tariff.price)}"
                + (" 🎁" if tariff.is_promo else ""),
                callback_data=f"topup:pkg:{tariff.id}",
            )
        ]
        for tariff in tariffs
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(pay_url: str, payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"topup:check:{payment_id}")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)],
        ]
    )


def profile_keyboard(*, has_email: bool) -> InlineKeyboardMarkup:
    change_label = "✏️ Изменить email" if has_email else "✏️ Указать email"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=change_label, callback_data=CHANGE_EMAIL_CB)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)],
        ]
    )


def support_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❓ FAQ", callback_data=SUPPORT_FAQ_CB),
                InlineKeyboardButton(text="✍️ Написать в поддержку", callback_data=SUPPORT_WRITE_CB),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=MENU_HOME_CB)],
        ]
    )


def faq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=MENU_SUPPORT_CB)]]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])



