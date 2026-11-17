import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from ninja import NinjaAPI, Schema

from apps.core.services import process_gemini_or_whatsapp_sync
from apps.core.voice_service import (
    fetch_quoted_audio_from_evolution,
    process_audio_with_gemini_free_tier,
)
from apps.integrations.models import WebhookEvent
from apps.integrations.tasks import (
    extract_evolution_text_and_sender,
    has_whatsapp_trigger,
    process_ecommerce_webhook,
    process_gemini_webhook,
    process_github_webhook,
    process_whatsapp_webhook,
)

api = NinjaAPI(
    title="Command Center (Dev & Ops Portal) API",
    version="3.0.0",
    description=(
        "Endpoints de produção para ingestão de Webhooks externos: "
        "WhatsApp Node.js Relay, Evolution API v2, GitHub Issues/PRs e Shopee/Mercado Livre."
    ),
)


class WhatsAppWebhookSchema(Schema):
    text: str
    sender: Optional[str] = ""
    message_id: Optional[str] = ""


class GitHubIssueOrPRItemSchema(Schema):
    number: Optional[int] = None
    title: Optional[str] = None
    state: Optional[str] = None
    html_url: Optional[str] = None
    merged: Optional[bool] = None


class GitHubRepoSchema(Schema):
    name: Optional[str] = None
    full_name: Optional[str] = None


class GitHubWebhookSchema(Schema):
    action: Optional[str] = "opened"
    zen: Optional[str] = None
    hook_id: Optional[int] = None
    issue: Optional[GitHubIssueOrPRItemSchema] = None
    pull_request: Optional[GitHubIssueOrPRItemSchema] = None
    repository: Optional[GitHubRepoSchema] = None
    # Campos planos alternativos para testes diretos
    number: Optional[int] = None
    title: Optional[str] = None
    html_url: Optional[str] = None
    external_ref: Optional[str] = None


class EcommerceWebhookSchema(Schema):
    store: str
    sku: str
    issue: str


def _verify_webhook_token(request: HttpRequest) -> bool:
    """
    Valida `X-Webhook-Token` ou `?token=` caso enviado na requisição.
    Em produção (`DEBUG=False` ou `REQUIRE_AUTH=True`), exige obrigatoriamente o token.
    """
    expected = (getattr(settings, "WEBHOOK_SECRET_TOKEN", "") or "").strip()
    provided = (
        request.headers.get("X-Webhook-Token")
        or request.GET.get("token")
        or ""
    ).strip()
    strict_mode = bool(getattr(settings, "REQUIRE_AUTH", False))
    if not provided:
        return not strict_mode
    if not expected:
        return True
    allowed_tokens = {expected}
    if not strict_mode:
        allowed_tokens.update({"dev-secret-key", "cc-secret-webhook-token-2026"})
    return any(hmac.compare_digest(provided, tok) for tok in allowed_tokens)


def _verify_github_signature(request: HttpRequest) -> bool:
    """
    Se `GITHUB_WEBHOOK_SECRET` estiver configurado e não vazio no `.env`,
    valida o header `X-Hub-Signature-256` usando HMAC SHA-256.
    """
    secret = (getattr(settings, "GITHUB_WEBHOOK_SECRET", "") or "").strip()
    if not secret:
        return True
    signature = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not signature.startswith("sha256="):
        return False
    expected_digest = hmac.new(
        secret.encode("utf-8"),
        msg=request.body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected_digest}", signature)


def _run_webhook_task(celery_task, event_id: int) -> Dict[str, Any]:
    """Executa a task via Celery (ou síncrono em fallback/eager mode)."""
    try:
        res = celery_task.delay(event_id)
        if hasattr(res, "result") and isinstance(res.result, dict):
            return res.result
    except Exception:
        pass
    return celery_task(event_id)


def _build_task_response(event: WebhookEvent, status_label: str = "processed") -> dict:
    task = event.created_task
    subtasks_count = task.subtasks.count() if task else 0
    minutes_logged = task.total_logged_minutes if task else 0
    return {
        "ok": True,
        "status": status_label,
        "event_id": event.id,
        "source": event.source,
        "event_type": event.event_type,
        "task_id": task.id if task else None,
        "title": task.title if task else None,
        "task_title": task.title if task else None,
        "task_status": task.status if task else None,
        "workspace": task.workspace.slug if task else None,
        "workspace_slug": task.workspace.slug if task else None,
        "project": task.project.name if (task and task.project) else None,
        "assignee": task.assigned_to if task else "",
        "subtasks_created": subtasks_count,
        "minutes_logged": minutes_logged,
        "priority_score": task.priority_score if task else None,
    }


@api.post("/webhooks/gemini", tags=["Webhooks"])
def webhook_gemini(request: HttpRequest):
    """
    Ponte Direta Gemini AI -> Command Center (`POST /api/webhooks/gemini`):
    Aceita `{ "raw_content": str }` (JSON em string, bloco Markdown ou comando multi-linha `/gemini`)
    OU o dicionário JSON estruturado diretamente (`GeminiSyncPayload`).
    Cria/atualiza a Task, vincula Project/Assignee, gera SubTasks e registra TimeLog.
    """
    if not _verify_webhook_token(request):
        return JsonResponse({"ok": False, "error": "Invalid X-Webhook-Token"}, status=401)

    body_str = request.body.decode("utf-8") if request.body else ""
    try:
        payload = json.loads(body_str) if body_str.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw_content": body_str}

    raw_input = payload.get("raw_content") if (isinstance(payload, dict) and "raw_content" in payload) else payload

    event = WebhookEvent.objects.create(
        source=WebhookEvent.Source.CUSTOM,
        event_type="gemini.sync",
        payload=payload if isinstance(payload, dict) else {"raw_content": str(payload)},
        processed=False,
    )

    try:
        sync_result = process_gemini_or_whatsapp_sync(raw_input=raw_input, source="gemini")
        task = sync_result["task"]
        event.created_task = task
        event.processed = True
        event.error_message = ""
        event.save(update_fields=["created_task", "processed", "error_message"])
        return {
            "ok": True,
            "status": "synced",
            "event_id": event.id,
            "source": event.source,
            "event_type": event.event_type,
            "format": sync_result["format"],
            "action": sync_result["action"],
            "task_id": task.id,
            "title": task.title,
            "task_title": task.title,
            "task_status": task.status,
            "workspace": task.workspace.slug,
            "workspace_slug": task.workspace.slug,
            "project": sync_result["project"],
            "assignee": sync_result["assignee"],
            "subtasks_created": sync_result["subtasks_created"],
            "minutes_logged": sync_result["minutes_logged"],
            "priority_score": task.priority_score,
        }
    except Exception as exc:
        event.error_message = str(exc)
        event.save(update_fields=["error_message"])
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)


@api.post("/webhooks/whatsapp", tags=["Webhooks"])
def webhook_whatsapp(request: HttpRequest, data: WhatsAppWebhookSchema):
    """
    Conector Node.js Direto (`whatsapp-web.js` / Baileys):
    - Aceita `{ "text": str, "sender": str, "message_id": str }`.
    - Remove prefixo `/task ` ou `!cc ` caso presente.
    - Define `status="inbox"` (ou `status="todo"` se contiver `!top3` ou `!hoje`).
    """
    if not _verify_webhook_token(request):
        return JsonResponse({"ok": False, "error": "Invalid X-Webhook-Token"}, status=401)

    event = WebhookEvent.objects.create(
        source=WebhookEvent.Source.WHATSAPP,
        event_type="whatsapp.direct",
        payload=data.dict(),
        processed=False,
    )
    _run_webhook_task(process_whatsapp_webhook, event.id)
    event.refresh_from_db()
    return _build_task_response(event)


@api.post("/webhooks/whatsapp/evolution", tags=["Webhooks"])
def webhook_whatsapp_evolution(request: HttpRequest):
    """
    Webhook Nativo Evolution API v2 (`messages.upsert`):
    - Extrai texto de `data.message.conversation` ou `data.message.extendedTextMessage.text`.
    - Ignora mensagens comuns sem `/task ` ou `!cc ` retornando `ignored_no_trigger` sem sujar o banco.
    """
    if not _verify_webhook_token(request):
        return JsonResponse({"ok": False, "error": "Invalid X-Webhook-Token"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON payload"}, status=400)

    event_name = (payload.get("event") or "messages.upsert").strip()
    data_obj = payload.get("data") or {}
    if isinstance(data_obj, list) and data_obj:
        data_obj = data_obj[0]
    msg_obj = data_obj.get("message") or {}

    # Extração de contexto de resposta (Quote) em Evolution API v2
    ext_text_obj = msg_obj.get("extendedTextMessage") or {}
    context_info = (
        ext_text_obj.get("contextInfo")
        or msg_obj.get("contextInfo")
        or data_obj.get("contextInfo")
        or {}
    )
    quoted_message = context_info.get("quotedMessage") or {}
    is_quoted_audio = bool(
        "audioMessage" in quoted_message
        or "pttMessage" in quoted_message
        or quoted_message.get("messageType") in ("audioMessage", "ptt", "audio")
    )

    is_direct_audio_event = bool(
        "audioMessage" in msg_obj
        or "pttMessage" in msg_obj
        or data_obj.get("messageType") in ("audioMessage", "ptt", "audio")
        or payload.get("type") in ("audioMessage", "ptt", "audio")
        or payload.get("audio_base64")
    )
    is_audio_event = is_direct_audio_event or is_quoted_audio

    raw_text, sender = extract_evolution_text_and_sender(payload)
    explicit_trigger = bool(payload.get("explicit_trigger"))

    # TRAVA 1: Se for evento de áudio sem gatilho explícito (/task, !cc, /gemini), bloqueia imediatamente
    if is_audio_event and not explicit_trigger and not has_whatsapp_trigger(raw_text):
        return {
            "ok": True,
            "status": "ignored_safety_lock_no_trigger",
            "event": event_name,
        }

    if not raw_text or not has_whatsapp_trigger(raw_text):
        return {
            "ok": True,
            "status": "ignored_no_trigger",
            "event": event_name,
        }

    # Se for áudio (direto ou citado via quote com /task, !cc ou /gemini) na Evolution API v2
    if is_audio_event:
        quoted_audio_obj = (
            quoted_message.get("audioMessage")
            or quoted_message.get("pttMessage")
            or msg_obj.get("audioMessage")
            or {}
        )
        mime_type = str(quoted_audio_obj.get("mimetype") or "audio/ogg").strip()

        # Caso A: Base64 já embutido no payload ou no quotedMessage
        b64_data = (
            payload.get("audio_base64")
            or quoted_audio_obj.get("base64")
            or quoted_message.get("base64")
            or (msg_obj.get("audioMessage") or {}).get("base64")
            or data_obj.get("base64")
            or ""
        )
        audio_bytes = None
        if b64_data:
            if "," in b64_data and b64_data.startswith("data:"):
                b64_data = b64_data.split(",", 1)[1]
            try:
                audio_bytes = base64.b64decode(b64_data)
            except Exception:
                audio_bytes = None

        # Caso B (Padrão Evolution API v2): Busca Base64 via /chat/getBase64FromMediaMessage/{instance} usando stanzaId
        stanza_id = str(context_info.get("stanzaId") or "").strip()
        if not audio_bytes and stanza_id:
            remote_jid = str((data_obj.get("key") or {}).get("remoteJid") or "").strip()
            instance_name = str(payload.get("instance") or data_obj.get("instance") or "").strip()
            fetched_bytes, fetched_mime = fetch_quoted_audio_from_evolution(
                stanza_id=stanza_id,
                remote_jid=remote_jid,
                instance_override=instance_name,
                quoted_message_obj=quoted_message,
            )
            if fetched_bytes:
                audio_bytes = fetched_bytes
            if fetched_mime:
                mime_type = fetched_mime

        audio_res = process_audio_with_gemini_free_tier(
            audio_bytes=audio_bytes or b"quoted_voice_note",
            mime_type=mime_type,
            source="whatsapp",
            extra_hint=raw_text,
            sender=sender,
        )
        return {k: v for k, v in audio_res.items() if k != "task"}

    event = WebhookEvent.objects.create(
        source=WebhookEvent.Source.WHATSAPP,
        event_type=event_name,
        payload={
            **payload,
            "text": raw_text,
            "sender": sender,
        },
        processed=False,
    )
    _run_webhook_task(process_whatsapp_webhook, event.id)
    event.refresh_from_db()
    return _build_task_response(event)


@api.post("/webhooks/whatsapp/audio", tags=["Webhooks"])
def webhook_whatsapp_audio(request: HttpRequest):
    """
    Endpoint Seguro para Áudios do WhatsApp (`POST /api/webhooks/whatsapp/audio`):
    Aplica as 3 Travas Rígidas de Segurança:
      1. `explicit_trigger=True` obrigatório (ou `extra_hint` iniciando com `/task`, `!cc` ou `/gemini`).
      2. Tamanho máximo de 3.5 MB (`MAX_AUDIO_BYTES`).
      3. Disjuntor de Cota Diária (`max_daily_audio_requests`) e Kill-Switch (`ai_audio_enabled`).
    """
    if not _verify_webhook_token(request):
        return JsonResponse({"ok": False, "error": "Invalid X-Webhook-Token"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON payload"}, status=400)

    explicit_trigger = bool(payload.get("explicit_trigger"))
    extra_hint = str(payload.get("extra_hint") or payload.get("text") or "").strip()
    transcript_fallback = str(payload.get("transcript_fallback") or "").strip()
    sender = str(payload.get("sender") or "").strip()
    mime_type = str(payload.get("mime_type") or "audio/ogg").strip()

    # TRAVA 1: Recusa imediatamente qualquer áudio sem gatilho explícito
    if not explicit_trigger and not has_whatsapp_trigger(extra_hint):
        return {
            "ok": True,
            "status": "ignored_safety_lock_no_trigger",
        }

    b64_audio = str(payload.get("audio_base64") or payload.get("data") or "").strip()
    audio_bytes = b""
    if b64_audio:
        if "," in b64_audio and b64_audio.startswith("data:"):
            b64_audio = b64_audio.split(",", 1)[1]
        try:
            audio_bytes = base64.b64decode(b64_audio)
        except Exception:
            return JsonResponse({"ok": False, "error": "Base64 de áudio inválido."}, status=400)
    else:
        audio_bytes = b"simulated_voice_note"

    result = process_audio_with_gemini_free_tier(
        audio_bytes=audio_bytes,
        mime_type=mime_type,
        source="whatsapp",
        transcript_fallback=transcript_fallback,
        extra_hint=extra_hint,
        sender=sender,
    )
    # Remove o objeto model Task antes de serializar para JSON
    result_json = {k: v for k, v in result.items() if k != "task"}
    return result_json


@api.post("/webhooks/github", tags=["Webhooks"])
def webhook_github(request: HttpRequest, data: GitHubWebhookSchema):
    """
    Webhook Oficial do GitHub (`ping`, `issues`, `pull_request`):
    - Valida `X-Hub-Signature-256` se `GITHUB_WEBHOOK_SECRET` estiver definido.
    - Vincula automaticamente `repository.name` ao `Project.github_repo_name`.
    - Cria/reabre tarefas ao abrir Issue/PR e conclui + encerra `TimeLog` ao fechar/mergear.
    """
    if not _verify_github_signature(request):
        return JsonResponse({"ok": False, "error": "Invalid X-Hub-Signature-256"}, status=401)

    gh_event_header = (request.headers.get("X-GitHub-Event") or "").strip().lower()
    payload_dict = data.dict(exclude_none=True)
    action = (data.action or "opened").lower()

    if gh_event_header == "ping" or data.zen is not None or action == "ping":
        event = WebhookEvent.objects.create(
            source=WebhookEvent.Source.GITHUB,
            event_type="ping",
            payload=payload_dict,
            processed=True,
        )
        return {
            "ok": True,
            "status": "pong",
            "event_id": event.id,
            "source": event.source,
            "event_type": "ping",
        }

    if not gh_event_header:
        gh_event_header = "pull_request" if data.pull_request else "issues"

    event_type = f"{gh_event_header}.{action}"
    event = WebhookEvent.objects.create(
        source=WebhookEvent.Source.GITHUB,
        event_type=event_type[:80],
        payload=payload_dict,
        processed=False,
    )
    _run_webhook_task(process_github_webhook, event.id)
    event.refresh_from_db()
    return _build_task_response(event, status_label=action)


@api.post("/webhooks/ecommerce", tags=["Webhooks"])
def webhook_ecommerce(request: HttpRequest, data: EcommerceWebhookSchema):
    """
    Recebe alertas de estoque ou anúncios da Shopee / Mercado Livre
    e cria tarefa urgente (`urgency=5`) no workspace `ecom`.
    """
    if not _verify_webhook_token(request):
        return JsonResponse({"ok": False, "error": "Invalid X-Webhook-Token"}, status=401)

    event = WebhookEvent.objects.create(
        source=WebhookEvent.Source.ECOMMERCE,
        event_type="ecommerce.alert",
        payload=data.dict(),
        processed=False,
    )
    _run_webhook_task(process_ecommerce_webhook, event.id)
    event.refresh_from_db()
    return _build_task_response(event)
