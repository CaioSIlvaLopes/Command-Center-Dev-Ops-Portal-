import json
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from django.conf import settings
from django.utils import timezone

from apps.core.models import SystemIntegrationConfig


def normalize_whatsapp_number(raw: Any) -> str:
    """
    Remove sufixos do WhatsApp (`@s.whatsapp.net`, `@lid`, `@c.us`),
    remove caracteres não numéricos e retorna apenas dígitos válidos (8 a 20 dígitos) ou `""`.
    """
    if not raw:
        return ""
    raw_str = str(raw).strip()
    if "@" in raw_str:
        raw_str = raw_str.split("@", 1)[0]
    digits = "".join(ch for ch in raw_str if ch.isdigit())
    if len(digits) < 8:
        return ""
    return digits[:30]


def resolve_target_whatsapp(task: Any, fallback_sender: Any = None) -> str:
    """
    Resolve o número de WhatsApp de destino para confirmações e lembretes:
      1. `normalize_whatsapp_number(fallback_sender)` (se informado e válido);
      2. `task.owner.team_profile.whatsapp_number` (se existir);
      3. `task.assignee.whatsapp_number` (se existir);
      4. `SystemIntegrationConfig.get_solo().default_whatsapp_number`.
    """
    sender_num = normalize_whatsapp_number(fallback_sender)
    if sender_num:
        return sender_num

    if task is not None:
        owner = getattr(task, "owner", None) if getattr(task, "owner_id", None) else None
        if owner and hasattr(owner, "team_profile") and owner.team_profile:
            owner_num = normalize_whatsapp_number(owner.team_profile.whatsapp_number)
            if owner_num:
                return owner_num

        assignee = getattr(task, "assignee", None) if getattr(task, "assignee_id", None) else None
        if assignee:
            assignee_num = normalize_whatsapp_number(assignee.whatsapp_number)
            if assignee_num:
                return assignee_num

    config = SystemIntegrationConfig.get_solo()
    default_num = normalize_whatsapp_number(config.default_whatsapp_number)
    if default_num:
        return default_num

    return ""


def send_whatsapp_text(number: str, text: str) -> dict:
    """
    Envia uma mensagem de texto ativa via Evolution API v2:
    `POST {evolution_api_url.rstrip('/')}/message/sendText/{evolution_instance_name}`
    com header `apikey` e body `{"number": "<numero_limpo>", "text": "<mensagem>"}`.
    Nunca lança exceção para não interromper o fluxo de criação de tarefas.
    """
    clean_number = normalize_whatsapp_number(number)
    clean_text = (text or "").strip()
    config = SystemIntegrationConfig.get_solo()

    if not config.whatsapp_notifications_enabled:
        return {"ok": False, "reason": "disabled_or_unconfigured"}

    api_url = (
        (config.evolution_api_url or "").strip()
        or getattr(settings, "EVOLUTION_API_URL", "").strip()
    )
    api_key = (
        (config.evolution_api_key or "").strip()
        or getattr(settings, "EVOLUTION_API_KEY", "").strip()
    )
    instance_name = (
        (config.evolution_instance_name or "").strip()
        or getattr(settings, "EVOLUTION_INSTANCE_NAME", "").strip()
    )

    if not clean_number or not clean_text or not api_url or not api_key or not instance_name:
        return {"ok": False, "reason": "disabled_or_unconfigured"}

    endpoint = f"{api_url.rstrip('/')}/message/sendText/{instance_name}"
    payload = {
        "number": clean_number,
        "text": clean_text,
    }

    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "apikey": api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_body = resp.read().decode("utf-8") if resp else "{}"
            try:
                resp_data = json.loads(raw_body) if raw_body else {}
            except Exception:
                resp_data = {"raw": raw_body}
            return {
                "ok": True,
                "number": clean_number,
                "endpoint": endpoint,
                "response": resp_data,
            }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "request_error",
            "number": clean_number,
            "error": str(exc),
        }


def send_task_creation_confirmation(
    task: Any,
    reminders: Optional[Iterable[Any]] = None,
    target_number: Optional[str] = None,
) -> dict:
    """
    Formata e envia uma confirmação objetiva em PT-BR no WhatsApp após criar/sincronizar
    uma tarefa via Evolution API v2 (texto ou áudio citado), listando também os lembretes agendados.
    """
    if task is None:
        return {"ok": False, "reason": "no_task"}

    resolved_number = resolve_target_whatsapp(task, fallback_sender=target_number)
    if not resolved_number:
        return {"ok": False, "reason": "disabled_or_unconfigured"}

    top3_badge = " [★ TOP 3]" if getattr(task, "is_top_three", False) else ""
    shift_label = task.get_shift_display() if hasattr(task, "get_shift_display") else getattr(task, "shift", "Manhã")
    ws_slug = task.workspace.slug if getattr(task, "workspace", None) else "geral"

    lines = [
        f"✅ *Tarefa Registrada — Command Center*{top3_badge}",
        f"• *Título:* {task.title}",
        f"• *Frente:* #{ws_slug} | *Turno:* {shift_label}",
        f"• *Prioridade:* Score {task.priority_score}/25",
    ]
    if getattr(task, "assigned_to", ""):
        lines.append(f"• *Responsável:* {task.assigned_to}")

    reminder_list = (
        list(reminders)
        if reminders is not None
        else list(task.reminders.filter(is_active=True)) if hasattr(task, "reminders") else []
    )
    if reminder_list:
        lines.append("")
        lines.append("⏰ *Lembretes Agendados:*")
        for rem in reminder_list:
            local_dt = timezone.localtime(rem.remind_at) if timezone.is_aware(rem.remind_at) else rem.remind_at
            repeat_str = (
                f" (repete a cada {rem.repeat_interval_minutes} min)"
                if getattr(rem, "repeat_interval_minutes", None)
                else ""
            )
            note_str = f" — {rem.custom_message}" if getattr(rem, "custom_message", "") else ""
            lines.append(f"• ⏰ {local_dt:%d/%m às %H:%M}{repeat_str}{note_str}")

    return send_whatsapp_text(resolved_number, "\n".join(lines))
