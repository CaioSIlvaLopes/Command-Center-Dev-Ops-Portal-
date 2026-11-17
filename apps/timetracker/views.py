from datetime import timedelta
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.tasks.models import Task
from apps.tasks.views import (
    build_dashboard_context,
    get_workspace_slug_from_request,
    render_dashboard_or_partial,
    render_drawer_with_oob,
)
from apps.timetracker.models import TimeLog


@require_POST
@transaction.atomic
def start_timer_view(request: HttpRequest, task_id: int) -> HttpResponse:
    """
    Inicia o cronômetro para a tarefa selecionada:
    1. Para qualquer outro TimeLog com is_running=True.
    2. Inicia um novo TimeLog para a tarefa clicada.
    3. Altera o status da tarefa para 'doing' (Em Execução).
    4. Atualiza o widget active_timer.html e a listagem de tarefas via HTMX.
    """
    task = get_object_or_404(Task, pk=task_id)
    workspace_slug = get_workspace_slug_from_request(request)

    running_logs = TimeLog.objects.select_for_update().filter(is_running=True)
    for running_log in running_logs:
        running_log.stop()
        if running_log.task_id != task.id and running_log.task.status == Task.Status.DOING:
            Task.objects.filter(pk=running_log.task_id).update(status=Task.Status.TODO)

    if task.status != Task.Status.DOING:
        task.status = Task.Status.DOING
        if task.completed_at is not None:
            task.completed_at = None
        task.save()

    TimeLog.objects.create(
        task=task,
        start_time=timezone.now(),
        is_running=True,
    )

    if request.headers.get("HX-Target") == "active-timer-container":
        context = build_dashboard_context(request, workspace_slug=workspace_slug)
        return render(request, "partials/active_timer.html", context)

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=f'Cronômetro iniciado em "{task.title}". Modo Foco ativado!',
        feedback_level="info",
    )


@require_POST
@transaction.atomic
def stop_timer_view(request: HttpRequest) -> HttpResponse:
    """Para o cronômetro atualmente ativo e contabiliza os minutos trabalhados."""
    workspace_slug = get_workspace_slug_from_request(request)
    running_logs = TimeLog.objects.select_for_update().filter(is_running=True).select_related("task")

    stopped_title = None
    logged_minutes = 0
    for log in running_logs:
        stopped_title = log.task.title
        log.stop()
        logged_minutes = log.duration_minutes

    if request.headers.get("HX-Target") == "active-timer-container":
        context = build_dashboard_context(request, workspace_slug=workspace_slug)
        return render(request, "partials/active_timer.html", context)

    msg = (
        f'Cronômetro pausado em "{stopped_title}" (+{logged_minutes} min registrados).'
        if stopped_title
        else "Nenhum cronômetro ativo no momento."
    )
    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
@transaction.atomic
def add_manual_time_view(request: HttpRequest, task_id: int) -> HttpResponse:
    """
    Adiciona tempo manual (+15m, +30m, +1h) diretamente em uma tarefa
    caso o usuário tenha esquecido de dar Play no cronômetro.
    """
    task = get_object_or_404(Task, pk=task_id)
    workspace_slug = get_workspace_slug_from_request(request)
    try:
        minutes = max(1, min(480, int(request.POST.get("minutes", 15))))
    except (TypeError, ValueError):
        minutes = 15

    now = timezone.now()
    TimeLog.objects.create(
        task=task,
        start_time=now - timedelta(minutes=minutes),
        end_time=now,
        duration_minutes=minutes,
        is_running=False,
    )

    msg = f'+{minutes} min adicionados manualmente em "{task.title}".'
    if request.POST.get("from_drawer") == "1":
        return render_drawer_with_oob(request, task=task, feedback_message=msg)

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
@transaction.atomic
def delete_timelog_view(request: HttpRequest, log_id: int) -> HttpResponse:
    """Exclui um registro de TimeLog incorreto na aba Pulso & Analytics."""
    log = get_object_or_404(TimeLog.objects.select_related("task"), pk=log_id)
    workspace_slug = get_workspace_slug_from_request(request)
    task_title = log.task.title
    mins = log.duration_minutes
    log.delete()

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=f'Registro de {mins} min em "{task_title}" removido.',
        feedback_level="info",
    )


@require_GET
def active_timer_partial_view(request: HttpRequest) -> HttpResponse:
    """Retorna apenas o widget `active_timer.html`."""
    context = build_dashboard_context(request)
    return render(request, "partials/active_timer.html", context)
