import base64
import json
import re
import urllib.error
import urllib.request
from typing import Optional

from django.conf import settings
from django.utils import timezone

from apps.core.models import Project, SystemIntegrationConfig, TeamMember, Workspace
from apps.core.services import process_gemini_or_whatsapp_sync
from apps.integrations.models import WebhookEvent
from apps.tasks.models import SubTask, Task

# TRAVA 2: Limite rígido de 3.5 MB por arquivo/payload de áudio (~2 minutos de voz)
MAX_AUDIO_BYTES = 3_500_000


def fetch_quoted_audio_from_evolution(
    stanza_id: str,
    remote_jid: str = "",
    instance_override: str = "",
    quoted_message_obj: Optional[dict] = None,
) -> tuple[Optional[bytes], str]:
    """
    Phase 7.1: Busca automaticamente o binário Base64 de um áudio citado (quote) na Evolution API v2
    via `POST /chat/getBase64FromMediaMessage/{instance}` quando o usuário responde ao áudio com
    `!cc`, `/task` ou `/gemini`.
    Caso a chamada de rede falhe ou não esteja configurada, retorna `(None, "audio/ogg")` sem quebrar o webhook.
    """
    if not stanza_id:
        return None, "audio/ogg"

    config = SystemIntegrationConfig.get_solo()
    api_url = (
        (config.evolution_api_url or "").strip()
        or getattr(settings, "EVOLUTION_API_URL", "http://127.0.0.1:8080").strip()
    )
    api_key = (
        (config.evolution_api_key or "").strip()
        or getattr(settings, "EVOLUTION_API_KEY", "").strip()
    )
    instance_name = (
        (instance_override or "").strip()
        or (config.evolution_instance_name or "").strip()
        or getattr(settings, "EVOLUTION_INSTANCE_NAME", "").strip()
        or "default"
    )

    if not api_url or not api_key:
        return None, "audio/ogg"

    endpoint = f"{api_url.rstrip('/')}/chat/getBase64FromMediaMessage/{instance_name}"
    body = {
        "message": {
            "key": {
                "id": stanza_id,
                "remoteJid": remote_jid or "",
            },
            "message": quoted_message_obj or {},
        },
        "convertToMp4": False,
    }

    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "apikey": api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            b64_str = str(resp_data.get("base64") or "").strip()
            mime_type = str(
                resp_data.get("mimetype")
                or resp_data.get("mimeType")
                or "audio/ogg"
            ).strip()
            if not b64_str:
                return None, mime_type
            if "," in b64_str and b64_str.startswith("data:"):
                b64_str = b64_str.split(",", 1)[1]
            return base64.b64decode(b64_str), mime_type
    except Exception:
        return None, "audio/ogg"



def get_daily_audio_quota_status() -> dict:
    """
    TRAVA 3: Retorna o estado atual do Disjuntor de Cota Diária e Kill-Switch de Áudio IA.
    Conta quantos eventos `audio.voice_note` foram processados na data de hoje.
    """
    config = SystemIntegrationConfig.get_solo()
    local_now = timezone.localtime(timezone.now())
    start_of_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    used_today = WebhookEvent.objects.filter(
        event_type="audio.voice_note",
        processed=True,
        created_at__gte=start_of_today,
    ).count()
    max_daily = max(1, int(config.max_daily_audio_requests or 20))
    remaining_today = max(0, max_daily - used_today)
    quota_pct = min(100, int(round((used_today / max_daily) * 100)))

    return {
        "config": config,
        "used_today": used_today,
        "max_daily": max_daily,
        "remaining_today": remaining_today,
        "quota_pct": quota_pct,
        "ai_audio_enabled": bool(config.ai_audio_enabled),
        "has_api_key": bool((config.gemini_api_key or "").strip()),
        "is_quota_exceeded": used_today >= max_daily,
    }


def normalize_spoken_command_ptbr(spoken_text: str) -> str:
    """
    Converte fala natural em português (Ditado Nativo Web Speech API - Custo Zero)
    para a sintaxe estruturada do Command Parser (`#workspace`, `@handle`, `!top3`, `!hoje`, `/turno`).
    """
    text = (spoken_text or "").strip()
    if not text:
        return ""

    # 1. Substituição dinâmica de Frentes (Workspaces) cadastradas no banco
    for ws in Workspace.objects.all():
        patterns = [
            rf"\bfrente\s+{re.escape(ws.name)}\b",
            rf"\bfrente\s+{re.escape(ws.slug)}\b",
            rf"\bhashtag\s+{re.escape(ws.slug)}\b",
            rf"\bhashtag\s+{re.escape(ws.name)}\b",
            rf"\btag\s+{re.escape(ws.slug)}\b",
        ]
        for pat in patterns:
            text = re.sub(pat, f"#{ws.slug}", text, flags=re.IGNORECASE)

    # 2. Substituição dinâmica de Responsáveis (TeamMembers) ativos no banco
    for member in TeamMember.objects.filter(is_active=True):
        first_name = member.name.split()[0] if member.name else member.handle
        patterns = [
            rf"\brespons[aá]vel\s+{re.escape(member.name)}\b",
            rf"\brespons[aá]vel\s+{re.escape(first_name)}\b",
            rf"\brespons[aá]vel\s+{re.escape(member.handle)}\b",
            rf"\batribuir\s+para\s+{re.escape(member.name)}\b",
            rf"\batribuir\s+para\s+{re.escape(first_name)}\b",
            rf"\batribuir\s+{re.escape(member.name)}\b",
            rf"\batribuir\s+{re.escape(first_name)}\b",
            rf"\batribuir\s+{re.escape(member.handle)}\b",
            rf"\barroba\s+{re.escape(member.handle)}\b",
            rf"\barroba\s+{re.escape(first_name)}\b",
        ]
        for pat in patterns:
            text = re.sub(pat, f"@{member.handle}", text, flags=re.IGNORECASE)

    # 3. Substituição de prioridades e prazos falados
    text = re.sub(
        r"(?<!!)\b(?:top\s*3|top\s*tr[eê]s|prioridade\s+m[aá]xima)\b",
        "!top3",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<!!)\b(?:para\s+hoje|urgente\s+hoje|vence\s+hoje|fazer\s+hoje)\b",
        "!hoje",
        text,
        flags=re.IGNORECASE,
    )

    # 4. Substituição de turnos falados
    text = re.sub(
        r"\b(?:turno\s+da\s+manh[aã]|de\s+manh[aã]|pela\s+manh[aã])\b",
        "/manha",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:turno\s+da\s+tarde|[aà]\s+tarde|de\s+tarde|pela\s+tarde)\b",
        "/tarde",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:turno\s+da\s+noite|[aà]\s+noite|de\s+noite|pela\s+noite)\b",
        "/noite",
        text,
        flags=re.IGNORECASE,
    )

    return re.sub(r"\s+", " ", text).strip()


def _call_gemini_multimodal_audio_api(
    api_key: str,
    audio_bytes: bytes,
    mime_type: str,
    extra_hint: str = "",
) -> Optional[dict]:
    """
    Envia o áudio codificado em Base64 para o endpoint REST oficial do Gemini 2.5 Flash
    (`responseMimeType: application/json`), contextualizado com as Frentes, Projetos e Equipe.
    """
    ws_list = ", ".join([f"{w.slug} ({w.name})" for w in Workspace.objects.all()])
    proj_list = ", ".join([f"{p.name} [#{p.workspace.slug}]" for p in Project.objects.filter(is_active=True)])
    team_list = ", ".join([f"@{m.handle} ({m.name})" for m in TeamMember.objects.filter(is_active=True)])

    prompt = (
        "Você é o assistente de operações do Command Center. Ouça este áudio e extraia uma tarefa "
        "estruturada em JSON estrito com as chaves: "
        '{"action": "upsert", "title": "...", "workspace": "...", "project": "...", '
        '"assignee": "...", "shift": "morning|afternoon|night", "impact": 1..5, "urgency": 1..5, '
        '"is_top_three": false, "minutes_spent": 0, "notes": "Resumo do áudio...", '
        '"subtasks": ["Passo 1", "Passo 2"]}.\n'
        f"Workspaces disponíveis: {ws_list}.\n"
        f"Projetos disponíveis: {proj_list}.\n"
        f"Equipe disponível: {team_list}.\n"
    )
    if extra_hint:
        prompt += f"Dica/Tags enviadas pelo usuário junto ao áudio: {extra_hint}\n"

    b64_audio = base64.b64encode(audio_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type or "audio/ogg",
                            "data": b64_audio,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))
        candidates = resp_data.get("candidates") or []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            if parts and "text" in parts[0]:
                return json.loads(parts[0]["text"])
    return None


def _default_subtasks_for_task(task: Task) -> list[str]:
    """Gera subtarefas técnicas inteligentes de fallback quando a chave de API não está definida."""
    slug = (task.workspace.slug or "").lower()
    if slug == "ecom":
        return [
            f"Conferir estoque e margem do item ({task.title[:45]})",
            "Validar preço e campanha ativa no painel do marketplace",
        ]
    if slug == "wsa":
        return [
            f"Alinhar escopo e entregáveis: {task.title[:45]}",
            "Validar homologação e publicar atualização para o cliente",
        ]
    return [
        f"Mapear requisitos técnicos para: {task.title[:45]}",
        "Implementar solução e validar testes automatizados",
    ]


def process_audio_with_gemini_free_tier(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    source: str = "web",
    transcript_fallback: str = "",
    extra_hint: str = "",
    current_workspace_slug: Optional[str] = None,
    sender: str = "",
    owner=None,
) -> dict:
    """
    Processa um áudio (do Gravador Web ou de um Quote explícito no WhatsApp) aplicando:
      - TRAVA 2: Tamanho máximo de 3.5 MB (`MAX_AUDIO_BYTES`).
      - TRAVA 3: Kill-Switch (`ai_audio_enabled`) e Disjuntor de Cota Diária (`max_daily_audio_requests`).
    """
    # TRAVA 2: Validação estrita de tamanho máximo (3.5 MB)
    if audio_bytes and len(audio_bytes) > MAX_AUDIO_BYTES:
        return {
            "ok": False,
            "blocked": True,
            "status": "blocked_payload_too_large",
            "error": "Áudio excede o limite de segurança de 3.5MB",
        }

    # TRAVA 3: Verificação do Kill-Switch e Disjuntor de Cota Diária
    quota = get_daily_audio_quota_status()
    if not quota["ai_audio_enabled"]:
        return {
            "ok": False,
            "blocked": True,
            "status": "blocked_kill_switch_disabled",
            "error": "Processamento de Áudio IA está pausado no Kill-Switch de segurança.",
            "used_today": quota["used_today"],
            "max_daily": quota["max_daily"],
        }

    if quota["is_quota_exceeded"]:
        return {
            "ok": False,
            "blocked": True,
            "status": "blocked_daily_quota_exceeded",
            "error": (
                f"Cota diária de segurança atingida ({quota['used_today']}/{quota['max_daily']} áudios hoje). "
                "Use o Ditado Nativo gratuito ou aumente o limite na aba Integrações."
            ),
            "used_today": quota["used_today"],
            "max_daily": quota["max_daily"],
        }

    api_key = (quota["config"].gemini_api_key or "").strip()
    gemini_json = None
    engine_mode = "local_fallback"

    if api_key and audio_bytes:
        try:
            gemini_json = _call_gemini_multimodal_audio_api(
                api_key=api_key,
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                extra_hint=extra_hint or transcript_fallback,
            )
            if gemini_json:
                engine_mode = "gemini_2.5_flash_audio"
        except Exception:
            gemini_json = None

    if gemini_json:
        sync_result = process_gemini_or_whatsapp_sync(
            raw_input=gemini_json,
            source=source,
            current_workspace_slug=current_workspace_slug,
            sender=sender,
            owner=owner,
        )
    else:
        raw_spoken = (transcript_fallback or extra_hint or "").strip()
        normalized_text = normalize_spoken_command_ptbr(raw_spoken)
        if not normalized_text:
            normalized_text = "Revisar nota de voz capturada no Command Center"

        sync_result = process_gemini_or_whatsapp_sync(
            raw_input=normalized_text,
            source=source,
            current_workspace_slug=current_workspace_slug,
            sender=sender,
            owner=owner,
        )
        task = sync_result["task"]
        if not task.notes:
            task.notes = f"[Áudio processado via {engine_mode}] Dica/Transcrição: {normalized_text}"
            task.save(update_fields=["notes"])

        if sync_result["subtasks_created"] == 0:
            default_sts = _default_subtasks_for_task(task)
            for st_title in default_sts:
                SubTask.objects.create(task=task, title=st_title[:200], is_done=False)
            sync_result["subtasks_created"] = len(default_sts)

    task = sync_result["task"]
    ev_source = (
        WebhookEvent.Source.WHATSAPP
        if source == "whatsapp"
        else WebhookEvent.Source.CUSTOM
    )
    event = WebhookEvent.objects.create(
        source=ev_source,
        event_type="audio.voice_note",
        payload={
            "source": source,
            "mime_type": mime_type,
            "audio_size_bytes": len(audio_bytes) if audio_bytes else 0,
            "extra_hint": extra_hint,
            "transcript_fallback": transcript_fallback,
            "engine_mode": engine_mode,
        },
        processed=True,
        created_task=task,
    )

    updated_quota = get_daily_audio_quota_status()
    return {
        "ok": True,
        "blocked": False,
        "status": "processed_audio",
        "engine_mode": engine_mode,
        "event_id": event.id,
        "task": task,
        "task_id": task.id,
        "title": task.title,
        "workspace": task.workspace.slug,
        "project": sync_result["project"],
        "assignee": sync_result["assignee"],
        "subtasks_created": sync_result["subtasks_created"],
        "minutes_logged": sync_result["minutes_logged"],
        "priority_score": task.priority_score,
        "used_today": updated_quota["used_today"],
        "max_daily": updated_quota["max_daily"],
        "quota": {
            "used_today": updated_quota["used_today"],
            "max_daily": updated_quota["max_daily"],
            "remaining_today": updated_quota["remaining_today"],
        },
    }


def generate_subtasks_with_gemini(task: Task) -> dict:
    """
    Quebra uma `Task` existente em 3 a 5 `SubTasks` técnicas ao clicar no botão
    `⚡ Quebrar com IA` dentro do Slide-Over Drawer (respeitando a trava de cota diária).
    """
    quota = get_daily_audio_quota_status()
    if not quota["ai_audio_enabled"]:
        return {
            "ok": False,
            "error": "Motor IA pausado no Kill-Switch de segurança.",
            "created_count": 0,
        }
    if quota["is_quota_exceeded"]:
        return {
            "ok": False,
            "error": f"Cota diária de segurança atingida ({quota['used_today']}/{quota['max_daily']}).",
            "created_count": 0,
        }

    api_key = (quota["config"].gemini_api_key or "").strip()
    subtask_titles = []
    mode = "local_smart_breakdown"

    if api_key:
        try:
            prompt = (
                "Gere de 3 a 5 subtarefas técnicas, diretas e acionáveis para a seguinte tarefa:\n"
                f"Título: {task.title}\n"
                f"Frente: {task.workspace.name} (#{task.workspace.slug})\n"
                f"Projeto: {task.project.name if task.project else 'Geral'}\n"
                f"Notas: {task.notes or 'Sem notas adicionais'}\n"
                'Retorne estritamente um JSON no formato: {"subtasks": ["Passo 1", "Passo 2", "Passo 3"]}'
            )
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.25,
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                candidates = resp_data.get("candidates") or []
                if candidates:
                    parts = (candidates[0].get("content") or {}).get("parts") or []
                    if parts and "text" in parts[0]:
                        parsed = json.loads(parts[0]["text"])
                        subtask_titles = parsed.get("subtasks") or []
                        mode = "gemini_2.5_flash"
        except Exception:
            subtask_titles = []

    if not subtask_titles:
        subtask_titles = [
            f"Levantar requisitos e contexto de: {task.title[:45]}",
            f"Executar implementação principal em #{task.workspace.slug}",
            "Realizar revisão de qualidade e validar entrega",
        ]

    existing_titles = {
        s.title.lower().strip() for s in task.subtasks.all()
    }
    created = []
    for st_raw in subtask_titles[:5]:
        clean_title = str(st_raw or "").strip()[:200]
        if clean_title and clean_title.lower() not in existing_titles:
            st = SubTask.objects.create(task=task, title=clean_title, is_done=False)
            created.append(st)
            existing_titles.add(clean_title.lower())

    return {
        "ok": True,
        "mode": mode,
        "created_count": len(created),
        "subtasks": created,
    }
