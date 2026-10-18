from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, override_settings
from apps.videos.services.polza_video import (
    PolzaVideoError,
    create_video_task,
    extract_video_result,
    poll_video_task,
    upload_video_to_polza_storage,
)


class PolzaVideoClientTests(SimpleTestCase):
    @override_settings(POLZA_API_KEY="test_key")
    @patch("requests.post")
    def test_create_video_task_payload(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "gen_123", "status": "pending"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        data = create_video_task(
            prompt="Make it sunset",
            video_source_url="https://example.com/source.mp4",
            aspect_ratio="16:9",
            resolution="720p",
            duration="6",
            model="google/gemini-omni-1.1-flash",
        )

        self.assertEqual(data["id"], "gen_123")
        self.assertEqual(data["status"], "pending")
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "google/gemini-omni-1.1-flash")
        self.assertEqual(payload["input"]["prompt"], "Make it sunset")
        self.assertEqual(payload["input"]["videos"], [{"type": "url", "data": "https://example.com/source.mp4"}])
        self.assertEqual(payload["async"], True)

    @override_settings(POLZA_API_KEY="")
    def test_missing_api_key_raises(self):
        with self.assertRaises(PolzaVideoError):
            create_video_task(prompt="test", video_source_url="https://test.com/v.mp4")

    def test_extract_video_result_string_url(self):
        payload = {
            "id": "gen_123",
            "status": "completed",
            "data": "https://cdn.polza.ai/results/gen_123.mp4",
        }
        res = extract_video_result(payload)
        self.assertEqual(res.video_url, "https://cdn.polza.ai/results/gen_123.mp4")
        self.assertEqual(res.media_id, "gen_123")

    def test_extract_video_result_dict_url(self):
        payload = {
            "id": "gen_456",
            "status": "completed",
            "data": {"url": "https://cdn.polza.ai/results/gen_456.mp4"},
        }
        res = extract_video_result(payload)
        self.assertEqual(res.video_url, "https://cdn.polza.ai/results/gen_456.mp4")

    def test_extract_video_result_dict_video_list(self):
        payload = {
            "id": "gen_789",
            "status": "completed",
            "data": {"videos": [{"url": "https://cdn.polza.ai/results/gen_789.mp4"}]},
        }
        res = extract_video_result(payload)
        self.assertEqual(res.video_url, "https://cdn.polza.ai/results/gen_789.mp4")

    def test_extract_video_result_failure_raises(self):
        payload = {
            "id": "gen_fail",
            "status": "failed",
            "error": {"message": "Invalid prompt content"},
        }
        with self.assertRaises(PolzaVideoError) as ctx:
            extract_video_result(payload)
        self.assertIn("Invalid prompt content", str(ctx.exception))

    @override_settings(POLZA_API_KEY="test_key")
    @patch("requests.post")
    def test_upload_to_polza_storage(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "file_123", "url": "https://cdn.polza.ai/files/file_123.mp4"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        url = upload_video_to_polza_storage(b"dummy_bytes", filename="clip.mp4")
        self.assertEqual(url, "https://cdn.polza.ai/files/file_123.mp4")
