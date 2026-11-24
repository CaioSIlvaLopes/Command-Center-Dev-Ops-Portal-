from datetime import date, timedelta
from celery import shared_task
from .models import Task


@shared_task(name="apps.tasks.tasks.recalculate_daily_priorities")
def recalculate_daily_priorities() -> dict:
    """
    Rotina diária do Celery:
    1. Aumenta a urgência (urgency=5) de tarefas ativas com vencimento hoje ou atrasadas.
    2. Aumenta a urgência (urgency>=4) de tarefas que vencem amanhã.
    3. Preenche automaticamente vagas abertas no Top 3 com as tarefas de maior priority_score.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)

    active_qs = Task.objects.exclude(status=Task.Status.DONE)

    # 1. Tarefas vencendo hoje ou atrasadas recebem urgência máxima (5)
    overdue_or_today = active_qs.filter(due_date__lte=today, urgency__lt=5)
    escalated_today_count = 0
    for task in overdue_or_today:
        task.urgency = 5
        task.save()
        escalated_today_count += 1

    # 2. Tarefas vencendo amanhã recebem urgência mínima 4
    due_tomorrow = active_qs.filter(due_date=tomorrow, urgency__lt=4)
    escalated_tomorrow_count = 0
    for task in due_tomorrow:
        task.urgency = 4
        task.save()
        escalated_tomorrow_count += 1

    # 3. Completa os 3 slots do Top 3 separadamente por owner caso haja slots vagos
    promoted_count = 0
    distinct_owner_ids = list(active_qs.values_list("owner_id", flat=True).distinct())
    for owner_id in distinct_owner_ids:
        owner_active_qs = active_qs.filter(owner_id=owner_id)
        current_top_three_count = owner_active_qs.filter(is_top_three=True).count()
        if current_top_three_count < 3:
            slots_available = 3 - current_top_three_count
            candidates = (
                owner_active_qs.filter(is_top_three=False)
                .exclude(status=Task.Status.INBOX)
                .order_by("-priority_score", "due_date", "created_at")[:slots_available]
            )
            for candidate in candidates:
                candidate.is_top_three = True
                candidate.save()
                promoted_count += 1

    return {
        "date": str(today),
        "escalated_today_or_overdue": escalated_today_count,
        "escalated_tomorrow": escalated_tomorrow_count,
        "promoted_to_top_three": promoted_count,
    }

