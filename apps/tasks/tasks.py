from datetime import date, timedelta
from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone

from apps.core.whatsapp_service import resolve_target_whatsapp, send_whatsapp_text
from .models import Task, TaskReminder


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


@shared_task(name="apps.tasks.tasks.dispatch_due_reminders")
def dispatch_due_reminders() -> dict:
    """
    Task periódica do Celery Beat (a cada 60s):
    1. Desativa lembretes de tarefas que já foram concluídas.
    2. Busca `TaskReminder` ativos com `remind_at <= timezone.now()` e `task.status != DONE`.
    3. Usa `select_for_update(skip_locked=True)` quando suportado pelo banco (PostgreSQL).
    4. Aplica janela de silêncio (22:00 às 06:59) para lembretes recorrentes (`repeat_interval_minutes`),
       adiando para 07:00 sem consumir `attempts_sent`.
    5. Envia mensagem via Evolution API v2 (`send_whatsapp_text`) e reagenda ou finaliza o lembrete.
    """
    now = timezone.now()
    local_now = timezone.localtime(now)

    # Garante cancelamento de lembretes órfãos de tarefas já concluídas
    TaskReminder.objects.filter(is_active=True, task__status=Task.Status.DONE).update(is_active=False)

    sent_count = 0
    rescheduled_count = 0
    deferred_quiet_count = 0
    failed_count = 0

    with transaction.atomic():
        base_qs = (
            TaskReminder.objects.filter(is_active=True, remind_at__lte=now)
            .exclude(task__status=Task.Status.DONE)
            .select_related("task", "task__workspace", "task__project", "task__owner", "task__assignee")
            .order_by("remind_at", "pk")
        )
        if getattr(connection.features, "has_select_for_update_skip_locked", False):
            due_reminders = list(base_qs.select_for_update(skip_locked=True))
        elif getattr(connection.features, "has_select_for_update", False):
            due_reminders = list(base_qs.select_for_update())
        else:
            due_reminders = list(base_qs)

        for reminder in due_reminders:
            task = reminder.task
            if task.status == Task.Status.DONE:
                reminder.is_active = False
                reminder.save(update_fields=["is_active"])
                continue

            # Janela de Silêncio (22:00 às 06:59) apenas para lembretes recorrentes
            if reminder.repeat_interval_minutes and (local_now.hour >= 22 or local_now.hour < 7):
                if local_now.hour >= 22:
                    next_morning = (local_now + timedelta(days=1)).replace(
                        hour=7, minute=0, second=0, microsecond=0
                    )
                else:
                    next_morning = local_now.replace(
                        hour=7, minute=0, second=0, microsecond=0
                    )
                reminder.remind_at = next_morning
                reminder.save(update_fields=["remind_at"])
                deferred_quiet_count += 1
                continue

            target_number = reminder.target_whatsapp or resolve_target_whatsapp(task)
            if not target_number:
                reminder.last_error = "Número de WhatsApp de destino não configurado."
                reminder.save(update_fields=["last_error"])
                failed_count += 1
                continue

            ws_slug = task.workspace.slug if task.workspace else "geral"
            next_attempt_num = reminder.attempts_sent + 1
            msg_lines = [
                "⏰ *Lembrete Command Center*",
                f"• *Tarefa:* {task.title}",
                f"• *Frente:* #{ws_slug} | *Score:* {task.priority_score}/25",
            ]
            if reminder.custom_message:
                msg_lines.append(f"• *Nota:* {reminder.custom_message}")
            if reminder.repeat_interval_minutes:
                remaining = max(0, int(reminder.max_repeats) - next_attempt_num)
                msg_lines.append(
                    f"• *Recorrência:* a cada {reminder.repeat_interval_minutes} min "
                    f"({next_attempt_num}/{reminder.max_repeats} — restam {remaining})"
                )

            send_res = send_whatsapp_text(target_number, "\n".join(msg_lines))
            if send_res.get("ok"):
                reminder.attempts_sent = next_attempt_num
                reminder.last_sent_at = now
                reminder.last_error = ""
                if not reminder.target_whatsapp:
                    reminder.target_whatsapp = target_number

                if reminder.repeat_interval_minutes and reminder.attempts_sent < reminder.max_repeats:
                    reminder.remind_at = now + timedelta(minutes=int(reminder.repeat_interval_minutes))
                    reminder.is_active = True
                    reminder.is_sent = False
                    rescheduled_count += 1
                else:
                    reminder.is_sent = True
                    reminder.is_active = False

                reminder.save(
                    update_fields=[
                        "attempts_sent",
                        "last_sent_at",
                        "last_error",
                        "target_whatsapp",
                        "remind_at",
                        "is_active",
                        "is_sent",
                    ]
                )
                sent_count += 1
            else:
                reminder.last_error = str(
                    send_res.get("error") or send_res.get("reason") or "Falha no envio WhatsApp"
                )
                reminder.save(update_fields=["last_error"])
                failed_count += 1

    return {
        "processed": len(due_reminders),
        "sent": sent_count,
        "rescheduled": rescheduled_count,
        "deferred_quiet_hours": deferred_quiet_count,
        "failed": failed_count,
    }

