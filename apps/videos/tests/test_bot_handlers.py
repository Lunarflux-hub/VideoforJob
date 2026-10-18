from unittest.mock import AsyncMock, MagicMock
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import TestCase
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from apps.billing.services import credit_generations, get_balance
from apps.telegram_bot.bot.handlers import start, video
from apps.telegram_bot.bot.services import get_or_create_profile
from apps.telegram_bot.bot.states import VideoFlow
from apps.telegram_bot.models import TelegramProfile

User = get_user_model()


class BotVideoHandlerTests(TestCase):
    def setUp(self):
        self.storage = MemoryStorage()

    async def _make_fsm_context(self, user_id: int, chat_id: int) -> FSMContext:
        key = StorageKey(bot_id=123456, chat_id=chat_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    async def test_start_creates_profile(self):
        user_mock = MagicMock()
        user_mock.id = 998877
        user_mock.username = "videotestuser"

        message = MagicMock()
        message.from_user = user_mock
        message.answer = AsyncMock()
        message.answer_photo = AsyncMock()

        state = await self._make_fsm_context(998877, 998877)
        await start.cmd_start(message, state)

        # Проверяем, что TelegramProfile и User созданы
        profile = await sync_to_async(
            lambda: TelegramProfile.objects.filter(telegram_id=998877).first()
        )()
        self.assertIsNotNone(profile)
        self.assertEqual(profile.telegram_username, "videotestuser")

    async def test_video_new_zero_balance_shows_topup(self):
        user_mock = MagicMock()
        user_mock.id = 112233
        user_mock.username = "pooruser"

        message = MagicMock()
        message.from_user = user_mock
        message.answer = AsyncMock()
        message.answer_photo = AsyncMock()

        state = await self._make_fsm_context(112233, 112233)
        await video.cmd_video(message, state)

        # Должен предложить пополнить баланс
        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        self.assertIn("Матрица списания кредитов", args[0])

    async def test_video_new_with_balance_prompts_purpose(self):
        user_mock = MagicMock()
        user_mock.id = 445566
        user_mock.username = "richuser"

        # Создаем профиль и начисляем 3 кредита через sync_to_async
        def _setup_user():
            p = get_or_create_profile(445566, "richuser")
            credit_generations(p.user, 3)
            return p

        await sync_to_async(_setup_user)()

        message = MagicMock()
        message.from_user = user_mock
        message.answer = AsyncMock()
        message.answer_photo = AsyncMock()

        state = await self._make_fsm_context(445566, 445566)
        await video.cmd_video(message, state)

        # Переходит в waiting_purpose
        current_state = await state.get_state()
        self.assertEqual(current_state, VideoFlow.waiting_purpose)
        message.answer.assert_called_once()
        args, _ = message.answer.call_args
        self.assertIn("Назначение видео", args[0])
