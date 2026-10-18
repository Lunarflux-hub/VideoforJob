from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.billing.models import GenerationLedgerEntry, UserBalance
from apps.billing.services import (
    InsufficientBalanceError,
    credit_generations,
    get_balance,
    refund_generation,
    spend_generation_for_video,
)
from apps.videos.models import VideoJob

User = get_user_model()


class VideoBillingIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="test_user")
        # Стартовый баланс: 0
        self.assertEqual(get_balance(self.user), 0)

    def test_spend_generation_insufficient_balance(self):
        job = VideoJob.objects.create(user=self.user, user_prompt="Test")
        with self.assertRaises(InsufficientBalanceError):
            spend_generation_for_video(self.user, video_job=job, amount=1)

    def test_spend_and_refund_lifecycle(self):
        # 1. Пополняем на 2 кредита
        credit_generations(self.user, amount=2)
        self.assertEqual(get_balance(self.user), 2)

        # 2. Создаем задачу и списываем 1 кредит
        job = VideoJob.objects.create(user=self.user, user_prompt="Sunset lighting")
        spend_generation_for_video(self.user, video_job=job, amount=1)

        self.assertEqual(get_balance(self.user), 1)
        spend_entry = GenerationLedgerEntry.objects.filter(user=self.user, video_job=job).first()
        self.assertIsNotNone(spend_entry)
        self.assertEqual(spend_entry.kind, GenerationLedgerEntry.Kind.SPEND)
        self.assertEqual(spend_entry.amount, -1)
        self.assertEqual(spend_entry.balance_after, 1)

        # 3. Возврат кредита при технической ошибке (ТЗ 3.7)
        refund_generation(self.user, video_job=job, amount=1, reason="Polza timeout")
        self.assertEqual(get_balance(self.user), 2)

        refund_entry = GenerationLedgerEntry.objects.filter(
            user=self.user, video_job=job, kind=GenerationLedgerEntry.Kind.REFUND
        ).first()
        self.assertIsNotNone(refund_entry)
        self.assertEqual(refund_entry.amount, 1)
        self.assertEqual(refund_entry.balance_after, 2)

    def test_fractional_tiered_billing_lifecycle(self):
        from decimal import Decimal
        # 1. Пополняем на 10.0 кредитов
        credit_generations(self.user, amount=Decimal("10.0"))
        self.assertEqual(get_balance(self.user), Decimal("10.0"))

        # 2. Создаем тяжелую 4K задачу (2.0 кредита)
        job_4k = VideoJob.objects.create(
            user=self.user,
            user_prompt="4K cinematic commercial",
            resolution="4k",
            credits_cost=Decimal("2.0"),
        )
        spend_generation_for_video(self.user, video_job=job_4k, amount=Decimal("2.0"))
        self.assertEqual(get_balance(self.user), Decimal("8.0"))

        # 3. Создаем черновую задачу (0.5 кредита)
        job_draft = VideoJob.objects.create(
            user=self.user,
            user_prompt="Draft preview",
            resolution="720p",
            credits_cost=Decimal("0.5"),
        )
        spend_generation_for_video(self.user, video_job=job_draft, amount=Decimal("0.5"))
        self.assertEqual(get_balance(self.user), Decimal("7.5"))

        # 4. Возврат 4K задачи при ошибке (возвращается ровно 2.0 кредита)
        refund_generation(self.user, video_job=job_4k, amount=job_4k.credits_cost, reason="GPU timeout")
        self.assertEqual(get_balance(self.user), Decimal("9.5"))
