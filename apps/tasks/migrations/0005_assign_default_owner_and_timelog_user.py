from django.conf import settings
from django.db import migrations


def assign_default_superuser_to_tasks_and_timelogs(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    Task = apps.get_model("tasks", "Task")
    TimeLog = apps.get_model("timetracker", "TimeLog")

    first_superuser = User.objects.filter(is_superuser=True).order_by("id").first()
    if first_superuser:
        Task.objects.filter(owner__isnull=True).update(owner=first_superuser)
        TimeLog.objects.filter(user__isnull=True).update(user=first_superuser)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_teammember_can_access_vault_teammember_user"),
        ("tasks", "0004_task_owner"),
        ("timetracker", "0002_timelog_user"),
    ]

    operations = [
        migrations.RunPython(
            assign_default_superuser_to_tasks_and_timelogs,
            migrations.RunPython.noop,
        ),
    ]
