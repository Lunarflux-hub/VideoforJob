import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("photostudio")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Периодическая уборка исходных фото пользователей (media/uploads/...).
# Без django-celery-beat: расписание фиксированное, воркер celery beat
# берёт его прямо отсюда (см. docker-compose.yml, сервис beat).
app.conf.beat_schedule = {
    "cleanup-expired-videos": {
        "task": "apps.videos.tasks.cleanup_expired_videos_task",
        "schedule": crontab(minute=0),  # раз в час
    },
}
