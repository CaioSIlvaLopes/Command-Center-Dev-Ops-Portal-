from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlparse

from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from apps.core.models import (
    CredentialEntry,
    Project,
    SystemIntegrationConfig,
    TeamMember,
    Workspace,
)
from apps.core.services import parse_and_create_task, process_gemini_or_whatsapp_sync
from apps.core.voice_service import (
    generate_subtasks_with_gemini,
    get_daily_audio_quota_status,
    normalize_spoken_command_ptbr,
    process_audio_with_gemini_free_tier,
)
from apps.integrations.models import WebhookEvent
from apps.integrations.tasks import (
    process_ecommerce_webhook,
    process_gemini_webhook,
    process_github_webhook,
    process_whatsapp_webhook,
)
from apps.tasks.models import SubTask, Task
from apps.tasks.tasks import recalculate_daily_priorities
from apps.timetracker.models import TimeLog


def format_minutes_hm(minutes: int) -> str:
    minutes = int(minutes or 0)
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m"


def _get_hx_current_url_param(request: HttpRequest, param_name: str) -> str:
    """Extrai um parâmetro da URL atual do navegador enviada pelo HTMX (`HX-Current-URL`)."""
    hx_url = (request.headers.get("HX-Current-URL") or "").strip()
    if not hx_url:
        return ""
    try:
        parsed = urlparse(hx_url)
        values = parse_qs(parsed.query).get(param_name)
        return (values[0] if values else "").strip()
    except Exception:
        return ""


def get_workspace_slug_from_request(request: HttpRequest) -> Optional[str]:
    slug = (
        request.GET.get("workspace")
        or request.POST.get("workspace")
        or request.headers.get("X-Workspace-Slug")
        or _get_hx_current_url_param(request, "workspace")
        or ""
    ).strip()
    if not slug or slug == "all":
        return None
    return slug


def get_project_id_from_request(request: HttpRequest) -> Optional[int]:
    raw_proj = (
        request.GET.get("project")
        or request.POST.get("project")
        or _get_hx_current_url_param(request, "project")
        or ""
    ).strip()
    if raw_proj.isdigit():
        return int(raw_proj)
    return None


def get_view_mode_from_request(request: HttpRequest) -> str:
    if request.path.startswith("/settings/"):
        default_mode = "settings"
    elif request.path.startswith("/vault/"):
        default_mode = "vault"
    else:
        default_mode = "focus"
    mode = (
        request.GET.get("view")
        or request.POST.get("view")
        or _get_hx_current_url_param(request, "view")
        or default_mode
    ).strip().lower()
    if mode not in ("focus", "list", "kanban", "analytics", "integrations", "styleguide", "settings", "vault"):
        return default_mode
    return mode


def build_dashboard_context(
    request: HttpRequest,
    workspace_slug: Optional[str] = None,
    feedback_message: Optional[str] = None,
    feedback_level: str = "info",
) -> dict:
    """Monta o contexto completo para o Dashboard, Kanban, Analytics, Settings e fragmentos HTMX."""
    if workspace_slug is None:
        workspace_slug = get_workspace_slug_from_request(request)

    project_id = get_project_id_from_request(request)
    view_mode = get_view_mode_from_request(request)

    workspaces = Workspace.objects.annotate(
        active_count=Count("tasks", filter=~Q(tasks__status=Task.Status.DONE), distinct=True),
        inbox_count=Count("tasks", filter=Q(tasks__status=Task.Status.INBOX), distinct=True),
        projects_count=Count("projects", distinct=True),
        tasks_count=Count("tasks", distinct=True),
    ).order_by("shortcut_key", "name")

    current_workspace = None
    if workspace_slug and workspace_slug != "all":
        current_workspace = Workspace.objects.filter(slug__iexact=workspace_slug).first()

    # Projetos para o Launchpad rápido
    projects_qs = Project.objects.filter(is_active=True).select_related("workspace").annotate(
        active_tasks_count=Count("tasks", filter=~Q(tasks__status=Task.Status.DONE))
    )
    if current_workspace:
        projects = list(projects_qs.filter(workspace=current_workspace))
    else:
        projects = list(projects_qs)

    current_project = None
    if project_id:
        current_project = Project.objects.filter(pk=project_id).select_related("workspace").first()

    base_tasks_qs = Task.objects.select_related(
        "workspace", "project", "assignee"
    ).prefetch_related("subtasks", "time_logs", "webhook_events")
    if current_workspace:
        base_tasks_qs = base_tasks_qs.filter(workspace=current_workspace)
    if current_project:
        base_tasks_qs = base_tasks_qs.filter(project=current_project)

    # Top 3 do Dia (apenas ativas, status != done)
    top_three_tasks = list(
        base_tasks_qs.filter(is_top_three=True)
        .exclude(status=Task.Status.DONE)
        .order_by("-priority_score", "created_at")[:3]
    )
    empty_slots_count = max(0, 3 - len(top_three_tasks))
    top_three_empty_slots = list(range(len(top_three_tasks) + 1, 4))[:empty_slots_count]

    # Fila por Turnos (Manhã, Tarde, Noite) - tarefas ativas fora da Inbox e fora do Top 3
    shift_base_qs = (
        base_tasks_qs.exclude(status__in=[Task.Status.DONE, Task.Status.INBOX])
        .filter(is_top_three=False)
        .order_by("-priority_score", "due_date", "-created_at")
    )
    morning_tasks = list(shift_base_qs.filter(shift=Task.Shift.MORNING))
    afternoon_tasks = list(shift_base_qs.filter(shift=Task.Shift.AFTERNOON))
    night_tasks = list(shift_base_qs.filter(shift=Task.Shift.NIGHT))

    # Inbox (Triagem de Webhooks e capturas rápidas pendentes de classificação)
    inbox_tasks = list(
        base_tasks_qs.filter(status=Task.Status.INBOX).order_by(
            "-priority_score", "-created_at"
        )
    )

    # Concluídas recentemente
    done_tasks = list(
        base_tasks_qs.filter(status=Task.Status.DONE).order_by("-completed_at", "-created_at")[:8]
    )

    # Colunas Kanban (para alternância instantânea Modo Foco <-> Quadro Kanban)
    kanban_todo = list(
        base_tasks_qs.filter(status=Task.Status.TODO).order_by("-is_top_three", "-priority_score")
    )
    kanban_doing = list(
        base_tasks_qs.filter(status=Task.Status.DOING).order_by("-is_top_three", "-priority_score")
    )

    # Phase 7.2: Visão em Lista (`/?view=list`) com Ordem de Chegada (FIFO / LIFO), Ordenação por Coluna e Filtros
    list_sort = (
        request.GET.get("sort")
        or request.POST.get("sort")
        or _get_hx_current_url_param(request, "sort")
        or "arrival_desc"
    ).strip().lower()
    if list_sort not in ("arrival_desc", "arrival_asc", "score_desc", "score_asc", "status", "title_asc", "title_desc"):
        list_sort = "arrival_desc"

    list_status_filter = (
        request.GET.get("status_filter")
        or request.POST.get("status_filter")
        or _get_hx_current_url_param(request, "status_filter")
        or "active"
    ).strip().lower()
    if list_status_filter not in ("active", "all", "inbox", "todo", "doing", "done"):
        list_status_filter = "active"

    list_search_query = (
        request.GET.get("q")
        or request.POST.get("q")
        or _get_hx_current_url_param(request, "q")
        or ""
    ).strip()

    list_qs = base_tasks_qs
    if list_status_filter == "active":
        list_qs = list_qs.exclude(status=Task.Status.DONE)
    elif list_status_filter in ("inbox", "todo", "doing", "done"):
        list_qs = list_qs.filter(status=list_status_filter)

    if list_search_query:
        clean_q = list_search_query.lstrip("#@").strip()
        list_qs = list_qs.filter(
            Q(title__icontains=list_search_query)
            | Q(notes__icontains=list_search_query)
            | Q(project__name__icontains=clean_q)
            | Q(workspace__slug__icontains=clean_q)
            | Q(workspace__name__icontains=clean_q)
            | Q(assigned_to__icontains=clean_q)
            | Q(assignee__name__icontains=clean_q)
            | Q(assignee__handle__icontains=clean_q)
        )

    sort_order_map = {
        "arrival_desc": ("-created_at", "-id"),
        "arrival_asc": ("created_at", "id"),
        "score_desc": ("-priority_score", "-created_at", "-id"),
        "score_asc": ("priority_score", "created_at", "id"),
        "status": ("status", "-priority_score", "-created_at"),
        "title_asc": ("title", "-created_at"),
        "title_desc": ("-title", "-created_at"),
    }
    list_tasks = list(list_qs.order_by(*sort_order_map[list_sort]))

    local_now = timezone.localtime(timezone.now())
    start_of_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    arrived_today_count = base_tasks_qs.filter(created_at__gte=start_of_today).count()
    inbox_count_for_ws = base_tasks_qs.filter(status=Task.Status.INBOX).count()
    doing_count_for_ws = base_tasks_qs.filter(status=Task.Status.DOING).count()
    todo_count_for_ws = base_tasks_qs.filter(status=Task.Status.TODO).count()

    # Timer Ativo Global
    active_timelog = (
        TimeLog.objects.filter(is_running=True)
        .select_related("task", "task__workspace", "task__project")
        .first()
    )
    running_task_id = active_timelog.task_id if active_timelog else None

    # Métricas Rápidas e Analytics (Pulso & Métricas)
    today = date.today()
    week_ago = today - timedelta(days=7)

    total_tasks_count = base_tasks_qs.count()
    total_active_count = base_tasks_qs.exclude(status=Task.Status.DONE).count()
    total_done_count = base_tasks_qs.filter(status=Task.Status.DONE).count()
    done_today_count = base_tasks_qs.filter(
        status=Task.Status.DONE, completed_at__date=today
    ).count()
    done_week_count = base_tasks_qs.filter(
        status=Task.Status.DONE, completed_at__date__gte=week_ago
    ).count()

    completion_rate_pct = (
        int(round((total_done_count / total_tasks_count) * 100))
        if total_tasks_count > 0
        else 0
    )

    total_focus_minutes = (
        TimeLog.objects.filter(start_time__date=today).aggregate(
            total=Sum("duration_minutes")
        )["total"]
        or 0
    )

    # Cálculo de horas registradas por Workspace (Pulso & Analytics)
    ws_analytics_qs = Workspace.objects.annotate(
        logged_minutes=Sum("tasks__time_logs__duration_minutes"),
        tasks_total=Count("tasks", distinct=True),
        tasks_done=Count("tasks", filter=Q(tasks__status=Task.Status.DONE), distinct=True),
    ).order_by("-logged_minutes", "shortcut_key")

    max_ws_minutes = max([w.logged_minutes or 0 for w in ws_analytics_qs] + [1])
    analytics_by_workspace = []
    for ws in ws_analytics_qs:
        mins = ws.logged_minutes or 0
        analytics_by_workspace.append(
            {
                "workspace": ws,
                "minutes": mins,
                "formatted_time": format_minutes_hm(mins),
                "tasks_total": ws.tasks_total,
                "tasks_done": ws.tasks_done,
                "bar_pct": int(round((mins / max_ws_minutes) * 100)) if mins > 0 else 4,
            }
        )

    # Cálculo de horas registradas por Projeto
    proj_analytics_qs = (
        Project.objects.filter(is_active=True)
        .select_related("workspace")
        .annotate(
            logged_minutes=Sum("tasks__time_logs__duration_minutes"),
            tasks_total=Count("tasks", distinct=True),
        )
        .order_by("-logged_minutes", "name")
    )
    if current_workspace:
        proj_analytics_qs = proj_analytics_qs.filter(workspace=current_workspace)

    max_proj_minutes = max([p.logged_minutes or 0 for p in proj_analytics_qs] + [1])
    analytics_by_project = []
    for proj in proj_analytics_qs:
        mins = proj.logged_minutes or 0
        analytics_by_project.append(
            {
                "project": proj,
                "minutes": mins,
                "formatted_time": format_minutes_hm(mins),
                "tasks_total": proj.tasks_total,
                "bar_pct": int(round((mins / max_proj_minutes) * 100)) if mins > 0 else 4,
            }
        )

    total_logged_all_minutes = (
        TimeLog.objects.aggregate(total=Sum("duration_minutes"))["total"] or 0
    )

    recent_timelogs_qs = TimeLog.objects.select_related(
        "task", "task__workspace", "task__project"
    ).order_by("-start_time")
    if current_workspace:
        recent_timelogs_qs = recent_timelogs_qs.filter(task__workspace=current_workspace)
    recent_timelogs = list(recent_timelogs_qs[:12])

    all_active_tasks = list(base_tasks_qs.exclude(status=Task.Status.DONE)[:20])

    recent_webhooks = list(
        WebhookEvent.objects.select_related(
            "created_task", "created_task__workspace", "created_task__project"
        ).order_by("-created_at")[:20]
    )

    team_members = list(TeamMember.objects.filter(is_active=True).order_by("handle"))
    all_team_members = list(
        TeamMember.objects.annotate(
            active_tasks_count=Count("tasks", filter=~Q(tasks__status=Task.Status.DONE)),
            total_tasks_count=Count("tasks"),
        ).order_by("-is_active", "handle")
    )
    all_projects_manage = list(
        Project.objects.select_related("workspace")
        .annotate(
            tasks_count=Count("tasks", distinct=True),
            active_tasks_count=Count(
                "tasks", filter=~Q(tasks__status=Task.Status.DONE), distinct=True
            ),
        )
        .order_by("-is_active", "workspace__shortcut_key", "name")
    )

    from django.conf import settings as django_settings
    base_url = request.build_absolute_uri("/").rstrip("/")
    audio_quota = get_daily_audio_quota_status()

    # Cofre de Credenciais & Acessos (Vault - /?view=vault)
    vault_category = (
        request.GET.get("vault_category")
        or request.POST.get("vault_category")
        or _get_hx_current_url_param(request, "vault_category")
        or "all"
    ).strip().lower()
    valid_vault_cats = {c[0] for c in CredentialEntry.Category.choices} | {"all"}
    if vault_category not in valid_vault_cats:
        vault_category = "all"

    vault_search_query = (
        request.GET.get("q")
        or request.POST.get("q")
        or _get_hx_current_url_param(request, "q")
        or ""
    ).strip()

    vault_layout = (
        request.GET.get("vault_layout")
        or request.POST.get("vault_layout")
        or _get_hx_current_url_param(request, "vault_layout")
        or "grid"
    ).strip().lower()
    if vault_layout not in ("grid", "list"):
        vault_layout = "grid"

    base_vault_qs = CredentialEntry.objects.select_related("workspace", "project")
    if current_workspace:
        base_vault_qs = base_vault_qs.filter(workspace=current_workspace)
    if current_project:
        base_vault_qs = base_vault_qs.filter(project=current_project)

    vault_total_count = base_vault_qs.count()
    vault_email_ecom_count = base_vault_qs.filter(
        category__in=[CredentialEntry.Category.EMAIL, CredentialEntry.Category.ECOMMERCE]
    ).count()
    vault_infra_api_count = base_vault_qs.filter(
        category__in=[
            CredentialEntry.Category.INFRA,
            CredentialEntry.Category.DOMAIN,
            CredentialEntry.Category.API,
        ]
    ).count()

    vault_qs = base_vault_qs
    if vault_category != "all":
        vault_qs = vault_qs.filter(category=vault_category)

    if vault_search_query:
        clean_vq = vault_search_query.lstrip("#").strip()
        vault_qs = vault_qs.filter(
            Q(title__icontains=vault_search_query)
            | Q(username_or_email__icontains=vault_search_query)
            | Q(notes__icontains=vault_search_query)
            | Q(recovery_info__icontains=vault_search_query)
            | Q(project__name__icontains=clean_vq)
            | Q(workspace__slug__icontains=clean_vq)
            | Q(workspace__name__icontains=clean_vq)
        )

    vault_credentials = list(vault_qs.order_by("workspace__shortcut_key", "category", "title"))

    return {
        "workspaces": workspaces,
        "all_projects": Project.objects.filter(is_active=True).select_related("workspace"),
        "all_projects_manage": all_projects_manage,
        "team_members": team_members,
        "all_team_members": all_team_members,
        "vault_credentials": vault_credentials,
        "vault_category": vault_category,
        "vault_search_query": vault_search_query,
        "vault_layout": vault_layout,
        "vault_categories": CredentialEntry.Category.choices,
        "vault_total_count": vault_total_count,
        "vault_email_ecom_count": vault_email_ecom_count,
        "vault_infra_api_count": vault_infra_api_count,
        "audio_quota": audio_quota,
        "integration_config": audio_quota["config"],
        "current_workspace": current_workspace,
        "current_workspace_slug": current_workspace.slug if current_workspace else "all",
        "current_project": current_project,
        "current_project_id": current_project.id if current_project else "",
        "view_mode": view_mode,
        "projects": projects,
        "top_three_tasks": top_three_tasks,
        "top_three_empty_slots": top_three_empty_slots,
        "morning_tasks": morning_tasks,
        "afternoon_tasks": afternoon_tasks,
        "night_tasks": night_tasks,
        "inbox_tasks": inbox_tasks,
        "done_tasks": done_tasks,
        "kanban_todo": kanban_todo,
        "kanban_doing": kanban_doing,
        "list_tasks": list_tasks,
        "list_sort": list_sort,
        "list_status_filter": list_status_filter,
        "list_search_query": list_search_query,
        "arrived_today_count": arrived_today_count,
        "inbox_count_for_ws": inbox_count_for_ws,
        "doing_count_for_ws": doing_count_for_ws,
        "todo_count_for_ws": todo_count_for_ws,
        "active_timelog": active_timelog,
        "running_task_id": running_task_id,
        "total_tasks_count": total_tasks_count,
        "total_active_count": total_active_count,
        "total_done_count": total_done_count,
        "done_today_count": done_today_count,
        "done_week_count": done_week_count,
        "completion_rate_pct": completion_rate_pct,
        "total_focus_minutes": total_focus_minutes,
        "total_focus_formatted": format_minutes_hm(total_focus_minutes),
        "total_logged_all_minutes": total_logged_all_minutes,
        "total_logged_all_formatted": format_minutes_hm(total_logged_all_minutes),
        "analytics_by_workspace": analytics_by_workspace,
        "analytics_by_project": analytics_by_project,
        "recent_timelogs": recent_timelogs,
        "recent_webhooks": recent_webhooks,
        "base_url": base_url,
        "webhook_secret_token": getattr(django_settings, "WEBHOOK_SECRET_TOKEN", "dev-secret-key"),
        "all_active_tasks": all_active_tasks,
        "feedback_message": feedback_message,
        "feedback_level": feedback_level,
        "today": today,
    }


def render_dashboard_or_partial(
    request: HttpRequest,
    workspace_slug: Optional[str] = None,
    feedback_message: Optional[str] = None,
    feedback_level: str = "info",
) -> HttpResponse:
    """
    Se a requisição vier via HTMX (`HX-Request`), retorna o fragmento composto
    com swap OOB para atualizar Header de Contexto, Cronômetro e Painel Principal.
    Caso contrário, renderiza a página completa `dashboard.html`.
    """
    context = build_dashboard_context(
        request,
        workspace_slug=workspace_slug,
        feedback_message=feedback_message,
        feedback_level=feedback_level,
    )
    if request.headers.get("HX-Request"):
        context["is_htmx_oob"] = True
        return render(request, "partials/dashboard_content.html", context)
    return render(request, "dashboard.html", context)


def render_drawer_with_oob(
    request: HttpRequest,
    task: Task,
    feedback_message: Optional[str] = None,
) -> HttpResponse:
    """Renderiza o Slide-Over Drawer da tarefa e atualiza o dashboard ao fundo via OOB."""
    workspace_slug = get_workspace_slug_from_request(request)
    context = build_dashboard_context(
        request,
        workspace_slug=workspace_slug,
        feedback_message=feedback_message,
    )
    task = (
        Task.objects.select_related("workspace", "project", "assignee")
        .prefetch_related("subtasks", "time_logs")
        .get(pk=task.pk)
    )
    context["drawer_task"] = task
    context["include_dashboard_oob"] = True
    return render(request, "partials/task_detail_drawer.html", context)


@require_GET
def dashboard_view(request: HttpRequest) -> HttpResponse:
    """View principal do Command Center (suporta `?workspace=slug`, `?project=id`, `?view=focus|kanban|analytics|settings`)."""
    return render_dashboard_or_partial(request)


@require_POST
def command_bar_create_view(request: HttpRequest) -> HttpResponse:
    """
    Endpoint HTMX para a barra de comando rápida (`Ctrl + K`).
    Usa `parse_and_create_task` para interpretar #slug, @nome, !hoje, !top3 e /turno.
    """
    raw_command = request.POST.get("command", "").strip()
    workspace_slug = get_workspace_slug_from_request(request)

    if not raw_command:
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message="Digite um comando válido na barra Ctrl + K.",
            feedback_level="warning",
        )

    task = parse_and_create_task(
        raw_text=raw_command,
        current_workspace_slug=workspace_slug,
    )

    msg = f'Tarefa "{task.title}" criada em #{task.workspace.slug} (Score: {task.priority_score}).'
    level = "info"
    if getattr(task, "_demoted_task_title", None):
        msg += f' Nota: "{task._demoted_task_title}" saiu do Top 3 para liberar o slot.'
        level = "warning"

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level=level,
    )


# --- FEATURE 1: GAVETA LATERAL DE DETALHES (SLIDE-OVER), NOTAS & SUBTAREFAS ---

@require_GET
def task_detail_drawer_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Carrega a gaveta lateral (Slide-Over) com detalhes, notas e checklist da tarefa."""
    task = get_object_or_404(
        Task.objects.select_related("workspace", "project", "assignee").prefetch_related(
            "subtasks", "time_logs"
        ),
        pk=pk,
    )
    workspace_slug = get_workspace_slug_from_request(request)
    context = build_dashboard_context(request, workspace_slug=workspace_slug)
    context["drawer_task"] = task
    context["include_dashboard_oob"] = False
    return render(request, "partials/task_detail_drawer.html", context)


@require_POST
def task_update_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Salva edições completas da tarefa vindas do Slide-Over Drawer."""
    task = get_object_or_404(Task, pk=pk)

    title = request.POST.get("title", "").strip()
    if title:
        task.title = title[:255]

    ws_id = request.POST.get("workspace_id", "").strip()
    if ws_id.isdigit():
        ws = Workspace.objects.filter(pk=int(ws_id)).first()
        if ws:
            task.workspace = ws

    proj_id = request.POST.get("project_id", "").strip()
    if proj_id == "":
        task.project = None
    elif proj_id.isdigit():
        proj = Project.objects.filter(pk=int(proj_id)).first()
        if proj:
            task.project = proj

    shift = request.POST.get("shift", "").strip()
    if shift in {c[0] for c in Task.Shift.choices}:
        task.shift = shift

    status = request.POST.get("status", "").strip()
    if status in {c[0] for c in Task.Status.choices}:
        task.status = status

    try:
        task.impact = max(1, min(5, int(request.POST.get("impact", task.impact))))
    except (TypeError, ValueError):
        pass

    try:
        task.urgency = max(1, min(5, int(request.POST.get("urgency", task.urgency))))
    except (TypeError, ValueError):
        pass

    if "assignee_id" in request.POST:
        raw_aid = request.POST.get("assignee_id", "").strip()
        if raw_aid in ("", "none", "0"):
            task.assignee = None
            task.assigned_to = ""
        elif raw_aid.isdigit():
            member = TeamMember.objects.filter(pk=int(raw_aid)).first()
            task.assignee = member
            task.assigned_to = f"@{member.handle}" if member else ""
    elif "assigned_to" in request.POST:
        raw_assigned = request.POST.get("assigned_to", "").strip()[:50]
        if not raw_assigned:
            task.assignee = None
            task.assigned_to = ""
        else:
            task.assigned_to = raw_assigned

    task.external_ref = request.POST.get("external_ref", task.external_ref).strip()[:120]
    task.notes = request.POST.get("notes", task.notes).strip()

    due_date_raw = request.POST.get("due_date", "").strip()
    if due_date_raw:
        try:
            task.due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    elif "due_date" in request.POST:
        task.due_date = None

    if "is_top_three" in request.POST:
        task.is_top_three = request.POST.get("is_top_three") in ("1", "true", "on")

    task.save()
    return render_drawer_with_oob(
        request,
        task=task,
        feedback_message=f'Alterações salvas em "{task.title}" (Score: {task.priority_score}).',
    )


@require_POST
def subtask_add_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Adiciona uma nova SubTask (item de checklist) à Task."""
    task = get_object_or_404(Task, pk=pk)
    subtask_title = request.POST.get("title", "").strip()
    if subtask_title:
        SubTask.objects.create(task=task, title=subtask_title[:200], is_done=False)
    return render_drawer_with_oob(request, task=task)


@require_POST
def subtask_toggle_view(request: HttpRequest, subtask_id: int) -> HttpResponse:
    """Marca/desmarca uma SubTask e atualiza a barra de progresso da tarefa."""
    subtask = get_object_or_404(SubTask.objects.select_related("task"), pk=subtask_id)
    subtask.is_done = not subtask.is_done
    subtask.save(update_fields=["is_done"])
    return render_drawer_with_oob(request, task=subtask.task)


@require_POST
def subtask_delete_view(request: HttpRequest, subtask_id: int) -> HttpResponse:
    """Exclui uma SubTask e atualiza a barra de progresso."""
    subtask = get_object_or_404(SubTask.objects.select_related("task"), pk=subtask_id)
    task = subtask.task
    subtask.delete()
    return render_drawer_with_oob(request, task=task)


# --- FEATURE 2: DRAG-AND-DROP INTERATIVO (SORTABLEJS) ---

@require_POST
def task_move_view(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Endpoint HTMX `POST /tasks/<int:pk>/move/` acionado pelo SortableJS ao arrastar
    um card entre colunas do Kanban ou entre os turnos (Manhã, Tarde, Noite).
    """
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)

    new_status = request.POST.get("status", "").strip()
    new_shift = request.POST.get("shift", "").strip()

    valid_statuses = {c[0] for c in Task.Status.choices}
    valid_shifts = {c[0] for c in Task.Shift.choices}

    changes = []
    if new_status in valid_statuses and task.status != new_status:
        task.status = new_status
        changes.append(f"Status: {task.get_status_display()}")

    if new_shift in valid_shifts:
        if task.shift != new_shift:
            task.shift = new_shift
            changes.append(f"Turno: {task.get_shift_display()}")
        if task.status == Task.Status.INBOX:
            task.status = Task.Status.TODO

    task.save()

    if task.status == Task.Status.DONE:
        for log in TimeLog.objects.filter(task=task, is_running=True):
            log.stop()

    summary = " | ".join(changes) if changes else "Posição atualizada"
    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=f'"{task.title}" movida ({summary}).',
        feedback_level="info",
    )


# --- AÇÕES RÁPIDAS NOS CARDS ---

@require_POST
def task_toggle_top_three_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Promove ou remove uma tarefa do Top 3 do Dia respeitando o limite de 3 slots."""
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)

    if task.status == Task.Status.DONE:
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message="Tarefas concluídas não podem ocupar slots do Top 3.",
            feedback_level="warning",
        )

    task.is_top_three = not task.is_top_three
    if task.is_top_three and task.status == Task.Status.INBOX:
        task.status = Task.Status.TODO
    task.save()

    if task.is_top_three:
        msg = f'"{task.title}" promovida para o Top 3 do Dia!'
        level = "info"
        if getattr(task, "_demoted_task_title", None):
            msg = (
                f'Limite de 3 atingido: "{task._demoted_task_title}" foi rebaixada '
                f'automaticamente para promover "{task.title}" ao Top 3.'
            )
            level = "warning"
    else:
        msg = f'"{task.title}" removida do Top 3 do Dia.'
        level = "info"

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level=level,
    )


@require_POST
def task_status_update_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Atualiza o status da tarefa (inbox, todo, doing, done) via HTMX."""
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)
    new_status = request.POST.get("status", "").strip()

    valid_statuses = {choice[0] for choice in Task.Status.choices}
    if new_status not in valid_statuses:
        new_status = Task.Status.TODO if task.status == Task.Status.DONE else Task.Status.DONE

    task.status = new_status
    task.save()

    if new_status == Task.Status.DONE:
        running_logs = TimeLog.objects.filter(task=task, is_running=True)
        for log in running_logs:
            log.stop()
        msg = f'Tarefa "{task.title}" marcada como concluída.'
    else:
        msg = f'Status de "{task.title}" atualizado para {task.get_status_display()}.'

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def task_shift_update_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Altera o turno de execução (morning, afternoon, night)."""
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)
    new_shift = request.POST.get("shift", "").strip()

    valid_shifts = {choice[0] for choice in Task.Shift.choices}
    if new_shift in valid_shifts:
        task.shift = new_shift
        if task.status == Task.Status.INBOX:
            task.status = Task.Status.TODO
        task.save()
        msg = f'"{task.title}" alocada no turno {task.get_shift_display()}.'
    else:
        msg = "Turno inválido."

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def task_triage_inbox_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Aprova item da Inbox enviando-o para a fila do turno selecionado (ou Top 3)."""
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)
    shift = request.POST.get("shift") or task.shift
    promote_top3 = request.POST.get("top3") == "1"

    if shift in {choice[0] for choice in Task.Shift.choices}:
        task.shift = shift
    task.status = Task.Status.TODO
    if promote_top3:
        task.is_top_three = True
        task.impact = 5
        task.urgency = 5
    task.save()

    msg = f'Item da Inbox "{task.title}" triado para {task.get_shift_display()}.'
    level = "info"
    if getattr(task, "_demoted_task_title", None):
        msg += f' ("{task._demoted_task_title}" saiu do Top 3).'
        level = "warning"

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level=level,
    )


@require_POST
def task_priority_update_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Atualiza impacto e urgência recalculando automaticamente o GeneratedField priority_score."""
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)

    try:
        impact = int(request.POST.get("impact", task.impact))
        urgency = int(request.POST.get("urgency", task.urgency))
        task.impact = max(1, min(5, impact))
        task.urgency = max(1, min(5, urgency))
        task.save()
        task.refresh_from_db()
        msg = f'Prioridade de "{task.title}" recalculada (Score: {task.priority_score}).'
    except (TypeError, ValueError):
        msg = "Valores de impacto/urgência inválidos."

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def task_delete_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove uma tarefa do Command Center via HTMX."""
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)
    title = task.title
    task.delete()
    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=f'Tarefa "{title}" removida.',
        feedback_level="info",
    )


@require_POST
def trigger_recalculate_priorities_view(request: HttpRequest) -> HttpResponse:
    """Aciona manualmente a rotina Celery de recálculo diário de prioridades via UI."""
    workspace_slug = get_workspace_slug_from_request(request)
    result = recalculate_daily_priorities()
    msg = (
        f"Recálculo diário concluído: {result['escalated_today_or_overdue']} urgências "
        f"escaladas e {result['promoted_to_top_three']} vagas preenchidas no Top 3."
    )
    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


# --- FEATURE 5 & PHASE 3: SIMULADOR DE WEBHOOKS E REPROCESSAMENTO DE EVENTOS ---

@require_POST
def simulate_webhook_view(request: HttpRequest) -> HttpResponse:
    """
    Simulador de Webhooks com 1 clique na Inbox e na aba Integrações & Logs:
    Suporta `whatsapp`, `evolution_real`, `github`, `github_open_real`, `github_close_real` e `ecommerce`.
    """
    workspace_slug = get_workspace_slug_from_request(request)
    source = request.POST.get("source", "whatsapp").strip().lower()

    if source in ("github", "github_open_real"):
        issue_num = 42 if source == "github_open_real" else timezone.now().second + 300
        payload = {
            "action": "opened",
            "repository": {
                "name": "Sistema-de-Chamado",
                "full_name": "caio/Sistema-de-Chamado",
            },
            "pull_request": {
                "number": issue_num,
                "title": "Implementar fila prioritária Redis e SLA dinâmico no Deskify",
                "html_url": f"https://github.com/caio/Sistema-de-Chamado/pull/{issue_num}",
            },
        }
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.GITHUB,
            event_type="pull_request.opened",
            payload=payload,
        )
        process_github_webhook(event.id)
        msg = f"Webhook GitHub (Sistema-de-Chamado#{issue_num}) processado -> Vinculado ao Deskify na Inbox!"
    elif source == "github_close_real":
        payload = {
            "action": "closed",
            "repository": {
                "name": "Sistema-de-Chamado",
                "full_name": "caio/Sistema-de-Chamado",
            },
            "pull_request": {
                "number": 42,
                "title": "Implementar fila prioritária Redis e SLA dinâmico no Deskify",
                "html_url": "https://github.com/caio/Sistema-de-Chamado/pull/42",
                "merged": True,
            },
        }
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.GITHUB,
            event_type="pull_request.closed",
            payload=payload,
        )
        process_github_webhook(event.id)
        msg = "Webhook GitHub (PR #42 Closed/Merged) processado -> Tarefa concluída e Timer encerrado!"
    elif source == "ecommerce":
        payload = {
            "store": "Info Mundi / Mercuria Shop",
            "sku": f"SKU-SHP-{timezone.now().second + 500}",
            "issue": "Alerta Shopee: Estoque crítico no Kit Teclado Mecânico Sem Fio (Urgência Máxima)",
        }
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.ECOMMERCE,
            event_type="ecommerce.alert",
            payload=payload,
        )
        process_ecommerce_webhook(event.id)
        msg = "Webhook Shopee/ML simulado -> Alerta urgente (#f97316) criado na Inbox!"
    elif source == "gemini_sync":
        payload = {
            "action": "upsert",
            "title": "Refatorar módulo de relatórios e exportação no Deskify",
            "workspace": "saas",
            "project": "Deskify",
            "assignee": "caio",
            "shift": "morning",
            "status": "todo",
            "impact": 5,
            "urgency": 5,
            "is_top_three": True,
            "minutes_spent": 30,
            "notes": "Alinhado no chat com o Gemini AI para aplicar tokens semânticos do Tactile Design System.",
            "subtasks": [
                "[x] Ajustar contraste dos cards em Tactile Dark e Tactile Sand",
                "Padronizar colunas da tabela de métricas e exportação",
                "Validar testes automatizados do módulo técnico",
            ],
        }
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.CUSTOM,
            event_type="gemini.sync",
            payload=payload,
        )
        process_gemini_webhook(event.id)
        msg = "⚡ Sync Gemini AI simulado -> Task no Deskify criada com 3 subtarefas e 30 min registrados!"
    elif source == "evolution_real":
        payload = {
            "event": "messages.upsert",
            "instance": "command-center-prod",
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "fromMe": True,
                    "id": f"EVO-{timezone.now().strftime('%H%M%S')}",
                },
                "pushName": "Caio Dev",
                "message": {
                    "extendedTextMessage": {
                        "text": "/task Revisar campanha no Portal MS CREW #wsa @henrique /tarde !hoje"
                    }
                },
            },
            "text": "/task Revisar campanha no Portal MS CREW #wsa @henrique /tarde !hoje",
            "sender": "@henrique",
        }
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.WHATSAPP,
            event_type="messages.upsert",
            payload=payload,
        )
        process_whatsapp_webhook(event.id)
        msg = "Payload Real Evolution API (messages.upsert com /task) processado com sucesso!"
    else:
        payload = {
            "text": "/task [Áudio Evolution API] Cliente solicitou ajuste no banner da LP #wsa @henrique /tarde",
            "sender": "@henrique",
        }
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.WHATSAPP,
            event_type="whatsapp.direct",
            payload=payload,
        )
        process_whatsapp_webhook(event.id)
        msg = "Webhook WhatsApp (Evolution API / Node.js) simulado -> Tarefa de cliente na Inbox!"

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="warning" if source == "ecommerce" else "info",
    )


@require_POST
def reprocess_webhook_event_view(request: HttpRequest, event_id: int) -> HttpResponse:
    """Reprocessa um `WebhookEvent` existente com 1 clique na tela de Integrações & Logs."""
    event = get_object_or_404(WebhookEvent, pk=event_id)
    workspace_slug = get_workspace_slug_from_request(request)

    if event.source == WebhookEvent.Source.GITHUB:
        process_github_webhook(event.id)
    elif event.source == WebhookEvent.Source.ECOMMERCE:
        process_ecommerce_webhook(event.id)
    elif event.source == WebhookEvent.Source.CUSTOM or event.event_type == "gemini.sync":
        process_gemini_webhook(event.id)
    else:
        process_whatsapp_webhook(event.id)

    event.refresh_from_db()
    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=f"Evento de Webhook #{event.id} ({event.source}) reprocessado com sucesso!",
        feedback_level="info",
    )


# Endpoints dedicados para fragmentos parciais isolados
@require_GET
def partial_top_three_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/top_three.html", context)


@require_GET
def partial_shift_queue_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/shift_queue.html", context)


@require_GET
def partial_inbox_list_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/inbox_list.html", context)


@require_GET
def partial_analytics_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/analytics_view.html", context)


@require_GET
def partial_integrations_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/integrations_view.html", context)


@require_GET
def partial_styleguide_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/styleguide_view.html", context)


@require_GET
def partial_settings_view(request: HttpRequest) -> HttpResponse:
    context = build_dashboard_context(request)
    return render(request, "partials/settings_view.html", context)


# --- PHASE 6: PONTE DUPLA GEMINI SYNC (1-CLICK CLIPBOARD PASTE + MULTI-LINE PARSER) ---

@require_POST
def gemini_paste_sync_view(request: HttpRequest) -> HttpResponse:
    """
    Endpoint HTMX (`POST /tasks/gemini-paste/`) acionado pelo botão `"⚡ Sync Gemini"`
    no Header ou pelo Modal Tátil de Fallback.
    Interpreta JSON estruturado do Gemini, blocos multi-linha `/gemini` ou comandos rápidos,
    criando/atualizando a Task, SubTasks, Notes e TimeLog em 1 clique.
    """
    workspace_slug = get_workspace_slug_from_request(request)
    clipboard_text = (
        request.POST.get("clipboard_text")
        or request.POST.get("raw_content")
        or ""
    ).strip()

    if not clipboard_text:
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message="Nenhum conteúdo recebido da área de transferência. Cole o bloco do Gemini no modal.",
            feedback_level="warning",
        )

    try:
        sync_result = process_gemini_or_whatsapp_sync(
            raw_input=clipboard_text,
            source="gemini",
            current_workspace_slug=workspace_slug,
        )
        task = sync_result["task"]
        WebhookEvent.objects.create(
            source=WebhookEvent.Source.CUSTOM,
            event_type="gemini.sync",
            payload={"raw_content": clipboard_text, "format": sync_result["format"]},
            processed=True,
            created_task=task,
        )
        msg = (
            f'⚡ Sync Gemini aplicado: "{task.title}" '
            f'(+{sync_result["subtasks_created"]} subtarefas, {sync_result["minutes_logged"]} min)'
        )
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message=msg,
            feedback_level="info",
        )
    except Exception as exc:
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message=f"Erro ao processar Sync Gemini: {exc}",
            feedback_level="warning",
        )


# --- PHASE 7: MOTOR HÍBRIDO DE VOZ & ÁUDIO COM TRAVAS DE SEGURANÇA E COTA ---

@require_POST
def voice_dictation_create_view(request: HttpRequest) -> HttpResponse:
    """
    Endpoint HTMX (`POST /tasks/voice-dictation/`) para o Ditado Nativo do navegador (Custo Zero).
    Converte fala natural em português (`normalize_spoken_command_ptbr`) e cria a tarefa.
    """
    workspace_slug = get_workspace_slug_from_request(request)
    spoken_text = (
        request.POST.get("spoken_text")
        or request.POST.get("command")
        or ""
    ).strip()

    if not spoken_text:
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message="Nenhuma fala detectada pelo microfone.",
            feedback_level="warning",
        )

    normalized = normalize_spoken_command_ptbr(spoken_text)
    sync_result = process_gemini_or_whatsapp_sync(
        raw_input=normalized,
        source="gemini",
        current_workspace_slug=workspace_slug,
    )
    task = sync_result["task"]
    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=(
            f'🎙️ Ditado convertido: "{normalized}" -> Tarefa "{task.title}" '
            f'criada em #{task.workspace.slug} (Score: {task.priority_score})!'
        ),
        feedback_level="info",
    )


@require_POST
def audio_upload_sync_view(request: HttpRequest) -> HttpResponse:
    """
    Endpoint HTMX (`POST /tasks/audio-upload/`) acionado pelo modal `🎧 Áudio IA` no Header.
    Respeita as Travas 2 (3.5MB) e 3 (Kill-Switch e Disjuntor de Cota Diária).
    """
    import base64

    workspace_slug = get_workspace_slug_from_request(request)
    extra_hint = request.POST.get("extra_hint", "").strip()
    transcript_fallback = request.POST.get("transcript_fallback", "").strip()
    mime_type = request.POST.get("mime_type", "audio/webm").strip()

    audio_bytes = b""
    uploaded_file = request.FILES.get("audio_file")
    b64_audio = request.POST.get("audio_base64", "").strip()

    if uploaded_file:
        audio_bytes = uploaded_file.read()
        mime_type = uploaded_file.content_type or mime_type
    elif b64_audio:
        if "," in b64_audio and b64_audio.startswith("data:"):
            b64_audio = b64_audio.split(",", 1)[1]
        try:
            audio_bytes = base64.b64decode(b64_audio)
        except Exception:
            audio_bytes = b""

    if not audio_bytes and not extra_hint and not transcript_fallback:
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message="Grave um áudio ou selecione um arquivo antes de enviar.",
            feedback_level="warning",
        )

    result = process_audio_with_gemini_free_tier(
        audio_bytes=audio_bytes or b"voice_note",
        mime_type=mime_type,
        source="web",
        transcript_fallback=transcript_fallback,
        extra_hint=extra_hint,
        current_workspace_slug=workspace_slug,
    )

    if result.get("blocked"):
        return render_dashboard_or_partial(
            request,
            workspace_slug=workspace_slug,
            feedback_message=f'🔒 Trava de Segurança: {result.get("error")}',
            feedback_level="warning",
        )

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=(
            f'🎧 Áudio IA processado ({result["used_today"]}/{result["max_daily"]} hoje): '
            f'"{result["title"]}" (+{result["subtasks_created"]} subtarefas)'
        ),
        feedback_level="info",
    )


@require_POST
def task_ai_subtasks_view(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Endpoint HTMX (`POST /tasks/<int:pk>/ai-subtasks/`) acionado pelo botão
    `⚡ Quebrar com IA` no cabeçalho de Subtarefas dentro do Slide-Over Drawer.
    """
    task = get_object_or_404(Task, pk=pk)
    res = generate_subtasks_with_gemini(task)
    if not res.get("ok"):
        msg = f'🔒 {res.get("error")}'
    else:
        msg = f'⚡ {res["created_count"]} subtarefas técnicas geradas para "{task.title}"!'
    return render_drawer_with_oob(request, task=task, feedback_message=msg)


@require_POST
def audio_quota_config_save_view(request: HttpRequest) -> HttpResponse:
    """
    Atualiza as configurações do Motor de Voz & Disjuntor de Cota Diária
    (`gemini_api_key`, `max_daily_audio_requests`, `ai_audio_enabled`) na aba Integrações.
    """
    config = SystemIntegrationConfig.get_solo()

    if "toggle_killswitch" in request.POST:
        config.ai_audio_enabled = not config.ai_audio_enabled
        config.save(update_fields=["ai_audio_enabled", "updated_at"])
        state = "ATIVADO" if config.ai_audio_enabled else "PAUSADO (Kill-Switch)"
        return render_dashboard_or_partial(
            request,
            feedback_message=f"🔒 Processamento de Áudio IA {state}.",
            feedback_level="info" if config.ai_audio_enabled else "warning",
        )

    if "gemini_api_key" in request.POST:
        raw_key = request.POST.get("gemini_api_key", "").strip()
        if raw_key == "__CLEAR__":
            config.gemini_api_key = ""
        elif raw_key:
            config.gemini_api_key = raw_key[:255]

    if "max_daily_audio_requests" in request.POST:
        try:
            max_req = int(request.POST.get("max_daily_audio_requests", 20))
            config.max_daily_audio_requests = max(1, min(500, max_req))
        except (TypeError, ValueError):
            pass

    if "ai_audio_enabled" in request.POST:
        config.ai_audio_enabled = request.POST.get("ai_audio_enabled") in ("1", "true", "on")

    if "evolution_api_url" in request.POST:
        evo_url = request.POST.get("evolution_api_url", "").strip()
        config.evolution_api_url = (evo_url or "http://127.0.0.1:8080")[:255]

    if "evolution_instance_name" in request.POST:
        config.evolution_instance_name = request.POST.get("evolution_instance_name", "").strip()[:120]

    if "evolution_api_key" in request.POST:
        raw_evo_key = request.POST.get("evolution_api_key", "").strip()
        if raw_evo_key == "__CLEAR__":
            config.evolution_api_key = ""
        elif raw_evo_key:
            config.evolution_api_key = raw_evo_key[:255]

    config.save()
    return render_dashboard_or_partial(
        request,
        feedback_message=(
            f"Configurações de Segurança salvas: Limite diário de {config.max_daily_audio_requests} áudios/dia "
            f"({'Ativo' if config.ai_audio_enabled else 'Pausado'})."
        ),
        feedback_level="info",
    )


# --- PHASE 5: ATRIBUIÇÃO RÁPIDA DE RESPONSÁVEL (1 CLIQUE) E GESTÃO DINÂMICA (SETTINGS) ---

@require_POST
def task_assign_member_view(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Atribui, troca ou remove (`member_id=""` ou `"none"`) o responsável (`TeamMember`)
    de uma `Task` com 1 clique, tanto nos cards do Dashboard/Kanban quanto no Drawer.
    """
    task = get_object_or_404(Task, pk=pk)
    workspace_slug = get_workspace_slug_from_request(request)
    raw_member_id = request.POST.get("member_id", "").strip()
    raw_handle = request.POST.get("handle", "").strip().lstrip("@").lower()
    from_drawer = request.POST.get("from_drawer") == "1"
    toggle = request.POST.get("toggle") == "1"

    member = None
    if raw_member_id and raw_member_id not in ("none", "0", "null"):
        if raw_member_id.isdigit():
            member = TeamMember.objects.filter(pk=int(raw_member_id)).first()
    elif raw_handle:
        member = TeamMember.objects.filter(handle__iexact=raw_handle).first()

    if member is None:
        task.assignee = None
        task.assigned_to = ""
        task.save()
        msg = f'Responsável retirado da tarefa "{task.title}".'
    elif toggle and task.assignee_id == member.id:
        task.assignee = None
        task.assigned_to = ""
        task.save()
        msg = f'Atribuição de @{member.handle} retirada de "{task.title}".'
    else:
        task.assignee = member
        task.assigned_to = f"@{member.handle}"
        task.save()
        msg = f'Tarefa "{task.title}" atribuída a @{member.handle} ({member.name}).'

    if from_drawer:
        return render_drawer_with_oob(request, task=task, feedback_message=msg)

    return render_dashboard_or_partial(
        request,
        workspace_slug=workspace_slug,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def workspace_save_view(request: HttpRequest) -> HttpResponse:
    """Cria ou edita uma Frente de Trabalho (`Workspace`) na tela Gestão & Cadastros."""
    ws_id = request.POST.get("workspace_id", "").strip()
    name = request.POST.get("name", "").strip()[:100]
    raw_slug = request.POST.get("slug", "").strip().lstrip("#").lower()
    accent_color = request.POST.get("accent_color", "#3b82f6").strip()
    shortcut_key = request.POST.get("shortcut_key", "").strip()[:20]
    icon_name = request.POST.get("icon_name", "briefcase").strip()[:50] or "briefcase"

    if accent_color not in ("#3b82f6", "#f97316"):
        accent_color = "#3b82f6"

    if not name:
        return render_dashboard_or_partial(
            request,
            feedback_message="Informe o nome da Frente de Trabalho.",
            feedback_level="warning",
        )

    slug = slugify(raw_slug or name)[:50]
    if not slug:
        slug = "frente"

    if ws_id.isdigit():
        ws = get_object_or_404(Workspace, pk=int(ws_id))
        if Workspace.objects.filter(slug__iexact=slug).exclude(pk=ws.pk).exists():
            return render_dashboard_or_partial(
                request,
                feedback_message=f'Já existe outra frente com o slug "#{slug}".',
                feedback_level="warning",
            )
        ws.name = name
        ws.slug = slug
        ws.accent_color = accent_color
        ws.shortcut_key = shortcut_key
        ws.icon_name = icon_name
        ws.save()
        msg = f'Frente "#{ws.slug}" ({ws.name}) atualizada com sucesso.'
    else:
        if Workspace.objects.filter(slug__iexact=slug).exists():
            return render_dashboard_or_partial(
                request,
                feedback_message=f'A frente "#{slug}" já está cadastrada.',
                feedback_level="warning",
            )
        if not shortcut_key:
            next_num = Workspace.objects.count() + 1
            shortcut_key = f"Alt+{next_num}" if next_num <= 9 else ""
        ws = Workspace.objects.create(
            name=name,
            slug=slug,
            accent_color=accent_color,
            shortcut_key=shortcut_key,
            icon_name=icon_name,
        )
        msg = f'Nova Frente "#{ws.slug}" ({ws.name}) criada com sucesso!'

    return render_dashboard_or_partial(
        request,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def workspace_delete_view(request: HttpRequest, ws_id: int) -> HttpResponse:
    """
    Exclui uma Frente de Trabalho (`Workspace`).
    Se possuir tarefas/projetos vinculados e existir outra frente disponível (ou `target_workspace_id`),
    migra as tarefas órfãs automaticamente (a menos que `delete_tasks=1`), evitando qualquer erro 500.
    """
    ws = get_object_or_404(Workspace, pk=ws_id)
    ws_name = ws.name
    ws_slug = ws.slug

    delete_tasks = request.POST.get("delete_tasks") == "1"
    target_ws_id = request.POST.get("target_workspace_id", "").strip()

    fallback_ws = None
    if target_ws_id.isdigit():
        fallback_ws = Workspace.objects.exclude(pk=ws.pk).filter(pk=int(target_ws_id)).first()
    if fallback_ws is None and not delete_tasks:
        fallback_ws = Workspace.objects.exclude(pk=ws.pk).order_by("shortcut_key", "id").first()

    tasks_count = Task.objects.filter(workspace=ws).count()
    migrated_info = ""
    if tasks_count > 0 and fallback_ws is not None and not delete_tasks:
        Task.objects.filter(workspace=ws).update(workspace=fallback_ws)
        migrated_info = f' ({tasks_count} tarefa(s) migrada(s) para #{fallback_ws.slug})'

    current_ws_slug = get_workspace_slug_from_request(request)
    if current_ws_slug and current_ws_slug.lower() == ws_slug.lower():
        current_ws_slug = "all"

    ws.delete()
    return render_dashboard_or_partial(
        request,
        workspace_slug=current_ws_slug,
        feedback_message=f'Frente "#{ws_slug}" ({ws_name}) excluída{migrated_info}.',
        feedback_level="warning",
    )


@require_POST
def project_save_view(request: HttpRequest) -> HttpResponse:
    """Cria ou edita um Projeto, Cliente ou Loja (`Project`) na tela Gestão & Cadastros."""
    proj_id = request.POST.get("project_id", "").strip()
    name = request.POST.get("name", "").strip()[:120]
    ws_id = request.POST.get("workspace_id", "").strip()
    client_or_store = request.POST.get("client_or_store", "").strip()[:120]
    github_repo_name = request.POST.get("github_repo_name", "").strip()[:150]
    repo_url = request.POST.get("repo_url", "").strip()[:200]
    admin_or_panel_url = request.POST.get("admin_or_panel_url", "").strip()[:200]

    if not name:
        return render_dashboard_or_partial(
            request,
            feedback_message="Informe o nome do Projeto / Loja.",
            feedback_level="warning",
        )

    workspace = None
    if ws_id.isdigit():
        workspace = Workspace.objects.filter(pk=int(ws_id)).first()
    if workspace is None:
        workspace = Workspace.objects.order_by("shortcut_key", "id").first()

    if workspace is None:
        return render_dashboard_or_partial(
            request,
            feedback_message="Cadastre ao menos uma Frente de Trabalho antes de criar um Projeto.",
            feedback_level="warning",
        )

    if proj_id.isdigit():
        proj = get_object_or_404(Project, pk=int(proj_id))
        proj.name = name
        proj.workspace = workspace
        proj.client_or_store = client_or_store
        proj.github_repo_name = github_repo_name
        proj.repo_url = repo_url
        proj.admin_or_panel_url = admin_or_panel_url
        if "is_active" in request.POST:
            proj.is_active = request.POST.get("is_active") in ("1", "true", "on")
        proj.save()
        msg = f'Projeto/Loja "{proj.name}" (#{workspace.slug}) atualizado.'
    else:
        proj = Project.objects.create(
            name=name,
            workspace=workspace,
            client_or_store=client_or_store,
            github_repo_name=github_repo_name,
            repo_url=repo_url,
            admin_or_panel_url=admin_or_panel_url,
            is_active=True,
        )
        msg = f'Projeto/Loja "{proj.name}" vinculado a #{workspace.slug} com sucesso!'

    return render_dashboard_or_partial(
        request,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def project_toggle_active_view(request: HttpRequest, proj_id: int) -> HttpResponse:
    """Alterna o status Ativo / Arquivado (`is_active`) de um `Project`."""
    proj = get_object_or_404(Project, pk=proj_id)
    proj.is_active = not proj.is_active
    proj.save(update_fields=["is_active"])
    state_label = "Ativo no Launchpad" if proj.is_active else "Arquivado"
    return render_dashboard_or_partial(
        request,
        feedback_message=f'Projeto "{proj.name}" alterado para: {state_label}.',
        feedback_level="info",
    )


@require_POST
def project_delete_view(request: HttpRequest, proj_id: int) -> HttpResponse:
    """Exclui um `Project` (mantendo suas tarefas com `project=None`)."""
    proj = get_object_or_404(Project, pk=proj_id)
    proj_name = proj.name
    proj.delete()
    return render_dashboard_or_partial(
        request,
        feedback_message=f'Projeto/Loja "{proj_name}" removido.',
        feedback_level="warning",
    )


@require_POST
def team_member_save_view(request: HttpRequest) -> HttpResponse:
    """Cria ou edita um Responsável / Membro da Equipe (`TeamMember`) na tela Gestão & Cadastros."""
    member_id = request.POST.get("member_id", "").strip()
    name = request.POST.get("name", "").strip()[:100]
    raw_handle = request.POST.get("handle", "").strip().lstrip("@").lower()
    role = request.POST.get("role", "").strip()[:100]
    accent_color = request.POST.get("accent_color", "#3b82f6").strip()
    avatar_initials = request.POST.get("avatar_initials", "").strip().upper()[:4]

    if accent_color not in ("#3b82f6", "#f97316"):
        accent_color = "#3b82f6"

    if not name and not raw_handle:
        return render_dashboard_or_partial(
            request,
            feedback_message="Informe o nome e o @handle do responsável.",
            feedback_level="warning",
        )

    if not name:
        name = raw_handle.capitalize()
    handle = slugify(raw_handle or name).replace("-", "")[:50] or "membro"

    if member_id.isdigit():
        member = get_object_or_404(TeamMember, pk=int(member_id))
        if TeamMember.objects.filter(handle__iexact=handle).exclude(pk=member.pk).exists():
            return render_dashboard_or_partial(
                request,
                feedback_message=f'Já existe outro membro com o handle "@{handle}".',
                feedback_level="warning",
            )
        member.name = name
        member.handle = handle
        member.role = role
        member.accent_color = accent_color
        if avatar_initials:
            member.avatar_initials = avatar_initials
        if "is_active" in request.POST:
            member.is_active = request.POST.get("is_active") in ("1", "true", "on")
        member.save()
        Task.objects.filter(assignee=member).update(assigned_to=f"@{member.handle}")
        msg = f'Responsável @{member.handle} ({member.name}) atualizado.'
    else:
        if TeamMember.objects.filter(handle__iexact=handle).exists():
            return render_dashboard_or_partial(
                request,
                feedback_message=f'O responsável "@{handle}" já está cadastrado.',
                feedback_level="warning",
            )
        member = TeamMember.objects.create(
            name=name,
            handle=handle,
            role=role,
            accent_color=accent_color,
            avatar_initials=avatar_initials,
            is_active=True,
        )
        # Vincula automaticamente tarefas existentes que já tinham assigned_to="@handle"
        Task.objects.filter(
            assignee__isnull=True, assigned_to__iexact=f"@{member.handle}"
        ).update(assignee=member, assigned_to=f"@{member.handle}")
        msg = f'Responsável @{member.handle} ({member.name}) cadastrado na equipe!'

    return render_dashboard_or_partial(
        request,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def team_member_toggle_active_view(request: HttpRequest, member_id: int) -> HttpResponse:
    """Alterna o status Ativo / Inativo (`is_active`) de um `TeamMember`."""
    member = get_object_or_404(TeamMember, pk=member_id)
    member.is_active = not member.is_active
    member.save(update_fields=["is_active"])
    state_label = "Ativo" if member.is_active else "Inativo"
    return render_dashboard_or_partial(
        request,
        feedback_message=f'Responsável @{member.handle} marcado como {state_label}.',
        feedback_level="info",
    )


@require_POST
def team_member_delete_view(request: HttpRequest, member_id: int) -> HttpResponse:
    """
    Exclui um `TeamMember` e limpa `assignee=None` + `assigned_to=""`
    nas tarefas que estavam atribuídas a ele.
    """
    member = get_object_or_404(TeamMember, pk=member_id)
    handle = member.handle
    name = member.name
    Task.objects.filter(assignee=member).update(assignee=None, assigned_to="")
    Task.objects.filter(assigned_to__iexact=f"@{handle}").update(assignee=None, assigned_to="")
    member.delete()
    return render_dashboard_or_partial(
        request,
        feedback_message=f'Responsável @{handle} ({name}) removido e tarefas desvinculadas.',
        feedback_level="warning",
    )


# --- COFRE DE CREDENCIAIS & ACESSOS (VAULT) ---

@require_POST
def vault_credential_save_view(request: HttpRequest) -> HttpResponse:
    """Cria ou edita uma credencial (`CredentialEntry`) criptografando a senha/token com segurança."""
    cred_id = request.POST.get("credential_id", "").strip()
    title = request.POST.get("title", "").strip()[:150]
    category = request.POST.get("category", CredentialEntry.Category.EMAIL).strip()
    username_or_email = request.POST.get("username_or_email", "").strip()[:200]
    raw_secret = request.POST.get("secret", "")
    service_url = request.POST.get("service_url", "").strip()[:200]
    recovery_info = request.POST.get("recovery_info", "").strip()[:255]
    notes = request.POST.get("notes", "").strip()

    ws_id_or_slug = request.POST.get("workspace_id", "").strip()
    proj_id = request.POST.get("project_id", "").strip()

    if not title or not username_or_email:
        return render_dashboard_or_partial(
            request,
            feedback_message="Informe o Título do Acesso e o E-mail / Usuário.",
            feedback_level="warning",
        )

    valid_cats = {c[0] for c in CredentialEntry.Category.choices}
    if category not in valid_cats:
        category = CredentialEntry.Category.OTHER

    workspace = None
    if ws_id_or_slug.isdigit():
        workspace = Workspace.objects.filter(pk=int(ws_id_or_slug)).first()
    elif ws_id_or_slug and ws_id_or_slug != "all":
        workspace = Workspace.objects.filter(slug__iexact=ws_id_or_slug.lstrip("#")).first()

    project = None
    if proj_id.isdigit():
        project = Project.objects.select_related("workspace").filter(pk=int(proj_id)).first()
        if project and not workspace:
            workspace = project.workspace

    if not workspace:
        current_ws_slug = get_workspace_slug_from_request(request)
        if current_ws_slug:
            workspace = Workspace.objects.filter(slug__iexact=current_ws_slug).first()
    if not workspace:
        workspace = Workspace.objects.order_by("shortcut_key", "id").first()

    if cred_id.isdigit():
        cred = get_object_or_404(CredentialEntry, pk=int(cred_id))
        cred.workspace = workspace
        cred.project = project
        cred.title = title
        cred.category = category
        cred.username_or_email = username_or_email
        cred.service_url = service_url
        cred.recovery_info = recovery_info
        cred.notes = notes
        if raw_secret.strip():
            cred.set_secret(raw_secret.strip())
        cred.save()
        msg = f'Credencial "{cred.title}" (#{workspace.slug}) atualizada no Cofre.'
    else:
        cred = CredentialEntry(
            workspace=workspace,
            project=project,
            title=title,
            category=category,
            username_or_email=username_or_email,
            service_url=service_url,
            recovery_info=recovery_info,
            notes=notes,
        )
        cred.set_secret(raw_secret.strip())
        cred.save()
        msg = f'Credencial "{cred.title}" criptografada e salva em #{workspace.slug}!'

    return render_dashboard_or_partial(
        request,
        feedback_message=msg,
        feedback_level="info",
    )


@require_POST
def vault_credential_delete_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Exclui uma credencial do Cofre (`CredentialEntry`)."""
    cred = get_object_or_404(CredentialEntry, pk=pk)
    title = cred.title
    cred.delete()
    return render_dashboard_or_partial(
        request,
        feedback_message=f'Credencial "{title}" removida do Cofre.',
        feedback_level="warning",
    )


@require_GET
def vault_credential_secret_view(request: HttpRequest, pk: int) -> JsonResponse:
    """
    Endpoint seguro sob demanda (`GET /vault/<int:pk>/secret/`) para revelar ou copiar
    a senha descriptografada sem nunca expô-la no HTML estático inicial.
    """
    cred = get_object_or_404(CredentialEntry, pk=pk)
    return JsonResponse(
        {
            "ok": True,
            "id": cred.id,
            "title": cred.title,
            "username_or_email": cred.username_or_email,
            "secret": cred.get_secret(),
        }
    )

