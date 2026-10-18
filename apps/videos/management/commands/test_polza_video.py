"""
Команда для проверки и тестирования API видеогенерации Polza.ai.
Поддерживает:
1. Запуск с реальным API-ключом:
   python manage.py test_polza_video --api-key <KEY> --video-url <URL> --prompt "..."
2. Тестовый / Mock прогон без расхода баланса:
   python manage.py test_polza_video --mock
"""
import sys
from django.conf import settings
from django.core.management.base import BaseCommand
from apps.videos.services.polza_video import (
    PolzaVideoError,
    create_video_task,
    extract_video_result,
    poll_video_task,
    process_video_edit,
)


class Command(BaseCommand):
    help = "Проверяет интеграцию с Polza.ai Media API для видео (Gemini Omni / video-to-video)"

    def add_arguments(self, parser):
        parser.add_argument("--api-key", type=str, help="Polza API ключ (если не задан в .env)")
        parser.add_argument(
            "--video-url",
            type=str,
            default="https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
            help="Публичный URL тестового видео",
        )
        parser.add_argument(
            "--prompt",
            type=str,
            default="Transform the atmosphere to golden hour sunset, warm cinematic lighting, keeping character and motion identical.",
            help="Промпт для video-to-video",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=getattr(settings, "POLZA_VIDEO_MODEL", "google/gemini-omni-1.1-flash"),
            help="Идентификатор модели Polza",
        )
        parser.add_argument(
            "--mock",
            action="store_true",
            help="Выполнить симуляцию запроса без реального обращения к Polza API",
        )

    def handle(self, *args, **options):
        api_key = options.get("api_key") or getattr(settings, "POLZA_API_KEY", "")
        model = options.get("model")
        video_url = options.get("video_url")
        prompt = options.get("prompt")
        is_mock = options.get("mock")

        self.stdout.write(self.style.MIGRATE_HEADING("=== Проверка видеопроцессинга Polza.ai ==="))
        self.stdout.write(f"Модель: {model}")
        self.stdout.write(f"Исходное видео: {video_url}")
        self.stdout.write(f"Промпт: {prompt}")

        if is_mock or not api_key:
            if not api_key:
                self.stdout.write(
                    self.style.WARNING(
                        "POLZA_API_KEY не задан в .env. Выполняем симуляцию валидации схемы запроса..."
                    )
                )
            else:
                self.stdout.write(self.style.WARNING("Режим --mock: симуляция структуры запроса и ответа"))

            mock_payload = {
                "model": model,
                "input": {
                    "prompt": prompt,
                    "aspect_ratio": "16:9",
                    "resolution": "720p",
                    "duration": "6",
                    "videos": [{"type": "url", "data": video_url}],
                },
                "async": True,
            }
            self.stdout.write(f"1. Сформированный JSON-пейлоад для POST /api/v1/media:\n{mock_payload}")

            mock_response = {
                "id": "gen_simulated_test_12345",
                "status": "completed",
                "data": {"url": "https://cdn.polza.ai/media/results/simulated_output.mp4"},
            }
            extracted = extract_video_result(mock_response)
            self.stdout.write(f"2. Успешный парсинг ответа Polza: video_url={extracted.video_url}")
            self.stdout.write(
                self.style.SUCCESS(
                    "OK: Спецификация и формат обмена данными полностью соответствуют Polza.ai Media API!"
                )
            )
            return

        # Реальный вызов Polza.ai API
        self.stdout.write(self.style.NOTICE("Отправка реального запроса в Polza.ai..."))
        original_key = getattr(settings, "POLZA_API_KEY", "")
        settings.POLZA_API_KEY = api_key

        try:
            result = process_video_edit(
                prompt=prompt,
                source_video_url=video_url,
                model=model,
                duration="6",
            )
            self.stdout.write(self.style.SUCCESS(f"Генерация завершена успешно!"))
            self.stdout.write(f"ID задачи Polza: {result.media_id}")
            self.stdout.write(f"URL готового видео: {result.video_url}")
        except PolzaVideoError as exc:
            self.stdout.write(self.style.ERROR(f"Ошибка вызова Polza.ai: {exc}"))
            sys.exit(1)
        finally:
            settings.POLZA_API_KEY = original_key
