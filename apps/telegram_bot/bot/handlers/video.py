"""
Обработчики диалога VideoLab AI:
Загрузка короткого видео -> выбор задачи -> подтверждение -> фоновая генерация -> выдача и оценка.
"""
from __future__ import annotations

import io
import logging
import os
from decimal import Decimal
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from apps.billing.services import (
    InsufficientBalanceError,
    get_balance,
    spend_generation_for_video,
)
from apps.videos.models import UploadedVideo, VideoJob, VideoPreset
from apps.videos.services.analytics import track_event
from apps.videos.services.cost_calculator import calculate_tiered_credits
from apps.videos.services.video_validator import validate_video_file
from apps.videos.tasks import process_video_job_task

from .. import keyboards, services
from ..states import VideoFlow
from ..ui import finish_flow, render, start_wizard

logger = logging.getLogger(__name__)

router = Router(name="video")


async def _current_profile(user: User):
    return await sync_to_async(services.get_or_create_profile)(user.id, user.username or "")


# -----------------------------------------------------------------------------
# 1. Точка входа: /video или кнопка «🎬 Изменить видео»
# -----------------------------------------------------------------------------

def format_credits(val: Decimal | float | int) -> str:
    d = Decimal(str(val))
    if d % 1 == 0:
        return str(int(d))
    return f"{d:.2f}".rstrip("0").rstrip(".")


async def _start_video_flow(target: Message, tg_user: User, state: FSMContext) -> None:
    profile = await _current_profile(tg_user)
    balance = await sync_to_async(get_balance)(profile.user)

    price_single = getattr(settings, "VIDEO_PRICE_SINGLE", Decimal("299.00"))
    dec_bal = Decimal(str(balance))

    await sync_to_async(track_event)(
        "price_shown",
        user=profile.user,
        balance=float(dec_bal),
        price_single=str(price_single),
    )

    if dec_bal < Decimal("0.5"):
        bal_str = format_credits(dec_bal)
        text = (
            "🎬 <b>AI-редактирование видео (VideoLab AI)</b>\n\n"
            "Загрузите короткое видео до 10 секунд — нейросеть Gemini Omni изменит освещение, "
            "атмосферу или добавит визуальные эффекты, сохранив движение персонажа.\n\n"
            "💰 <b>Матрица списания кредитов (Tiered Billing):</b>\n"
            "• ⚡ Черновик (Draft 4–6с): <b>0.5 кр.</b>\n"
            "• 🔹 Лёгкая обработка (Draft 8–10с, 720p 4–6с): <b>0.75 кр.</b>\n"
            "• 🌟 Стандарт (720p 8–10с, 1080p 4–6с): <b>1.0 кр.</b>\n"
            "• 💎 Высокое качество (1080p 8–10с, 4K 4–8с): <b>1.5 кр.</b>\n"
            "• 🎬 Максимум (4K 10с): <b>2.0 кр.</b>\n\n"
            f"Ваш текущий баланс: <b>{bal_str} кредитов</b>.\n"
            "Пополните баланс кредитов, чтобы начать:"
        )
        await start_wizard(target, state, text, keyboards.balance_keyboard())
        return

    # Если баланс >= 1, начинаем воронку
    await sync_to_async(track_event)("video_upload_started", user=profile.user)
    await state.set_state(VideoFlow.waiting_purpose)

    text = (
        "🎯 <b>Назначение видео</b>\n\n"
        "Для чего вы готовите этот ролик? Это поможет лучше подобрать параметры модели "
        "(можно пропустить):"
    )
    await start_wizard(target, state, text, keyboards.video_purpose_keyboard())


@router.message(Command("video"))
async def cmd_video(message: Message, state: FSMContext) -> None:
    await _start_video_flow(message, message.from_user, state)


@router.callback_query(F.data == keyboards.MENU_VIDEO_NEW_CB)
async def cb_video_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await _start_video_flow(callback.message, callback.from_user, state)
    await callback.answer()


# -----------------------------------------------------------------------------
# 2. Выбор назначения видео (Сегментация)
# -----------------------------------------------------------------------------

@router.callback_query(VideoFlow.waiting_purpose, F.data.startswith("purpose:"))
async def choose_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    purpose = callback.data.split(":", 1)[1]
    await state.update_data(video_purpose=purpose)
    await state.set_state(VideoFlow.waiting_video)

    max_sec = getattr(settings, "VIDEO_MAX_DURATION_SEC", 10)
    max_mb = getattr(settings, "VIDEO_MAX_SIZE_MB", 50)

    text = (
        "📤 <b>Отправьте исходный видеоролик</b>\n\n"
        "📋 <b>Требования:</b>\n"
        f"• Формат: <b>MP4, MOV</b> или WebM\n"
        f"• Длительность: <b>до {max_sec} секунд</b>\n"
        f"• Размер: <b>до {max_mb} МБ</b>\n\n"
        "💡 <i>Совет: выбирайте фрагмент с понятным объектом или человеком в кадре. "
        "При необходимости обрежьте видео прямо в Telegram перед отправкой.</i>"
    )
    await render(callback, state, text, keyboards.video_cancel_keyboard())
    await callback.answer()


# -----------------------------------------------------------------------------
# 3. Приём и валидация видеофайла
# -----------------------------------------------------------------------------

@router.message(VideoFlow.waiting_video, F.video | F.document)
async def receive_video(message: Message, state: FSMContext) -> None:
    bot = message.bot
    tg_video = message.video

    if tg_video:
        file_id = tg_video.file_id
        file_name = tg_video.file_name or "video.mp4"
        reported_duration = float(tg_video.duration or 0)
        reported_size = tg_video.file_size or 0
        reported_width = tg_video.width
        reported_height = tg_video.height
    else:
        doc = message.document
        file_id = doc.file_id
        file_name = doc.file_name or "video.mp4"
        reported_duration = 0.0
        reported_size = doc.file_size or 0
        reported_width = None
        reported_height = None

    status_msg = await message.answer("⏳ Загружаем и проверяем видео...")

    try:
        tg_file = await bot.get_file(file_id)
        file_bytes_stream = io.BytesIO()
        await bot.download_file(tg_file.file_path, file_bytes_stream)
        file_bytes = file_bytes_stream.getvalue()
    except Exception as exc:
        logger.exception("Ошибка скачивания видео из Telegram: %s", exc)
        await status_msg.edit_text(
            "⚠️ Не удалось загрузить видео из Telegram. Попробуйте отправить файл снова.",
            reply_markup=keyboards.video_cancel_keyboard(),
        )
        return

    # Валидация
    val_res = validate_video_file(
        file_obj_or_bytes=file_bytes,
        filename=file_name,
        reported_duration=reported_duration,
        reported_size=reported_size,
        reported_width=reported_width,
        reported_height=reported_height,
    )

    if not val_res.is_valid:
        await status_msg.edit_text(
            f"❌ <b>Видео не подходит</b>\n\n{val_res.error_message}\n\nПожалуйста, отправьте подходящий ролик:",
            reply_markup=keyboards.video_cancel_keyboard(),
        )
        return

    # Сохраняем временные данные видео в сессии FSM
    temp_dir = Path(settings.MEDIA_ROOT) / "temp_video_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_filename = f"{message.from_user.id}_{file_id[:16]}_{file_name}"
    temp_file_path = temp_dir / temp_filename

    with open(temp_file_path, "wb") as f:
        f.write(file_bytes)

    await state.update_data(
        temp_file_path=str(temp_file_path),
        filename=file_name,
        duration_sec=val_res.duration_sec,
        size_bytes=val_res.size_bytes,
        width=val_res.width,
        height=val_res.height,
        content_type=val_res.content_type,
    )

    profile = await _current_profile(message.from_user)
    await sync_to_async(track_event)(
        "video_uploaded",
        user=profile.user,
        duration=val_res.duration_sec,
        size_bytes=val_res.size_bytes,
    )

    await state.set_state(VideoFlow.choosing_category)
    duration_str = f"{val_res.duration_sec:.1f}" if val_res.duration_sec > 0 else "< 10"

    await status_msg.edit_text(
        f"✅ <b>Видео принято!</b> (длительность: ~{duration_str} сек)\n\n"
        "Выберите, какое изменение нужно применить к ролику:",
        reply_markup=keyboards.video_category_keyboard(),
    )


@router.message(VideoFlow.waiting_video)
async def receive_non_video(message: Message) -> None:
    await message.answer(
        "⚠️ Пожалуйста, отправьте именно <b>видеофайл</b> (до 10 секунд).",
        reply_markup=keyboards.video_cancel_keyboard(),
    )


# -----------------------------------------------------------------------------
# 4. Выбор категории или свой вариант
# -----------------------------------------------------------------------------

@router.callback_query(VideoFlow.choosing_category, F.data.startswith("video_cat:"))
async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":", 1)[1]

    if category == "back":
        await state.set_state(VideoFlow.choosing_category)
        await render(
            callback,
            state,
            "Выберите задачу для ролика:",
            keyboards.video_category_keyboard(),
        )
        await callback.answer()
        return

    await state.update_data(chosen_category=category)

    if category == "custom":
        await state.set_state(VideoFlow.waiting_custom_prompt)
        text = (
            "✍️ <b>Опишите, что нужно изменить в видео</b>\n\n"
            "Пример:\n"
            "<i>«Сделай видео более кинематографичным, добавь мягкий закатный свет, "
            "сохрани основного персонажа и движение камеры»</i>\n\n"
            "Пришлите вашу инструкцию обычным текстом:"
        )
        await render(callback, state, text, keyboards.video_cancel_keyboard())
        await callback.answer()
        return

    # Загружаем готовые пресеты для выбранной категории
    presets = await sync_to_async(list)(
        VideoPreset.objects.filter(category=category, is_active=True).order_by("sort_order")
    )

    if not presets:
        # Если пресетов нет, сразу просим свой текст
        await state.set_state(VideoFlow.waiting_custom_prompt)
        await render(
            callback,
            state,
            "Опишите желаемые изменения текстом:",
            keyboards.video_cancel_keyboard(),
        )
        await callback.answer()
        return

    await state.set_state(VideoFlow.choosing_preset)
    await render(
        callback,
        state,
        "Выберите готовый шаблон задачи или введите свой вариант:",
        keyboards.video_presets_keyboard(presets),
    )
    await callback.answer()


@router.callback_query(VideoFlow.choosing_preset, F.data.startswith("video_preset:"))
async def choose_preset(callback: CallbackQuery, state: FSMContext) -> None:
    preset_id = int(callback.data.split(":", 1)[1])
    preset = await sync_to_async(VideoPreset.objects.filter(id=preset_id).first)()

    if not preset:
        await callback.answer("Шаблон не найден, выберите другой", show_alert=True)
        return

    await state.update_data(
        preset_id=preset.id,
        user_prompt=preset.title,
        final_prompt=preset.prompt_template,
        preset_title=preset.title,
    )

    await _show_order_confirmation(callback, state)
    await callback.answer()


@router.message(VideoFlow.waiting_custom_prompt, F.text)
async def receive_custom_prompt(message: Message, state: FSMContext) -> None:
    custom_text = message.text.strip()
    if len(custom_text) < 5:
        await message.answer(
            "⚠️ Инструкция слишком короткая. Опишите чуть подробнее, что нужно изменить."
        )
        return

    final_prompt = (
        f"{custom_text}. Maintain the core character, camera dynamics and primary action unchanged."
    )

    await state.update_data(
        preset_id=None,
        user_prompt=custom_text,
        final_prompt=final_prompt,
        preset_title="Свой вариант",
    )

    await _show_order_confirmation(message, state)


# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# 5. Подтверждение заказа и стоимости по Матрице Tiered Billing
# -----------------------------------------------------------------------------

QUALITY_LABELS = {
    "draft": "⚡ Черновой (быстрый тест)",
    "720p": "🌟 Стандарт 720p (Reels / Shorts)",
    "1080p": "💎 Full HD 1080p (высокая чёткость)",
    "4k": "🎬 Ultra 4K (максимум деталей)",
}


async def _show_order_confirmation(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    duration = data.get("duration_sec", 6)
    size_mb = (data.get("size_bytes", 0) or 0) / (1024 * 1024)
    user_prompt = data.get("user_prompt", "")
    preset_title = data.get("preset_title", "Индивидуальный запрос")
    quality = data.get("quality", "720p")

    # Расчёт списания по Матрице Tiered Billing из бизнес-плана
    credits_needed, expected_cogs = calculate_tiered_credits(
        resolution="720p" if quality == "draft" else quality,
        duration_sec=duration,
        is_draft=(quality == "draft"),
    )
    await state.update_data(quality=quality, credits_needed=str(credits_needed))

    user = event.from_user
    profile = await _current_profile(user)
    balance = await sync_to_async(get_balance)(profile.user)
    dec_bal = Decimal(str(balance))
    bal_after = dec_bal - credits_needed

    await sync_to_async(track_event)(
        "edit_prompt_submitted",
        user=profile.user,
        prompt=user_prompt[:100],
    )

    await state.set_state(VideoFlow.confirm)

    duration_str = f"{duration:.1f}" if duration > 0 else "< 10"
    quality_label = QUALITY_LABELS.get(quality, "🌟 Стандарт 720p")
    cr_str = format_credits(credits_needed)
    bal_str = format_credits(dec_bal)
    after_str = format_credits(bal_after)

    text = (
        "📋 <b>Подтверждение заказа</b>\n\n"
        f"🎬 <b>Ролик:</b> ~{duration_str} сек, {size_mb:.1f} МБ\n"
        f"🎯 <b>Режим:</b> {preset_title}\n"
        f"📝 <b>Задача:</b> <i>«{user_prompt}»</i>\n"
        f"⚙️ <b>Качество:</b> {quality_label}\n\n"
        f"💰 <b>Списание по матрице:</b> <b>{cr_str} кр.</b> <i>(себестоимость ~{expected_cogs:.0f} ₽)</i>\n"
        f"📊 <b>Ваш баланс:</b> {bal_str} кр. ➔ <b>Останется:</b> {after_str} кр.\n\n"
        "💡 <i>Выберите качество кнопками ниже или подтвердите запуск:</i>\n"
        "⚠️ <i>Результат генерируется моделью Gemini Omni. "
        "При техническом сбое кредиты возвращаются на баланс моментально.</i>"
    )

    await render(event, state, text, keyboards.video_confirm_keyboard(credits_amount=credits_needed, current_quality=quality))


@router.callback_query(VideoFlow.confirm, F.data.startswith("video:quality:"))
async def switch_quality_handler(callback: CallbackQuery, state: FSMContext) -> None:
    new_quality = callback.data.split(":")[-1]
    await state.update_data(quality=new_quality)
    await _show_order_confirmation(callback, state)
    await callback.answer(f"Качество: {new_quality}")


@router.callback_query(VideoFlow.confirm, F.data == "video:confirm_run")
async def confirm_run(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    temp_file_path = data.get("temp_file_path")

    if not temp_file_path or not os.path.exists(temp_file_path):
        await callback.message.edit_text(
            "⚠️ Файл видео устарел или был удален. Пожалуйста, отправьте видео снова.",
            reply_markup=keyboards.back_to_menu_keyboard(),
        )
        await finish_flow(state)
        return

    profile = await _current_profile(callback.from_user)
    quality = data.get("quality", "720p")
    duration = data.get("duration_sec", 6.0)

    # Точный расчёт списания
    credits_needed, _ = calculate_tiered_credits(
        resolution="720p" if quality == "draft" else quality,
        duration_sec=duration,
        is_draft=(quality == "draft"),
    )

    # Атомарное создание VideoJob + списание кредитов по матрице
    def _create_job_and_spend():
        with transaction.atomic():
            user = profile.user
            cur_balance = Decimal(str(get_balance(user)))
            if cur_balance < credits_needed:
                raise InsufficientBalanceError(cur_balance, credits_needed)

            credit_base_rate = getattr(settings, "CREDIT_BASE_REVENUE_RUB", Decimal("249.00"))
            revenue_rub = (credits_needed * credit_base_rate).quantize(Decimal("0.01"))

            job = VideoJob.objects.create(
                user=user,
                telegram_chat_id=callback.message.chat.id,
                telegram_message_id=callback.message.message_id,
                status=VideoJob.Status.PENDING,
                preset_category=data.get("chosen_category", "custom"),
                preset_id=data.get("preset_id"),
                user_prompt=data.get("user_prompt", ""),
                final_prompt=data.get("final_prompt", ""),
                video_purpose=data.get("video_purpose", "other"),
                duration_sec=duration,
                resolution="720p" if quality == "draft" else quality,
                credits_cost=credits_needed,
                polza_model=getattr(settings, "POLZA_VIDEO_MODEL", "google/gemini-omni-1.1-flash"),
                cost_rub=revenue_rub,
            )

            with open(temp_file_path, "rb") as f:
                uploaded = UploadedVideo.objects.create(
                    video_job=job,
                    file=ContentFile(f.read(), name=data.get("filename", "video.mp4")),
                    duration_sec=duration,
                    size_bytes=data.get("size_bytes", 0),
                    width=data.get("width"),
                    height=data.get("height"),
                    content_type=data.get("content_type", "video/mp4"),
                )

            # Списываем кредиты по матрице
            spend_generation_for_video(user, video_job=job, amount=credits_needed)
            return job

    try:
        job = await sync_to_async(_create_job_and_spend)()
    except InsufficientBalanceError:
        cr_needed_str = f"{credits_needed:.1f}" if credits_needed % 1 != 0 else f"{int(credits_needed)}"
        await callback.message.edit_text(
            f"⚠️ <b>Недостаточно кредитов на балансе!</b>\n\n"
            f"Для запуска в выбранном режиме требуется: <b>{cr_needed_str} кр.</b>\n\n"
            "Пожалуйста, пополните баланс для продолжения.",
            reply_markup=keyboards.balance_keyboard(),
        )
        await finish_flow(state)
        return

    # Запускаем Celery-таску
    process_video_job_task.delay(str(job.id))

    # Удаляем локальный временный файл
    try:
        os.remove(temp_file_path)
    except Exception:
        pass

    await finish_flow(state)

    text = (
        "⏳ <b>Видео принято в обработку!</b>\n\n"
        "Нейросеть Gemini Omni выполняет генерацию ролика. Обычно это занимает от 1 до 3 минут.\n\n"
        "Мы сообщим вам в этот чат, как только готовый результат будет доставлен! 🔔\n\n"
        "Пока видео обрабатывается, вы можете посмотреть статус в разделе «Мои обработки»."
    )
    await callback.message.edit_text(text, reply_markup=keyboards.back_to_menu_keyboard())
    await callback.answer()


@router.callback_query(VideoFlow.confirm, F.data == "video:change_prompt")
async def change_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(VideoFlow.choosing_category)
    await render(
        callback,
        state,
        "Выберите другую задачу или введите новую инструкцию:",
        keyboards.video_category_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "video:cancel")
async def cancel_video_flow(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    temp_file = data.get("temp_file_path")
    if temp_file and os.path.exists(temp_file):
        try:
            os.remove(temp_file)
        except Exception:
            pass
    await finish_flow(state)
    await callback.message.edit_text("Обработка отменена.", reply_markup=keyboards.main_menu())
    await callback.answer()


# -----------------------------------------------------------------------------
# 6. Просмотр примеров «до/после» (ТЗ раздел 3.2)
# -----------------------------------------------------------------------------

@router.callback_query(F.data == keyboards.MENU_VIDEO_EXAMPLES_CB)
async def show_examples(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await _current_profile(callback.from_user)
    await sync_to_async(track_event)("examples_viewed", user=profile.user)

    text = (
        "👀 <b>Примеры работы нейросети VideoLab AI</b>\n\n"
        "<b>1. Атмосфера и освещение (Golden Hour):</b>\n"
        "• Исходник: обычный дневной ролик на улице под пасмурным небом.\n"
        "• Результат: мягкий закатный золотой свет, теплые блики, кинематографичные тени.\n\n"
        "<b>2. Визуальные эффекты (Neon & Mist):</b>\n"
        "• Исходник: видео танца в студии.\n"
        "• Результат: добавлены неоновые световые акценты и легкий дым, движение сохранено 1:1.\n\n"
        "<b>3. Стилизация сцены (35mm Kodak Film):</b>\n"
        "• Исходник: съемка на смартфон.\n"
        "• Результат: аутентичная пленочная цветокоррекция, винтажное зерно и глубина кадра.\n\n"
        "💡 <i>Все обработки выполняются автоматически через модель Gemini Omni без ручного монтажа.</i>"
    )
    await callback.message.edit_text(text, reply_markup=keyboards.video_examples_keyboard())
    await callback.answer()


# -----------------------------------------------------------------------------
# 7. История обработок «Мои обработки» (ТЗ раздел 3.8)
# -----------------------------------------------------------------------------

async def _render_history(user: User):
    profile = await _current_profile(user)
    jobs = await sync_to_async(list)(
        VideoJob.objects.filter(user=profile.user).order_by("-created_at")[:10]
    )

    if not jobs:
        text = (
            "📁 <b>У вас пока нет обработанных видео</b>\n\n"
            "Нажмите «🎬 Изменить видео», чтобы запустить первую обработку!"
        )
        return text, keyboards.video_examples_keyboard()

    text = "📁 <b>Ваши последние обработки видео:</b>\n\nВыберите заказ для подробностей:"
    return text, keyboards.video_history_keyboard(jobs)


@router.message(Command("history"))
async def show_video_history_msg(message: Message, state: FSMContext) -> None:
    text, kb = await _render_history(message.from_user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.in_({keyboards.MENU_VIDEO_HISTORY_CB, keyboards.MENU_HISTORY_CB}))
async def show_video_history(callback: CallbackQuery, state: FSMContext) -> None:
    text, kb = await _render_history(callback.from_user)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("video_job:"))
async def show_job_detail(callback: CallbackQuery) -> None:
    job_id = callback.data.split(":", 1)[1]
    job = await sync_to_async(
        VideoJob.objects.select_related("result").filter(id=job_id).first
    )()

    if not job:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    status_name = job.get_status_display()
    duration_str = f"{job.duration_sec:.1f} сек" if job.duration_sec else "до 10 сек"

    text = (
        f"🎬 <b>Заказ #{str(job.id)[:8]}</b>\n\n"
        f"📅 <b>Дата:</b> {job.created_at:%d.%m.%Y %H:%M}\n"
        f"📊 <b>Статус:</b> {status_name}\n"
        f"⏱ <b>Длительность:</b> {duration_str}\n"
        f"📝 <b>Инструкция:</b> <i>{job.user_prompt}</i>\n"
    )

    if job.status == VideoJob.Status.DONE and hasattr(job, "result"):
        text += (
            f"\n📥 <a href=\"{job.result.file_url}\"><b>Открыть готовое видео</b></a>\n"
        )
    elif job.status == VideoJob.Status.FAILED:
        text += f"\n⚠️ <b>Ошибка:</b> {job.error_message or 'Сбой сервиса'}\n<i>(Кредит был возвращён)</i>\n"

    rows = []
    if job.status == VideoJob.Status.DONE:
        rows.append([
            {"text": "🔄 Повторить с этим видео", "callback_data": f"video:retry:{job.id}"}
        ])
    rows.append([{"text": "📁 К списку заказов", "callback_data": keyboards.MENU_VIDEO_HISTORY_CB}])
    rows.append([{"text": "⬅️ В главное меню", "callback_data": keyboards.MENU_HOME_CB}])

    import json
    from aiogram.types import InlineKeyboardMarkup
    markup = InlineKeyboardMarkup.model_validate({"inline_keyboard": rows})

    await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=False)
    await callback.answer()


# -----------------------------------------------------------------------------
# 8. Оценка результата и обратная связь (ТЗ 3.6, 3.7)
# -----------------------------------------------------------------------------

@router.callback_query(F.data.startswith("video:rate:"))
async def handle_video_rating(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if len(parts) < 4:
        return
    job_id = parts[2]
    rating = parts[3]  # 'good' | 'bad'

    job = await sync_to_async(VideoJob.objects.filter(id=job_id).first)()
    if not job:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    job.user_rating = "positive" if rating == "good" else "negative"
    await sync_to_async(job.save)(update_fields=["user_rating", "updated_at"])

    profile = await _current_profile(callback.from_user)
    await sync_to_async(track_event)(
        "result_rated",
        user=profile.user,
        video_job=job,
        rating=job.user_rating,
    )

    if rating == "good":
        await callback.message.answer(
            "🙌 <b>Спасибо за высокую оценку!</b>\n\nРады, что результат подошел. Вы можете обработать следующий ролик через меню:",
            reply_markup=keyboards.video_examples_keyboard(),
        )
        await callback.answer("Оценка сохранена!")
    else:
        await state.set_state(VideoFlow.waiting_feedback)
        await state.update_data(rating_job_id=str(job.id))
        await callback.message.answer(
            "💬 <b>Что именно не подошло в результате?</b>\n\n"
            "Напишите коротко (например: <i>«слишком изменилось лицо»</i>, <i>«мало эффектов»</i> или <i>«свет слишком темный»</i>). "
            "Это поможет улучшить точность модели:",
            reply_markup=keyboards.back_to_menu_keyboard(),
        )
        await callback.answer()


@router.message(VideoFlow.waiting_feedback, F.text)
async def receive_feedback_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    job_id = data.get("rating_job_id")

    if job_id:
        job = await sync_to_async(VideoJob.objects.filter(id=job_id).first)()
        if job:
            job.user_feedback = message.text.strip()
            await sync_to_async(job.save)(update_fields=["user_feedback", "updated_at"])

    await finish_flow(state)
    await message.answer(
        "🤝 <b>Спасибо за отзыв!</b> Мы передали его разработчикам и уже настраиваем модель.",
        reply_markup=keyboards.main_menu(),
    )


# -----------------------------------------------------------------------------
# 9. Повторная обработка (Retry)
# -----------------------------------------------------------------------------

@router.callback_query(F.data.startswith("video:retry:"))
async def retry_video_job(callback: CallbackQuery, state: FSMContext) -> None:
    job_id = callback.data.split(":", 2)[2]
    old_job = await sync_to_async(
        VideoJob.objects.select_related("uploaded_video").filter(id=job_id).first
    )()

    if not old_job or not hasattr(old_job, "uploaded_video") or not old_job.uploaded_video.file:
        await callback.answer("Исходное видео этого заказа уже удалено по сроку хранения.", show_alert=True)
        return

    profile = await _current_profile(callback.from_user)
    await sync_to_async(track_event)("retry_requested", user=profile.user, video_job=old_job)

    # Копируем путь к файлу в сессию
    uploaded = old_job.uploaded_video
    await state.update_data(
        temp_file_path=uploaded.file.path,
        filename=os.path.basename(uploaded.file.name),
        duration_sec=uploaded.duration_sec,
        size_bytes=uploaded.size_bytes,
        width=uploaded.width,
        height=uploaded.height,
        content_type=uploaded.content_type,
    )

    await state.set_state(VideoFlow.choosing_category)
    await callback.message.answer(
        "🔄 <b>Новый вариант для того же видео</b>\n\n"
        "Выберите желаемое изменение:",
        reply_markup=keyboards.video_category_keyboard(),
    )
    await callback.answer()
