from decimal import Decimal
from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from apps.billing.services import credit_generations, get_balance, spend_generation_for_video
from apps.videos.models import UploadedVideo, VideoAnalyticsEvent, VideoJob
from apps.videos.services.polza_video import PolzaVideoError, VideoGenerationResult
from apps.videos.tasks import process_video_job_task

User = get_user_model()


class VideoJobTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="task_user")
        credit_generations(self.user, 5)

        self.job = VideoJob.objects.create(
            user=self.user,
            user_prompt="Golden sunset lighting",
            final_prompt="Golden sunset lighting prompt",
            polza_model="google/gemini-omni-1.1-flash",
            duration_sec=6.0,
        )
        self.uploaded = UploadedVideo.objects.create(
            video_job=self.job,
            file=ContentFile(b"fake video content", name="test.mp4"),
            duration_sec=6.0,
            size_bytes=100,
            polza_file_url="https://cdn.polza.ai/files/source.mp4",
        )

    @patch("apps.videos.tasks.notify_video_job_done")
    @patch("apps.videos.tasks.upload_video_from_url")
    @patch("apps.videos.tasks.process_video_edit")
    def test_process_video_job_success(self, mock_process, mock_upload, mock_notify):
        mock_process.return_value = VideoGenerationResult(
            video_url="https://cdn.polza.ai/results/out.mp4",
            media_id="gen_success_1",
            raw_response={"status": "completed"},
        )
        mock_upload.return_value = ("video_results/key.mp4", "https://s3.storage.net/key.mp4")

        spend_generation_for_video(self.user, video_job=self.job, amount=1)
        self.assertEqual(get_balance(self.user), 4)

        process_video_job_task(str(self.job.id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJob.Status.DONE)
        self.assertEqual(self.job.polza_media_id, "gen_success_1")
        self.assertIsNotNone(self.job.result)
        self.assertEqual(self.job.result.file_url, "https://s3.storage.net/key.mp4")
        self.assertGreaterEqual(self.job.execution_time_sec, 0)
        mock_notify.assert_called_once_with(self.job)

        # Проверяем сохранение аналитики и финансовых полей
        self.assertEqual(self.job.polza_task_id, "gen_success_1")
        self.assertEqual(self.job.cost_source, VideoJob.CostSource.PRICING_ESTIMATE)
        self.assertFalse(self.job.cost_verified)
        self.assertEqual(self.job.contribution_profit_rub, self.job.gross_profit_rub)
        self.assertEqual(self.job.contribution_margin_percent, self.job.gross_margin_percent)

        events = list(VideoAnalyticsEvent.objects.filter(video_job=self.job).values_list("event_name", flat=True))
        self.assertIn("generation_started", events)
        self.assertIn("generation_succeeded", events)

    @patch("apps.videos.tasks.notify_video_job_failed")
    @patch("apps.videos.tasks.process_video_edit")
    def test_process_video_job_failure_auto_refunds_credit(self, mock_process, mock_notify):
        mock_process.side_effect = PolzaVideoError("Simulated upstream GPU timeout")

        # Списываем 1 кредит
        spend_generation_for_video(self.user, video_job=self.job, amount=1)
        self.assertEqual(get_balance(self.user), 4)

        process_video_job_task(str(self.job.id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJob.Status.FAILED)
        self.assertIn("Simulated upstream GPU timeout", self.job.error_message)

        # ТЗ 3.7: Кредит должен автоматически вернуться на баланс!
        self.assertEqual(get_balance(self.user), 5)
        mock_notify.assert_called_once_with(self.job, self.job.error_message)

        # Проблема № 4: Учёт убытка от сбоя (Failure COGS), не обнуляя расходы
        self.assertEqual(self.job.failure_reason_category, VideoJob.FailureCategory.TIMEOUT)
        self.assertEqual(self.job.refunded_credits, Decimal("1.0"))
        self.assertFalse(self.job.provider_refunded)
        self.assertGreater(self.job.failure_cogs_rub, Decimal("0.00"))
        # Маржинальная прибыль заказа при сбое отрицательна (равна прямому убытку)
        self.assertEqual(self.job.contribution_profit_rub, -self.job.failure_cogs_rub)

        # Проверяем сохранение аналитики сбоя и возврата
        events = list(VideoAnalyticsEvent.objects.filter(video_job=self.job).values_list("event_name", flat=True))
        self.assertIn("generation_started", events)
        self.assertIn("generation_failed", events)
        self.assertIn("credit_refunded", events)
