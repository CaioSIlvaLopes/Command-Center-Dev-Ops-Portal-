import re
from datetime import date
from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Project
from apps.core.permissions import get_default_superuser
from apps.core.services import (
    parse_and_create_task,
    process_gemini_or_whatsapp_sync,
    resolve_workspace,
    TODAY_TAG_RE,
    TOP3_TAG_RE,
)
from apps.core.whatsapp_service import send_task_creation_confirmation
from apps.integrations.models import WebhookEvent
from apps.tasks.models import Task
from apps.timetracker.models import TimeLog

TRIGGER_PREFIX_RE = re.compile(r"^(?:/task|!cc|/gemini)\b\s*", re.IGNORECASE)


def strip_whatsapp_trigger(raw_text: str) -> str:
    """Remove o prefixo `/task`, `!cc` ou `/gemini` do início da mensagem."""
    return TRIGGER_PREFIX_RE.sub("", (raw_text or "").strip()).strip()


def has_whatsapp_trigger(raw_text: str) -> bool:
    """Verifica se a mensagem inicia com `/task`, `!cc` ou `/gemini`."""
    return bool(TRIGGER_PREFIX_RE.match((raw_text or "").strip()))


def extract_evolution_text_and_sender(payload: dict) -> tuple[str, str]:
    """Extrai texto e remetente do payload nativo da Evolution API v2 (`messages.upsert`)."""
    data = payload.get("data") or {}
    if isinstance(data, list) and data:
        data = data[0]
    message = data.get("message") or {}

    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("ephemeralMessage", {}).get("message", {}).get("conversation"))
        or (message.get("ephemeralMessage", {}).get("message", {}).get("extendedTextMessage", {}).get("text"))
        or data.get("body")
        or payload.get("text")
        or ""
    ).strip()

    sender = (
        data.get("pushName")
        or (data.get("key") or {}).get("remoteJid", "").split("@")[0]
        or payload.get("sender")
        or ""
    ).strip()

    return text, sender


def resolve_github_project(repo_name: str, full_name: str = ""):
    """Localiza o Project correspondente pelo `github_repo_name` ou `name`."""
    candidates = [c.strip() for c in [repo_name, full_name.split("/")[-1] if full_name else ""] if c and c.strip()]
    for name in candidates:
        proj = Project.objects.select_related("workspace").filter(github_repo_name__iexact=name).first()
        if proj:
            return proj
        proj = Project.objects.select_related("workspace").filter(name__icontains=name).first()
        if proj:
            return proj
    return None


def extract_evolution_sender_jid(payload: dict) -> str:
    """Extrai o remoteJid / número bruto do remetente do payload da Evolution API v2."""
    data = payload.get("data") or {}
    if isinstance(data, list) and data:
        data = data[0]
    return (
        (data.get("key") or {}).get("remoteJid")
        or payload.get("sender_whatsapp")
        or payload.get("sender")
        or ""
    ).strip()


@shared_task(name="apps.integrations.tasks.process_whatsapp_webhook")
def process_whatsapp_webhook(event_id: int) -> dict:
    """
    Processa evento de webhook do WhatsApp (Conector Node.js ou Evolution API):
    - Suporta comandos rápidos de 1 linha (`/task`, `!cc`, `/gemini`) E blocos multi-linha
      colados do Gemini (com `Projeto:`, `Tempo:`, `Lembrete:`, `- [ ]` subtarefas e `Notas:`).
    - Dispara confirmação automática no WhatsApp após criar a tarefa e lembretes.
    """
    event = WebhookEvent.objects.get(pk=event_id)
    try:
        payload = event.payload or {}
        raw_text = payload.get("text", "").strip()
        sender = payload.get("sender", "").strip()
        sender_whatsapp = extract_evolution_sender_jid(payload)

        if not raw_text and "data" in payload:
            raw_text, evo_sender = extract_evolution_text_and_sender(payload)
            if not sender:
                sender = evo_sender

        sync_result = process_gemini_or_whatsapp_sync(
            raw_input=raw_text,
            source="whatsapp",
            sender=sender,
            sender_whatsapp=sender_whatsapp or sender,
        )
        task = sync_result["task"]

        event.created_task = task
        event.processed = True
        event.error_message = ""
        event.save(update_fields=["created_task", "processed", "error_message"])

        try:
            send_task_creation_confirmation(
                task,
                reminders=sync_result.get("reminders"),
                target_number=sender_whatsapp or sender,
            )
        except Exception:
            pass

        return {
            "event_id": event.id,
            "task_id": task.id,
            "status": task.status,
            "workspace": task.workspace.slug,
            "priority_score": task.priority_score,
            "subtasks_created": sync_result["subtasks_created"],
            "reminders_created": sync_result.get("reminders_created", 0),
            "minutes_logged": sync_result["minutes_logged"],
        }
    except Exception as exc:
        event.error_message = str(exc)
        event.save(update_fields=["error_message"])
        raise


@shared_task(name="apps.integrations.tasks.process_gemini_webhook")
def process_gemini_webhook(event_id: int) -> dict:
    """Processa ou reprocessa um evento `gemini.sync` (JSON ou bloco multi-linha)."""
    event = WebhookEvent.objects.get(pk=event_id)
    try:
        payload = event.payload or {}
        raw_content = payload.get("raw_content") if "raw_content" in payload else payload
        sync_result = process_gemini_or_whatsapp_sync(
            raw_input=raw_content,
            source="gemini",
        )
        task = sync_result["task"]
        event.created_task = task
        event.processed = True
        event.error_message = ""
        event.save(update_fields=["created_task", "processed", "error_message"])
        return {
            "event_id": event.id,
            "task_id": task.id,
            "status": task.status,
            "workspace": task.workspace.slug,
            "subtasks_created": sync_result["subtasks_created"],
            "minutes_logged": sync_result["minutes_logged"],
        }
    except Exception as exc:
        event.error_message = str(exc)
        event.save(update_fields=["error_message"])
        raise


@shared_task(name="apps.integrations.tasks.process_github_webhook")
def process_github_webhook(event_id: int) -> dict:
    """
    Processa payload oficial de Issues ou Pull Requests do GitHub:
    - Mapeia `repository.name` ao `Project` via `github_repo_name` ou `name`.
    - Se aberta/reaberta: evita duplicidade por `external_ref` ou cria na `inbox`.
    - Se fechada/merged: conclui a `Task` (`status="done"`) e encerra qualquer `TimeLog` em execução.
    """
    event = WebhookEvent.objects.get(pk=event_id)
    try:
        payload = event.payload or {}
        action = (payload.get("action") or "opened").lower()

        if event.event_type == "ping" or action == "ping":
            event.processed = True
            event.save(update_fields=["processed"])
            return {"event_id": event.id, "status": "pong"}

        issue_data = payload.get("issue") or payload.get("pull_request") or {}
        repo_data = payload.get("repository") or {}

        number = issue_data.get("number") or payload.get("number") or payload.get("id") or "0"
        item_title = (
            issue_data.get("title")
            or payload.get("title")
            or f"GitHub Issue/PR #{number}"
        )
        item_html_url = issue_data.get("html_url") or payload.get("html_url") or ""

        repo_name = (repo_data.get("name") or payload.get("repository_name") or "").strip()
        full_name = (repo_data.get("full_name") or "").strip()
        if not repo_name and full_name:
            repo_name = full_name.split("/")[-1]

        ext_ref = payload.get("external_ref") or (
            f"{repo_name}#{number}" if repo_name else f"GH#{number}"
        )
        short_ref = f"GH#{number}"

        project = resolve_github_project(repo_name, full_name)
        workspace = project.workspace if project else resolve_workspace("saas")

        if action in ("closed", "merged"):
            ref_filter = Q(external_ref__in=[ext_ref, short_ref, str(number)])
            if project and number:
                ref_filter |= Q(project=project, external_ref__endswith=f"#{number}")

            matching_tasks = Task.objects.filter(ref_filter).exclude(status=Task.Status.DONE)

            updated_task = None
            for task in matching_tasks:
                # Parada automática do cronômetro ativo vinculado à Task
                active_logs = TimeLog.objects.filter(task=task, is_running=True)
                for log in active_logs:
                    log.stop()

                task.status = Task.Status.DONE
                task.completed_at = timezone.now()
                task.is_top_three = False
                task.save()
                updated_task = task

            event.created_task = updated_task
            event.processed = True
            event.error_message = ""
            event.save(update_fields=["created_task", "processed", "error_message"])
            return {
                "event_id": event.id,
                "action": "closed",
                "task_id": updated_task.id if updated_task else None,
                "external_ref": ext_ref,
            }

        # Caso Issue ou PR seja aberta ou reaberta (evita duplicidade)
        existing_task = Task.objects.filter(external_ref=ext_ref).first()
        if existing_task:
            existing_task.status = Task.Status.TODO
            existing_task.completed_at = None
            if project and not existing_task.project:
                existing_task.project = project
                existing_task.workspace = project.workspace
            existing_task.save()
            task = existing_task
        else:
            prefix = f"[{repo_name} #{number}]" if repo_name else f"[GitHub #{number}]"
            task = Task.objects.create(
                workspace=workspace,
                project=project,
                owner=get_default_superuser(),
                title=f"{prefix} {item_title}"[:255],
                notes=item_html_url,
                status=Task.Status.INBOX,
                shift=Task.Shift.MORNING,
                impact=4,
                urgency=4,
                external_ref=ext_ref[:120],
            )

        event.created_task = task
        event.processed = True
        event.error_message = ""
        event.save(update_fields=["created_task", "processed", "error_message"])
        return {
            "event_id": event.id,
            "action": action,
            "task_id": task.id,
            "external_ref": task.external_ref,
        }
    except Exception as exc:
        event.error_message = str(exc)
        event.save(update_fields=["error_message"])
        raise


@shared_task(name="apps.integrations.tasks.process_ecommerce_webhook")
def process_ecommerce_webhook(event_id: int) -> dict:
    """
    Processa alertas de anúncios/estoque de E-commerce (Shopee / Mercado Livre)
    e cria tarefa urgente (urgency=5) no workspace 'ecom'.
    """
    event = WebhookEvent.objects.get(pk=event_id)
    try:
        payload = event.payload or {}
        store = (payload.get("store") or "E-commerce").strip()
        sku = (payload.get("sku") or "").strip()
        issue = (payload.get("issue") or "Alerta de operação").strip()

        workspace = resolve_workspace("ecom")
        project = (
            Project.objects.filter(workspace=workspace, name__icontains=store).first()
            or Project.objects.filter(workspace=workspace, client_or_store__icontains=store).first()
            or Project.objects.filter(workspace=workspace).first()
        )

        title_prefix = f"[{store}] " if store else ""
        sku_label = f"({sku}) " if sku else ""
        full_title = f"{title_prefix}{sku_label}{issue}"[:255]

        task = Task.objects.create(
            workspace=workspace,
            project=project,
            owner=get_default_superuser(),
            title=full_title,
            status=Task.Status.INBOX,
            shift=Task.Shift.AFTERNOON,
            impact=4,
            urgency=5,
            due_date=date.today(),
            external_ref=sku[:120],
        )
        event.created_task = task
        event.processed = True
        event.error_message = ""
        event.save(update_fields=["created_task", "processed", "error_message"])
        return {
            "event_id": event.id,
            "task_id": task.id,
            "workspace": workspace.slug,
            "urgency": task.urgency,
        }
    except Exception as exc:
        event.error_message = str(exc)
        event.save(update_fields=["error_message"])
        raise
