import json
import re
from datetime import date, timedelta
from typing import Any, Optional, Union

from django.utils import timezone

from apps.core.models import Workspace, Project, TeamMember
from apps.tasks.models import Task, SubTask
from apps.timetracker.models import TimeLog

# Expressões regulares para captura rápida (Ctrl + K, WhatsApp e Gemini Sync)
COMMAND_TRIGGER_RE = re.compile(r"^(?:/task|!cc|/gemini)\b\s*", re.IGNORECASE)
WORKSPACE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z0-9_-]+)", re.IGNORECASE)
ASSIGNEE_TAG_RE = re.compile(r"(?:^|\s)(@[a-zA-Z0-9_.-]+)", re.IGNORECASE)
TODAY_TAG_RE = re.compile(r"(?:^|\s)!hoje\b", re.IGNORECASE)
TOP3_TAG_RE = re.compile(r"(?:^|\s)!top3\b", re.IGNORECASE)
SHIFT_TAG_RE = re.compile(r"(?:^|\s)/(manha|manhã|tarde|noite)\b", re.IGNORECASE)
IMPACT_TAG_RE = re.compile(r"(?:^|\s)!i([1-5])\b", re.IGNORECASE)
URGENCY_TAG_RE = re.compile(r"(?:^|\s)!u([1-5])\b", re.IGNORECASE)

SHIFT_MAP = {
    "manha": Task.Shift.MORNING,
    "manhã": Task.Shift.MORNING,
    "tarde": Task.Shift.AFTERNOON,
    "noite": Task.Shift.NIGHT,
}

DEFAULT_WORKSPACE_CONFIGS = {
    "wsa": ("WSA Creative", "#3b82f6", "globe", "Alt+1"),
    "saas": ("SaaS & Dev", "#3b82f6", "code", "Alt+2"),
    "ecom": ("E-commerce", "#f97316", "shopping-bag", "Alt+3"),
    "facul": ("Faculdade SI", "#3b82f6", "academic-cap", "Alt+4"),
    "games": ("Indie Games", "#f97316", "cpu", "Alt+5"),
}


def resolve_workspace(slug: Optional[str]) -> Workspace:
    """Resolve ou cria um Workspace válido a partir do slug informado."""
    if slug:
        slug_clean = slug.lower().strip()
        ws = Workspace.objects.filter(slug__iexact=slug_clean).first()
        if ws:
            return ws
        if slug_clean in DEFAULT_WORKSPACE_CONFIGS:
            name, color, icon, shortcut = DEFAULT_WORKSPACE_CONFIGS[slug_clean]
            return Workspace.objects.create(
                slug=slug_clean,
                name=name,
                accent_color=color,
                icon_name=icon,
                shortcut_key=shortcut,
            )

    first_ws = Workspace.objects.first()
    if first_ws:
        return first_ws

    # Fallback caso o banco ainda esteja vazio
    name, color, icon, shortcut = DEFAULT_WORKSPACE_CONFIGS["saas"]
    return Workspace.objects.create(
        slug="saas",
        name=name,
        accent_color=color,
        icon_name=icon,
        shortcut_key=shortcut,
    )


def detect_project_for_workspace(text: str, workspace: Workspace) -> Optional[Project]:
    """Associa automaticamente o projeto caso o nome do projeto/loja/repo conste no comando."""
    projects = Project.objects.filter(workspace=workspace, is_active=True)
    text_lower = text.lower()
    for project in projects:
        if project.name.lower() in text_lower:
            return project
        if project.client_or_store and project.client_or_store.lower() in text_lower:
            return project
        if project.github_repo_name and project.github_repo_name.lower() in text_lower:
            return project
    return None


def parse_and_create_task(
    raw_text: str,
    current_workspace_slug: Optional[str] = None,
    default_status: str = Task.Status.TODO,
) -> Task:
    """
    Interpreta comandos rápidos digitados na barra Ctrl + K (ou via WhatsApp):
      - #slug  -> Define o Workspace (#wsa, #saas, #ecom, #facul, #games)
      - @nome  -> Define assigned_to (@pedro, @henrique, @monica)
      - !hoje  -> Define urgency=5 e due_date=date.today()
      - !top3  -> Define impact=5, urgency=5 e is_top_three=True
      - /manha, /tarde, /noite -> Define o shift
    Limpa as tags do texto final antes de salvar o title.
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("O comando da tarefa não pode estar vazio.")

    extracted_workspace_slug = None
    assigned_to = ""
    shift = Task.Shift.MORNING
    impact = 3
    urgency = 3
    due_date = None
    is_top_three = False

    # 1. Extrair #workspace
    ws_match = WORKSPACE_TAG_RE.search(text)
    if ws_match:
        extracted_workspace_slug = ws_match.group(1).lower()
        text = WORKSPACE_TAG_RE.sub(" ", text)

    # 2. Extrair @assigned_to
    assignee_match = ASSIGNEE_TAG_RE.search(text)
    if assignee_match:
        assigned_to = assignee_match.group(1).lower()
        text = ASSIGNEE_TAG_RE.sub(" ", text)

    # 3. Extrair /shift (/manha, /tarde, /noite)
    shift_match = SHIFT_TAG_RE.search(text)
    if shift_match:
        raw_shift = shift_match.group(1).lower()
        shift = SHIFT_MAP.get(raw_shift, Task.Shift.MORNING)
        text = SHIFT_TAG_RE.sub(" ", text)

    # 4. Extrair modificadores opcionais de impacto/urgência (!i4, !u5)
    impact_match = IMPACT_TAG_RE.search(text)
    if impact_match:
        impact = int(impact_match.group(1))
        text = IMPACT_TAG_RE.sub(" ", text)

    urgency_match = URGENCY_TAG_RE.search(text)
    if urgency_match:
        urgency = int(urgency_match.group(1))
        text = URGENCY_TAG_RE.sub(" ", text)

    # 5. Extrair !hoje
    if TODAY_TAG_RE.search(text):
        urgency = 5
        due_date = date.today()
        text = TODAY_TAG_RE.sub(" ", text)

    # 6. Extrair !top3
    if TOP3_TAG_RE.search(text):
        impact = 5
        urgency = 5
        is_top_three = True
        text = TOP3_TAG_RE.sub(" ", text)

    # Limpar espaços excedentes no título final
    clean_title = re.sub(r"\s+", " ", text).strip()
    if not clean_title:
        clean_title = "Nova Tarefa Rápida"

    target_slug = extracted_workspace_slug or (
        current_workspace_slug if current_workspace_slug not in (None, "", "all") else None
    )
    workspace = resolve_workspace(target_slug)

    # Se não especificou turno explicitamente e a frente for E-commerce, default natural pode ser mantido em morning ou ajustado
    project = detect_project_for_workspace(clean_title, workspace)

    task = Task(
        workspace=workspace,
        project=project,
        title=clean_title,
        status=default_status,
        shift=shift,
        impact=impact,
        urgency=urgency,
        is_top_three=is_top_three,
        assigned_to=assigned_to,
        due_date=due_date,
    )
    task.save()
    return task


def _parse_duration_minutes(raw_val: Any) -> int:
    """Converte strings de tempo como '45m', '1h 30m', '1h', '25 min' ou int em minutos."""
    if raw_val is None:
        return 0
    if isinstance(raw_val, (int, float)):
        return max(0, int(raw_val))
    raw_str = str(raw_val).strip().lower()
    if not raw_str:
        return 0
    if raw_str.isdigit():
        return int(raw_str)

    total_mins = 0
    hours_match = re.search(r"(\d+)\s*h", raw_str)
    mins_match = re.search(r"(\d+)\s*m(?:in)?", raw_str)
    if hours_match:
        total_mins += int(hours_match.group(1)) * 60
    if mins_match:
        total_mins += int(mins_match.group(1))
    if total_mins == 0:
        any_num = re.search(r"(\d+)", raw_str)
        if any_num:
            total_mins = int(any_num.group(1))
    return max(0, total_mins)


def _resolve_project_by_name(project_name: str, workspace: Optional[Workspace] = None) -> Optional[Project]:
    """Busca um Project pelo nome exato, parcial, client_or_store ou github_repo_name."""
    clean_name = (project_name or "").strip()
    if not clean_name:
        return None

    qs = Project.objects.select_related("workspace")
    proj = (
        qs.filter(name__iexact=clean_name).first()
        or qs.filter(name__icontains=clean_name).first()
        or qs.filter(client_or_store__icontains=clean_name).first()
        or qs.filter(github_repo_name__iexact=clean_name).first()
    )
    if proj:
        return proj

    if workspace:
        return qs.filter(workspace=workspace, is_active=True).first()
    return None


def _create_subtasks_from_list(task: Task, subtasks_raw: list) -> int:
    """Cria SubTasks a partir de uma lista de strings ou dicionários (suportando prefixos '[x] ' e '- [ ] ')."""
    created_count = 0
    for item in subtasks_raw or []:
        is_done = False
        st_title = ""
        if isinstance(item, dict):
            st_title = str(item.get("title") or "").strip()
            is_done = bool(item.get("is_done") or item.get("done"))
        else:
            raw_line = str(item or "").strip()
            raw_line = re.sub(r"^(?:[-*•]|\d+\.)\s*", "", raw_line).strip()
            checkbox_match = re.match(r"^\[([ xX✓])\]\s*(.+)$", raw_line)
            if checkbox_match:
                is_done = checkbox_match.group(1).strip().lower() in ("x", "✓")
                st_title = checkbox_match.group(2).strip()
            else:
                st_title = raw_line

        if st_title:
            SubTask.objects.create(task=task, title=st_title[:200], is_done=is_done)
            created_count += 1
    return created_count


def _create_completed_timelog(task: Task, minutes: int) -> Optional[TimeLog]:
    """Cria um registro TimeLog concluído caso minutes > 0."""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return None
    end_dt = timezone.now()
    start_dt = end_dt - timedelta(minutes=minutes)
    return TimeLog.objects.create(
        task=task,
        start_time=start_dt,
        end_time=end_dt,
        duration_minutes=minutes,
        is_running=False,
    )


def _try_parse_gemini_json(raw_input: Union[str, dict]) -> Optional[dict]:
    """Tenta extrair um dicionário JSON válido (mesmo se envolto em cercas Markdown ```json ... ```)."""
    if isinstance(raw_input, dict):
        if "raw_content" in raw_input and len(raw_input) == 1 and isinstance(raw_input["raw_content"], str):
            return _try_parse_gemini_json(raw_input["raw_content"])
        return raw_input

    text = (raw_input or "").strip()
    if not text:
        return None

    # Remover cercas markdown ```json ... ``` se presentes
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
    else:
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()

    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                if "raw_content" in parsed and len(parsed) == 1 and isinstance(parsed["raw_content"], str):
                    return _try_parse_gemini_json(parsed["raw_content"])
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def process_gemini_or_whatsapp_sync(
    raw_input: Union[str, dict],
    source: str = "gemini",
    current_workspace_slug: Optional[str] = None,
    sender: str = "",
) -> dict:
    """
    Motor Inteligente da Fase 6 (Ponte Dupla Gemini + WhatsApp):
    Interpreta automaticamente 3 formatos:
      - Formato A: JSON Estruturado do Gemini (com ou sem cercas Markdown)
      - Formato B: Bloco Multi-linha (/gemini, /task ou !cc com Projeto:, Tempo:, - [ ], Notas:)
      - Formato C: Comando Rápido de 1 Linha (/task Corrigir anúncio #ecom !hoje)
    """
    json_data = _try_parse_gemini_json(raw_input)

    # --- FORMATO A: JSON ESTRUTURADO DO GEMINI ---
    if json_data is not None:
        action = str(json_data.get("action") or "upsert").strip().lower()
        title = str(json_data.get("title") or "Tarefa Sync Gemini").strip()[:255]
        ws_slug = str(json_data.get("workspace") or "").strip().lstrip("#").lower()
        project_name = str(json_data.get("project") or "").strip()
        assignee_raw = str(
            json_data.get("assignee") or json_data.get("assigned_to") or ""
        ).strip().lstrip("@").lower()
        shift_raw = str(json_data.get("shift") or "morning").strip().lower()
        status_raw = str(json_data.get("status") or "").strip().lower()
        is_top_three = bool(json_data.get("is_top_three", False))

        try:
            impact = max(1, min(5, int(json_data.get("impact") or (5 if is_top_three else 3))))
        except (TypeError, ValueError):
            impact = 5 if is_top_three else 3

        try:
            urgency = max(1, min(5, int(json_data.get("urgency") or (5 if is_top_three else 3))))
        except (TypeError, ValueError):
            urgency = 5 if is_top_three else 3

        minutes_spent = _parse_duration_minutes(
            json_data.get("minutes_spent") or json_data.get("minutes") or json_data.get("tempo") or 0
        )
        notes = str(json_data.get("notes") or "").strip()
        subtasks_list = json_data.get("subtasks") or []
        external_ref = str(json_data.get("external_ref") or "").strip()[:120]

        project = _resolve_project_by_name(project_name) if project_name else None
        effective_ws_slug = (
            ws_slug
            or (project.workspace.slug if project else None)
            or (current_workspace_slug if current_workspace_slug not in (None, "", "all") else None)
        )
        workspace = resolve_workspace(effective_ws_slug)
        if project is None:
            project = detect_project_for_workspace(title, workspace)

        shift = SHIFT_MAP.get(shift_raw, shift_raw if shift_raw in ("morning", "afternoon", "night") else Task.Shift.MORNING)

        if action == "complete" or status_raw == "done":
            target_status = Task.Status.DONE
        elif status_raw in ("inbox", "todo", "doing", "done"):
            target_status = status_raw
        else:
            target_status = Task.Status.TODO

        task = None
        if external_ref:
            task = Task.objects.filter(external_ref=external_ref).first()
        if task is None and (action == "complete" or json_data.get("update_existing")):
            task = Task.objects.filter(title__iexact=title).first()

        if task is None:
            task = Task(
                workspace=workspace,
                project=project,
                title=title,
                status=target_status,
                shift=shift,
                impact=impact,
                urgency=urgency,
                is_top_three=is_top_three and target_status != Task.Status.DONE,
                notes=notes,
                external_ref=external_ref,
                due_date=date.today() if (is_top_three or urgency == 5) else None,
            )
        else:
            task.workspace = workspace
            if project:
                task.project = project
            task.title = title
            task.status = target_status
            task.shift = shift
            task.impact = impact
            task.urgency = urgency
            if target_status == Task.Status.DONE:
                task.is_top_three = False
                task.completed_at = timezone.now()
            else:
                task.is_top_three = is_top_three
            if notes:
                task.notes = f"{task.notes}\n\n{notes}".strip() if task.notes and notes not in task.notes else notes
            if external_ref:
                task.external_ref = external_ref

        if assignee_raw:
            task.assigned_to = f"@{assignee_raw}"

        task.save()

        if task.status == Task.Status.DONE:
            for active_log in TimeLog.objects.filter(task=task, is_running=True):
                active_log.stop()

        subtasks_created = _create_subtasks_from_list(task, subtasks_list)
        _create_completed_timelog(task, minutes_spent)

        return {
            "format": "json",
            "action": action,
            "task": task,
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "workspace": task.workspace.slug,
            "project": task.project.name if task.project else None,
            "assignee": task.assigned_to,
            "subtasks_created": subtasks_created,
            "minutes_logged": minutes_spent,
            "priority_score": task.priority_score,
        }

    # --- FORMATOS B (MULTI-LINHA) & C (1 LINHA) ---
    raw_text = str(raw_input or "").strip()
    if not raw_text:
        raise ValueError("Conteúdo vazio enviado para sincronização.")

    raw_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not raw_lines:
        raise ValueError("Conteúdo vazio enviado para sincronização.")

    first_line = COMMAND_TRIGGER_RE.sub("", raw_lines[0]).strip()
    remaining_lines = raw_lines[1:]
    if not first_line and remaining_lines:
        first_line = COMMAND_TRIGGER_RE.sub("", remaining_lines[0]).strip()
        remaining_lines = remaining_lines[1:]

    # Se o usuário enviou apenas "/task" ou "!cc" (ex: respondendo a um áudio sem texto extra),
    # define um título padrão em vez de lançar ValueError 500.
    if not first_line:
        first_line = (
            "Processar nota de voz capturada no WhatsApp"
            if source == "whatsapp"
            else "Nova Tarefa Sync Gemini"
        )

    explicit_ws_in_first_line = bool(WORKSPACE_TAG_RE.search(first_line))
    has_urgent_flag = bool(TOP3_TAG_RE.search(first_line) or TODAY_TAG_RE.search(first_line))
    is_gemini_trigger = bool(re.match(r"^/gemini\b", raw_lines[0], re.IGNORECASE))

    if source == "whatsapp" and not has_urgent_flag and not is_gemini_trigger and not remaining_lines:
        default_status = Task.Status.INBOX
    else:
        default_status = Task.Status.TODO

    task = parse_and_create_task(
        raw_text=first_line,
        current_workspace_slug=current_workspace_slug,
        default_status=default_status,
    )

    minutes_spent = 0
    subtasks_to_create = []
    notes_lines = []

    for line in remaining_lines:
        proj_match = re.match(r"^(?:projeto|project|loja|cliente)\s*:\s*(.+)$", line, re.IGNORECASE)
        if proj_match:
            proj_name = proj_match.group(1).strip()
            resolved_proj = _resolve_project_by_name(proj_name, task.workspace)
            if resolved_proj:
                task.project = resolved_proj
                if not explicit_ws_in_first_line:
                    task.workspace = resolved_proj.workspace
            continue

        time_match = re.match(r"^(?:tempo|time|minutos|dura[cç][aã]o)\s*:\s*(.+)$", line, re.IGNORECASE)
        if time_match:
            minutes_spent += _parse_duration_minutes(time_match.group(1))
            continue

        assignee_line_match = re.match(r"^(?:respons[aá]vel|assignee)\s*:\s*(.+)$", line, re.IGNORECASE)
        if assignee_line_match:
            raw_handle = assignee_line_match.group(1).strip().lstrip("@").split()[0].lower()
            if raw_handle:
                task.assigned_to = f"@{raw_handle}"
            continue

        subtask_match = re.match(r"^(?:[-*•]|\d+\.)\s*(.+)$", line) or re.match(r"^(\[[ xX✓]\]\s*.+)$", line)
        if subtask_match:
            subtasks_to_create.append(line)
            continue

        notes_match = re.match(r"^(?:notas?|notes?|resumo|obs)\s*:\s*(.*)$", line, re.IGNORECASE)
        if notes_match:
            note_val = notes_match.group(1).strip()
            if note_val:
                notes_lines.append(note_val)
            continue

        notes_lines.append(line)

    if sender and not task.assigned_to:
        formatted_sender = sender if sender.startswith("@") else f"@{sender.split()[0].lower()}"
        task.assigned_to = formatted_sender[:50]

    if notes_lines:
        task.notes = "\n".join(notes_lines).strip()

    if task.status != default_status and default_status == Task.Status.INBOX:
        task.status = Task.Status.INBOX

    task.save()

    subtasks_created = _create_subtasks_from_list(task, subtasks_to_create)
    _create_completed_timelog(task, minutes_spent)

    return {
        "format": "multiline" if remaining_lines else "singleline",
        "action": "upsert",
        "task": task,
        "task_id": task.id,
        "title": task.title,
        "status": task.status,
        "workspace": task.workspace.slug,
        "project": task.project.name if task.project else None,
        "assignee": task.assigned_to,
        "subtasks_created": subtasks_created,
        "minutes_logged": minutes_spent,
        "priority_score": task.priority_score,
    }


