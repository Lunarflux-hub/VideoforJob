import io
import struct
from django.test import SimpleTestCase
from apps.videos.services.video_validator import (
    validate_video_file,
    _parse_mp4_duration_and_timescale,
)


def _make_dummy_mp4(duration_sec: float = 5.0, timescale: int = 1000) -> bytes:
    """Генерирует минимальный валидный заголовок MP4 с атомом moov/mvhd."""
    duration_val = int(duration_sec * timescale)
    # mvhd payload: version 0 (1 byte), flags (3 bytes), creation (4), mod (4), timescale (4), duration (4), rest (12 zeros)
    mvhd_payload = struct.pack(">B3sIIII12s", 0, b"\x00\x00\x00", 0, 0, timescale, duration_val, b"\x00" * 12)
    mvhd_box = struct.pack(">I4s", len(mvhd_payload) + 8, b"mvhd") + mvhd_payload
    moov_box = struct.pack(">I4s", len(mvhd_box) + 8, b"moov") + mvhd_box
    # ftyp box
    ftyp_payload = b"isom\x00\x00\x02\x00isomiso2mp41"
    ftyp_box = struct.pack(">I4s", len(ftyp_payload) + 8, b"ftyp") + ftyp_payload
    return ftyp_box + moov_box


class VideoValidatorTests(SimpleTestCase):
    def test_valid_mp4(self):
        data = _make_dummy_mp4(duration_sec=6.0)
        res = validate_video_file(data, filename="clip.mp4")
        self.assertTrue(res.is_valid)
        self.assertAlmostEqual(res.duration_sec, 6.0, places=1)
        self.assertEqual(res.content_type, "video/mp4")

    def test_duration_exceeds_limit(self):
        data = _make_dummy_mp4(duration_sec=15.0)
        res = validate_video_file(data, filename="long_clip.mp4")
        self.assertFalse(res.is_valid)
        self.assertIn("превышает максимум", res.error_message)

    def test_unsupported_format(self):
        res = validate_video_file(b"some content", filename="doc.pdf")
        self.assertFalse(res.is_valid)
        self.assertIn("Неподдерживаемый формат", res.error_message)

    def test_empty_file(self):
        res = validate_video_file(b"", filename="empty.mp4")
        self.assertFalse(res.is_valid)
        self.assertIn("пустой", res.error_message)

    def test_reported_metadata_used_if_present(self):
        data = b"\x00" * 1024
        res = validate_video_file(
            data,
            filename="telegram_clip.mov",
            reported_duration=8.5,
            reported_size=1024,
            reported_width=1080,
            reported_height=1920,
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.duration_sec, 8.5)
        self.assertEqual(res.width, 1080)
        self.assertEqual(res.height, 1920)
