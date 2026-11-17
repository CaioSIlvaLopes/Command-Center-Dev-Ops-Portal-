import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("command_center")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Rotina diária automática de recálculo de prioridades e prazos vencendo
app.conf.beat_schedule = {
    "recalculate-daily-priorities-every-morning": {
        "task": "apps.tasks.tasks.recalculate_daily_priorities",
        "schedule": crontab(hour=6, minute=0),
    },
}

