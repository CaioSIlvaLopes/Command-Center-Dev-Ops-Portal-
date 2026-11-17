import json
from datetime import date
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse

from apps.core.models import Workspace, Project, TeamMember
from apps.core.services import parse_and_create_task
from apps.tasks.models import Task, SubTask
from apps.timetracker.models import TimeLog
from apps.integrations.models import WebhookEvent


class CommandCenterTests(TestCase):
    def setUp(self):
        call_command("seed_command_center")
        self.client = Client()

    def test_seed_command_center_created_all_entities_and_phase3_repos(self):
        self.assertEqual(Workspace.objects.count(), 5)
        self.assertEqual(Project.objects.count(), 6)
        self.assertEqual(Task.objects.count(), 8)
        self.assertEqual(Task.objects.filter(is_top_three=True).count(), 3)
        self.assertGreater(SubTask.objects.count(), 0)
        self.assertGreater(TimeLog.objects.filter(is_running=False).count(), 0)

        deskify = Project.objects.get(name="Deskify")
        self.assertEqual(deskify.github_repo_name, "Sistema-de-Chamado")
        mscrew = Project.objects.get(name="Portal MS CREW")
        self.assertEqual(mscrew.github_repo_name, "MSCREW-Site")
        datemate = Project.objects.get(name="Date & Mate")
        self.assertEqual(datemate.github_repo_name, "date-and-mate")
        northvale = Project.objects.get(name="NorthVale")
        self.assertEqual(northvale.github_repo_name, "NorthVale")

    def test_command_parser_extracts_all_tags(self):
        task = parse_and_create_task(
            "Revisar campanha de remarketing no Portal MS CREW #wsa @henrique /tarde !hoje !top3"
        )
        self.assertEqual(task.workspace.slug, "wsa")
        self.assertEqual(task.project.name, "Portal MS CREW")
        self.assertEqual(task.assigned_to, "@henrique")
        self.assertEqual(task.shift, Task.Shift.AFTERNOON)
        self.assertEqual(task.due_date, date.today())
        self.assertEqual(task.impact, 5)
        self.assertEqual(task.urgency, 5)
        self.assertEqual(task.priority_score, 25)
        self.assertTrue(task.is_top_three)
        self.assertEqual(task.title, "Revisar campanha de remarketing no Portal MS CREW")
        self.assertEqual(
            Task.objects.filter(is_top_three=True).exclude(status=Task.Status.DONE).count(),
            3,
        )

    def test_subtasks_notes_and_detail_drawer(self):
        task = Task.objects.first()
        resp_drawer = self.client.get(reverse("tasks:task_detail", args=[task.id]))
        self.assertEqual(resp_drawer.status_code, 200)
        self.assertIn(task.title, resp_drawer.content.decode("utf-8"))

        initial_count = task.subtasks.count()
        resp_add = self.client.post(
            reverse("tasks:subtask_add", args=[task.id]),
            {"title": "Executar bateria de regressão E2E"},
        )
        self.assertEqual(resp_add.status_code, 200)
        self.assertEqual(task.subtasks.count(), initial_count + 1)

        new_st = task.subtasks.last()
        resp_toggle = self.client.post(reverse("tasks:subtask_toggle", args=[new_st.id]))
        self.assertEqual(resp_toggle.status_code, 200)
        new_st.refresh_from_db()
        self.assertTrue(new_st.is_done)

        resp_update = self.client.post(
            reverse("tasks:update_detail", args=[task.id]),
            {
                "title": task.title,
                "workspace_id": task.workspace_id,
                "project_id": task.project_id or "",
                "shift": "afternoon",
                "status": "doing",
                "impact": 5,
                "urgency": 4,
                "assigned_to": "@pedro",
                "notes": "Especificação técnica atualizada",
            },
        )
        self.assertEqual(resp_update.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.notes, "Especificação técnica atualizada")
        self.assertEqual(task.priority_score, 20)

        resp_del = self.client.post(reverse("tasks:subtask_delete", args=[new_st.id]))
        self.assertEqual(resp_del.status_code, 200)
        self.assertEqual(task.subtasks.count(), initial_count)

    def test_sortable_move_endpoint_updates_shift_and_status(self):
        task = Task.objects.filter(status=Task.Status.TODO).first()
        resp = self.client.post(
            reverse("tasks:move_task", args=[task.id]),
            {"shift": "night", "status": "doing"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.shift, Task.Shift.NIGHT)
        self.assertEqual(task.status, Task.Status.DOING)

    def test_analytics_view_and_manual_time_logging(self):
        task = Task.objects.first()
        before_mins = task.total_logged_minutes

        resp_manual = self.client.post(
            reverse("timetracker:add_manual", args=[task.id]),
            {"minutes": "30"},
        )
        self.assertEqual(resp_manual.status_code, 200)
        task_reloaded = Task.objects.prefetch_related("time_logs").get(pk=task.id)
        self.assertEqual(task_reloaded.total_logged_minutes, before_mins + 30)

        resp_analytics = self.client.get("/?view=analytics")
        self.assertEqual(resp_analytics.status_code, 200)
        self.assertIn("analytics-view-section", resp_analytics.content.decode("utf-8"))

        last_log = TimeLog.objects.filter(task=task, is_running=False).first()
        resp_del_log = self.client.post(reverse("timetracker:delete_log", args=[last_log.id]))
        self.assertEqual(resp_del_log.status_code, 200)
        self.assertFalse(TimeLog.objects.filter(pk=last_log.id).exists())

    def test_evolution_api_webhook_creates_task_and_ignores_non_trigger_messages(self):
        events_before = WebhookEvent.objects.count()
        tasks_before = Task.objects.count()

        # 1. Mensagem comum sem prefixo /task ou !cc deve ser ignorada sem sujar o banco
        normal_payload = {
            "event": "messages.upsert",
            "data": {
                "pushName": "Cliente",
                "message": {"conversation": "Bom dia, tudo bem?"},
            },
        }
        resp_ignored = self.client.post(
            "/api/webhooks/whatsapp/evolution",
            data=json.dumps(normal_payload),
            content_type="application/json",
        )
        self.assertEqual(resp_ignored.status_code, 200)
        self.assertEqual(resp_ignored.json()["status"], "ignored_no_trigger")
        self.assertEqual(WebhookEvent.objects.count(), events_before)
        self.assertEqual(Task.objects.count(), tasks_before)

        # 2. Mensagem com prefixo /task deve criar a Task e o WebhookEvent
        command_payload = {
            "event": "messages.upsert",
            "data": {
                "pushName": "Caio",
                "message": {
                    "extendedTextMessage": {
                        "text": "/task Otimizar cache no Portal MS CREW #wsa @henrique /tarde !hoje"
                    }
                },
            },
        }
        resp_created = self.client.post(
            "/api/webhooks/whatsapp/evolution",
            data=json.dumps(command_payload),
            content_type="application/json",
        )
        self.assertEqual(resp_created.status_code, 200)
        data = resp_created.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["workspace"], "wsa")
        created_task = Task.objects.get(pk=data["task_id"])
        self.assertEqual(created_task.title, "Otimizar cache no Portal MS CREW")
        # Como continha !hoje, já entra direto como todo
        self.assertEqual(created_task.status, Task.Status.TODO)
        self.assertEqual(created_task.project.name, "Portal MS CREW")

    def test_github_webhook_full_lifecycle_auto_project_and_timer_stop(self):
        # 1. Ping event
        resp_ping = self.client.post(
            "/api/webhooks/github",
            data=json.dumps({"zen": "Keep it logically awesome."}),
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="ping",
        )
        self.assertEqual(resp_ping.status_code, 200)
        self.assertEqual(resp_ping.json()["status"], "pong")

        # 2. Abrir Issue #42 no repositório Sistema-de-Chamado (vinculado ao Deskify)
        open_payload = {
            "action": "opened",
            "repository": {
                "name": "Sistema-de-Chamado",
                "full_name": "caio/Sistema-de-Chamado",
            },
            "issue": {
                "number": 42,
                "title": "Corrigir deadlock na fila de tickets urgentes",
                "html_url": "https://github.com/caio/Sistema-de-Chamado/issues/42",
            },
        }
        resp_open = self.client.post(
            "/api/webhooks/github",
            data=json.dumps(open_payload),
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="issues",
        )
        self.assertEqual(resp_open.status_code, 200)
        task_id = resp_open.json()["task_id"]
        gh_task = Task.objects.get(pk=task_id)
        self.assertEqual(gh_task.project.name, "Deskify")
        self.assertEqual(gh_task.workspace.slug, "saas")
        self.assertEqual(gh_task.external_ref, "Sistema-de-Chamado#42")
        self.assertEqual(gh_task.status, Task.Status.INBOX)
        self.assertEqual(gh_task.notes, "https://github.com/caio/Sistema-de-Chamado/issues/42")

        # 3. Iniciar um TimeLog ativo nessa Task (is_running=True)
        self.client.post(reverse("timetracker:start", args=[gh_task.id]))
        self.assertTrue(TimeLog.objects.filter(task=gh_task, is_running=True).exists())

        # 4. Fechar a Issue/PR #42 no repositório Sistema-de-Chamado
        close_payload = {
            "action": "closed",
            "repository": {
                "name": "Sistema-de-Chamado",
                "full_name": "caio/Sistema-de-Chamado",
            },
            "pull_request": {
                "number": 42,
                "title": "Corrigir deadlock na fila de tickets urgentes",
                "html_url": "https://github.com/caio/Sistema-de-Chamado/pull/42",
                "merged": True,
            },
        }
        resp_close = self.client.post(
            "/api/webhooks/github",
            data=json.dumps(close_payload),
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(resp_close.status_code, 200)

        # Verifica se a Task mudou para done e se o TimeLog foi encerrado
        gh_task.refresh_from_db()
        self.assertEqual(gh_task.status, Task.Status.DONE)
        self.assertIsNotNone(gh_task.completed_at)
        self.assertFalse(TimeLog.objects.filter(task=gh_task, is_running=True).exists())
        stopped_log = TimeLog.objects.filter(task=gh_task, is_running=False).first()
        self.assertIsNotNone(stopped_log)
        self.assertIsNotNone(stopped_log.end_time)

    def test_integrations_view_reprocess_and_no_forbidden_colors(self):
        forbidden_tokens = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for url in (
            "/",
            "/?view=kanban",
            "/?view=analytics",
            "/?view=integrations",
            "/?view=styleguide",
            "/?view=settings",
            "/?workspace=ecom",
        ):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode("utf-8").lower()
            for token in forbidden_tokens:
                self.assertNotIn(token, html)

        # Reprocessar um WebhookEvent pela view HTMX
        wh = WebhookEvent.objects.first()
        resp_reproc = self.client.post(reverse("tasks:reprocess_webhook", args=[wh.id]))
        self.assertEqual(resp_reproc.status_code, 200)

    def test_styleguide_view_renders_all_five_tactile_ds_sections(self):
        resp = self.client.get("/?view=styleguide")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        for section in ("FOUNDATIONS", "CONTROLS", "NAVIGATION", "DATA DISPLAY", "FEEDBACK"):
            self.assertIn(section, html)

        for token in ("ds-layer-raised", "ds-layer-inset", "ds-layer-pressed", "tactile-dark", "tactile-sand"):
            self.assertIn(token, html)

        resp_partial = self.client.get(reverse("tasks:partial_styleguide"))
        self.assertEqual(resp_partial.status_code, 200)
        self.assertIn("styleguide-view-section", resp_partial.content.decode("utf-8"))

    def test_phase4_1_responsive_tactile_audit_and_touch_sortable(self):
        resp_home = self.client.get("/")
        self.assertEqual(resp_home.status_code, 200)
        html_home = resp_home.content.decode("utf-8")

        # Verifica configuração mobile do SortableJS e Header responsivo em base.html
        for token in (
            "delay: 120",
            "delayOnTouchOnly: true",
            "touchStartThreshold: 5",
            "order-last lg:order-none",
            "md:grid-cols-2 xl:grid-cols-3",
        ):
            self.assertIn(token, html_home)

        # Verifica tabela TimeLog com overflow-x-auto e min-w-[600px]
        resp_analytics = self.client.get("/?view=analytics")
        self.assertIn("min-w-[600px]", resp_analytics.content.decode("utf-8"))

    def test_phase5_workspace_crud_and_safe_task_migration_on_delete(self):
        # 1. Criar nova Frente de Trabalho via HTMX
        resp_create = self.client.post(
            reverse("tasks:workspace_save"),
            {
                "name": "Consultoria IA",
                "slug": "ia",
                "accent_color": "#f97316",
                "shortcut_key": "Alt+6",
                "view": "settings",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_create.status_code, 200)
        new_ws = Workspace.objects.get(slug="ia")
        self.assertEqual(new_ws.name, "Consultoria IA")
        self.assertEqual(new_ws.accent_color, "#f97316")
        self.assertIn("#ia", resp_create.content.decode("utf-8"))

        # 2. Criar uma tarefa vinculada à nova frente e depois excluir a frente migrando tarefas
        task = Task.objects.create(
            workspace=new_ws,
            title="Implementar agente RAG para cliente",
            status=Task.Status.TODO,
        )
        fallback_ws = Workspace.objects.get(slug="saas")
        resp_del = self.client.post(
            reverse("tasks:workspace_delete", args=[new_ws.id]),
            {"target_workspace_id": str(fallback_ws.id), "view": "settings"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertFalse(Workspace.objects.filter(slug="ia").exists())
        task.refresh_from_db()
        self.assertEqual(task.workspace, fallback_ws)

    def test_phase5_project_crud_and_launchpad_toggle(self):
        ecom_ws = Workspace.objects.get(slug="ecom")
        resp_create = self.client.post(
            reverse("tasks:project_save"),
            {
                "name": "Nova Loja Shopee",
                "workspace_id": str(ecom_ws.id),
                "client_or_store": "Shopee Full Oficial",
                "github_repo_name": "caio/shopee-sync",
                "repo_url": "https://github.com/caio/shopee-sync",
                "admin_or_panel_url": "https://seller.shopee.com.br/",
                "view": "settings",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_create.status_code, 200)
        proj = Project.objects.get(name="Nova Loja Shopee")
        self.assertEqual(proj.workspace, ecom_ws)
        self.assertTrue(proj.is_active)

        # Alternar status ativo/arquivado
        resp_toggle = self.client.post(
            reverse("tasks:project_toggle", args=[proj.id]),
            {"view": "settings"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_toggle.status_code, 200)
        proj.refresh_from_db()
        self.assertFalse(proj.is_active)

        # Excluir projeto
        resp_del = self.client.post(
            reverse("tasks:project_delete", args=[proj.id]),
            {"view": "settings"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertFalse(Project.objects.filter(pk=proj.id).exists())

    def test_phase5_team_member_crud_one_click_assign_unassign_and_delete(self):
        # Verificar que o seed criou @caio, @pedro, @henrique, @monica
        self.assertEqual(TeamMember.objects.count(), 4)

        # 1. Cadastrar novo responsável (@lucas)
        resp_create = self.client.post(
            reverse("tasks:member_save"),
            {
                "name": "Lucas Silva",
                "handle": "@lucas",
                "role": "Dev Frontend",
                "accent_color": "#3b82f6",
                "view": "settings",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_create.status_code, 200)
        lucas = TeamMember.objects.get(handle="lucas")
        self.assertEqual(lucas.name, "Lucas Silva")
        self.assertEqual(lucas.avatar_initials, "LS")

        # 2. Atribuir @lucas a uma tarefa com 1 clique
        task = Task.objects.first()
        resp_assign = self.client.post(
            reverse("tasks:assign_member", args=[task.id]),
            {"member_id": str(lucas.id)},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_assign.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.assignee, lucas)
        self.assertEqual(task.assigned_to, "@lucas")

        # 3. Retirar atribuição com 1 clique (member_id="none")
        resp_unassign = self.client.post(
            reverse("tasks:assign_member", args=[task.id]),
            {"member_id": "none"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_unassign.status_code, 200)
        task.refresh_from_db()
        self.assertIsNone(task.assignee)
        self.assertEqual(task.assigned_to, "")

        # 4. Atribuir novamente e excluir o TeamMember (deve manter a tarefa sem responsável)
        resp_assign_again = self.client.post(
            reverse("tasks:assign_member", args=[task.id]),
            {"member_id": str(lucas.id)},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_assign_again.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.assignee, lucas)

        resp_del_member = self.client.post(
            reverse("tasks:member_delete", args=[lucas.id]),
            {"view": "settings"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_del_member.status_code, 200)
        self.assertFalse(TeamMember.objects.filter(pk=lucas.id).exists())
        task.refresh_from_db()
        self.assertIsNone(task.assignee)
        self.assertEqual(task.assigned_to, "")

    def test_phase6_gemini_json_sync_via_clipboard_and_api_webhook(self):
        # 1. Envio via endpoint HTMX /tasks/gemini-paste/ (com cercas Markdown ```json ... ```)
        gemini_markdown_json = """```json
        {
          "action": "upsert",
          "title": "Implementar exportação PDF no Portal MS CREW",
          "workspace": "wsa",
          "project": "Portal MS CREW",
          "assignee": "henrique",
          "shift": "morning",
          "status": "todo",
          "impact": 5,
          "urgency": 5,
          "is_top_three": true,
          "minutes_spent": 35,
          "notes": "Resumo técnico gerado na conversa com o Gemini.",
          "subtasks": ["[x] Configurar template WeasyPrint", "Validar assinatura digital", "Testar download mobile"],
          "external_ref": "GEM-MSCREW-01"
        }
        ```"""
        resp_paste = self.client.post(
            reverse("tasks:gemini_paste"),
            {"clipboard_text": gemini_markdown_json},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_paste.status_code, 200)
        html_paste = resp_paste.content.decode("utf-8")
        self.assertIn("Sync Gemini aplicado", html_paste)
        self.assertIn("+3 subtarefas, 35 min", html_paste)

        task = Task.objects.get(external_ref="GEM-MSCREW-01")
        self.assertEqual(task.title, "Implementar exportação PDF no Portal MS CREW")
        self.assertEqual(task.workspace.slug, "wsa")
        self.assertEqual(task.project.name, "Portal MS CREW")
        self.assertEqual(task.assigned_to, "@henrique")
        self.assertEqual(task.assignee.handle, "henrique")
        self.assertEqual(task.subtasks.count(), 3)
        self.assertEqual(task.subtasks.filter(is_done=True).count(), 1)
        self.assertEqual(task.total_logged_minutes, 35)

        # 2. Envio direto para POST /api/webhooks/gemini
        api_payload = {
            "action": "upsert",
            "title": "Otimizar sincronizador de anúncios na Info Mundi Office",
            "workspace": "ecom",
            "project": "Info Mundi Office",
            "assignee": "monica",
            "shift": "afternoon",
            "status": "doing",
            "impact": 4,
            "urgency": 5,
            "minutes_spent": 20,
            "notes": "Sincronizado via API Gemini",
            "subtasks": ["[x] Mapear categorias ML", "Atualizar estoque Full"],
        }
        resp_api = self.client.post(
            "/api/webhooks/gemini",
            data=json.dumps(api_payload),
            content_type="application/json",
        )
        self.assertEqual(resp_api.status_code, 200)
        data = resp_api.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["subtasks_created"], 2)
        self.assertEqual(data["minutes_logged"], 20)

        api_task = Task.objects.get(pk=data["task_id"])
        self.assertEqual(api_task.project.name, "Info Mundi Office")
        self.assertEqual(api_task.total_logged_minutes, 20)

    def test_phase6_whatsapp_multiline_gemini_block_creates_subtasks_and_timelog(self):
        multiline_whatsapp_text = (
            "/gemini Refatorar módulo de relatórios #saas !top3 @caio /manha\n"
            "Projeto: Deskify\n"
            "Tempo: 25m\n"
            "- [x] Ajustar contraste dos cards dark mode\n"
            "- [ ] Padronizar colunas da tabela de materiais\n"
            "Notas: Alinhado no chat com o Gemini para usar tokens semânticos."
        )
        resp = self.client.post(
            "/api/webhooks/whatsapp",
            data=json.dumps({"text": multiline_whatsapp_text, "sender": "@caio"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["workspace"], "saas")
        self.assertEqual(data["project"], "Deskify")
        self.assertEqual(data["subtasks_created"], 2)
        self.assertEqual(data["minutes_logged"], 25)

        task = Task.objects.get(pk=data["task_id"])
        self.assertEqual(task.title, "Refatorar módulo de relatórios")
        self.assertEqual(task.project.name, "Deskify")
        self.assertEqual(task.assigned_to, "@caio")
        self.assertEqual(task.assignee.handle, "caio")
        self.assertTrue(task.is_top_three)
        self.assertEqual(task.subtasks.count(), 2)
        self.assertEqual(task.subtasks.filter(is_done=True).count(), 1)
        self.assertEqual(task.total_logged_minutes, 25)
        self.assertIn("tokens semânticos", task.notes)

    def test_phase7_trava1_whatsapp_audio_safety_lock_ignores_untriggered_audio(self):
        initial_task_count = Task.objects.count()

        # 1. Evolution API: Áudio normal de conversa SEM gatilho (/task ou !cc) deve ser 100% ignorado
        evolution_audio_payload = {
            "event": "messages.upsert",
            "data": {
                "key": {"id": "MSG-AUDIO-UNTRIGGERED-1", "fromMe": False},
                "pushName": "Cliente WhatsApp",
                "messageType": "audioMessage",
                "message": {
                    "audioMessage": {
                        "mimetype": "audio/ogg; codecs=opus",
                        "seconds": 14,
                    }
                },
            },
        }
        resp_evo = self.client.post(
            "/api/webhooks/whatsapp/evolution",
            data=json.dumps(evolution_audio_payload),
            content_type="application/json",
        )
        self.assertEqual(resp_evo.status_code, 200)
        evo_data = resp_evo.json()
        self.assertTrue(evo_data["ok"])
        self.assertEqual(evo_data["status"], "ignored_safety_lock_no_trigger")
        self.assertEqual(Task.objects.count(), initial_task_count)

        # 2. Endpoint /api/webhooks/whatsapp/audio sem explicit_trigger e sem /task no extra_hint
        resp_direct = self.client.post(
            "/api/webhooks/whatsapp/audio",
            data=json.dumps(
                {
                    "audio_base64": "OggS",
                    "mime_type": "audio/ogg",
                    "extra_hint": "mensagem comum sem gatilho",
                    "explicit_trigger": False,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp_direct.status_code, 200)
        direct_data = resp_direct.json()
        self.assertEqual(direct_data["status"], "ignored_safety_lock_no_trigger")
        self.assertEqual(Task.objects.count(), initial_task_count)

    def test_phase7_trava2_and_trava3_daily_quota_circuit_breaker(self):
        import base64
        from apps.core.models import SystemIntegrationConfig

        config = SystemIntegrationConfig.get_solo()
        config.max_daily_audio_requests = 2
        config.ai_audio_enabled = True
        config.save()

        sample_audio_b64 = base64.b64encode(b"fake-ogg-audio-bytes-for-test").decode("ascii")

        # 1º Áudio com gatilho explícito -> Sucesso (1/2 da cota)
        resp1 = self.client.post(
            "/api/webhooks/whatsapp/audio",
            data=json.dumps(
                {
                    "audio_base64": sample_audio_b64,
                    "mime_type": "audio/ogg",
                    "extra_hint": "/task Revisar campanha Black Friday frente wsa responsável pedro top 3 para hoje",
                    "sender": "@caio",
                    "explicit_trigger": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertTrue(data1["ok"])
        self.assertEqual(data1["quota"]["used_today"], 1)
        task1 = Task.objects.get(pk=data1["task_id"])
        self.assertEqual(task1.workspace.slug, "wsa")
        self.assertEqual(task1.assigned_to, "@pedro")
        self.assertTrue(task1.is_top_three)
        self.assertGreaterEqual(task1.subtasks.count(), 1)

        # 2º Áudio com gatilho explícito -> Sucesso (2/2 da cota)
        resp2 = self.client.post(
            "/api/webhooks/whatsapp/audio",
            data=json.dumps(
                {
                    "audio_base64": sample_audio_b64,
                    "mime_type": "audio/ogg",
                    "extra_hint": "!cc Conferir estoque de cadeiras #ecom @monica",
                    "sender": "@caio",
                    "explicit_trigger": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["ok"])
        self.assertEqual(data2["quota"]["used_today"], 2)

        # 3º Áudio -> Bloqueado pelo Disjuntor de Cota Diária (Trava 3)
        task_count_before_3rd = Task.objects.count()
        resp3 = self.client.post(
            "/api/webhooks/whatsapp/audio",
            data=json.dumps(
                {
                    "audio_base64": sample_audio_b64,
                    "mime_type": "audio/ogg",
                    "extra_hint": "/task Terceiro áudio acima da cota #saas",
                    "sender": "@caio",
                    "explicit_trigger": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertFalse(data3["ok"])
        self.assertTrue(data3["blocked"])
        self.assertEqual(data3["status"], "blocked_daily_quota_exceeded")
        self.assertEqual(Task.objects.count(), task_count_before_3rd)

    def test_phase7_voice_dictation_ptbr_normalization_and_ai_subtasks(self):
        from apps.core.voice_service import normalize_spoken_command_ptbr

        spoken = "Ajustar campanha de lançamento frente wsa responsável pedro top 3 para hoje turno da tarde"
        normalized = normalize_spoken_command_ptbr(spoken)
        self.assertIn("#wsa", normalized)
        self.assertIn("@pedro", normalized)
        self.assertIn("!top3", normalized)
        self.assertIn("!hoje", normalized)
        self.assertIn("/tarde", normalized)

        # Testa endpoint HTMX de Ditado Nativo (/tasks/voice-dictation/)
        resp_dict = self.client.post(
            "/tasks/voice-dictation/",
            data={
                "spoken_text": "Corrigir integração de frete frente ecom responsável monica urgente hoje turno da manhã",
                "workspace": "all",
                "view": "board",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_dict.status_code, 200)
        dictated_task = Task.objects.filter(title__icontains="Corrigir integração de frete").first()
        self.assertIsNotNone(dictated_task)
        self.assertEqual(dictated_task.workspace.slug, "ecom")
        self.assertEqual(dictated_task.assigned_to, "@monica")
        self.assertEqual(dictated_task.shift, Task.Shift.MORNING)

        # Testa o botão "⚡ Quebrar com IA" (/tasks/<pk>/ai-subtasks/)
        self.assertEqual(dictated_task.subtasks.count(), 0)
        resp_sub = self.client.post(
            f"/tasks/{dictated_task.id}/ai-subtasks/?workspace=all&view=board",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_sub.status_code, 200)
        self.assertGreaterEqual(dictated_task.subtasks.count(), 3)

    def test_phase7_1_evolution_quoted_audio_stanza_fetches_base64_and_creates_task(self):
        from unittest.mock import patch
        from apps.core.models import SystemIntegrationConfig

        # 1. Testa salvamento dos 3 campos da Evolution API v2 no formulário HTMX
        resp_save = self.client.post(
            "/settings/audio-quota/save/?workspace=all&view=integrations",
            data={
                "max_daily_audio_requests": "25",
                "ai_audio_enabled": "1",
                "evolution_api_url": "http://127.0.0.1:8080",
                "evolution_instance_name": "wsa-instance",
                "evolution_api_key": "evo-secret-token-123",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_save.status_code, 200)
        config = SystemIntegrationConfig.get_solo()
        self.assertEqual(config.evolution_api_url, "http://127.0.0.1:8080")
        self.assertEqual(config.evolution_instance_name, "wsa-instance")
        self.assertEqual(config.evolution_api_key, "evo-secret-token-123")

        # 2. Simula webhook messages.upsert da Evolution API v2 respondendo (quote) a um áudio com "!cc #wsa @pedro !top3"
        evolution_quote_payload = {
            "event": "messages.upsert",
            "instance": "wsa-instance",
            "data": {
                "key": {
                    "id": "MSG-QUOTE-REPLY-99",
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "fromMe": True,
                },
                "pushName": "Caio",
                "messageType": "extendedTextMessage",
                "message": {
                    "extendedTextMessage": {
                        "text": "!cc #wsa @pedro !top3",
                        "contextInfo": {
                            "stanzaId": "BAE5999AUDIO",
                            "participant": "5511888888888@s.whatsapp.net",
                            "quotedMessage": {
                                "audioMessage": {
                                    "mimetype": "audio/ogg; codecs=opus",
                                    "seconds": 18,
                                    "ptt": True,
                                }
                            },
                        },
                    }
                },
            },
        }

        with patch(
            "apps.integrations.api.fetch_quoted_audio_from_evolution",
            return_value=(b"simulated-evolution-ogg-bytes", "audio/ogg"),
        ) as mock_fetcher:
            resp = self.client.post(
                "/api/webhooks/whatsapp/evolution",
                data=json.dumps(evolution_quote_payload),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "processed_audio")

        mock_fetcher.assert_called_once_with(
            stanza_id="BAE5999AUDIO",
            remote_jid="5511999999999@s.whatsapp.net",
            instance_override="wsa-instance",
            quoted_message_obj={
                "audioMessage": {
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 18,
                    "ptt": True,
                }
            },
        )

        created_task = Task.objects.get(pk=data["task_id"])
        self.assertEqual(created_task.workspace.slug, "wsa")
        self.assertEqual(created_task.assigned_to, "@pedro")
        self.assertEqual(created_task.assignee.handle, "pedro")
        self.assertTrue(created_task.is_top_three)
        self.assertGreaterEqual(created_task.subtasks.count(), 1)

    def test_phase7_2_list_view_and_arrival_order_sorting(self):
        from datetime import timedelta
        from django.utils import timezone

        ws_wsa = Workspace.objects.get(slug="wsa")
        base_time = timezone.now() - timedelta(hours=3)

        task_a = Task.objects.create(
            workspace=ws_wsa,
            title="Task A - Primeira da Fila FIFO",
            status=Task.Status.TODO,
            impact=2,
            urgency=2,
        )
        Task.objects.filter(pk=task_a.pk).update(created_at=base_time)

        task_b = Task.objects.create(
            workspace=ws_wsa,
            title="Task B - Segunda da Fila FIFO",
            status=Task.Status.TODO,
            impact=3,
            urgency=3,
        )
        Task.objects.filter(pk=task_b.pk).update(created_at=base_time + timedelta(minutes=10))

        task_c = Task.objects.create(
            workspace=ws_wsa,
            title="Task C - Terceira e Mais Recente LIFO",
            status=Task.Status.TODO,
            impact=4,
            urgency=4,
        )
        Task.objects.filter(pk=task_c.pk).update(created_at=base_time + timedelta(minutes=20))

        # 1. Ordem de Chegada FIFO (sort=arrival_asc): Task A -> Task B -> Task C
        resp_asc = self.client.get("/?view=list&workspace=wsa&sort=arrival_asc")
        self.assertEqual(resp_asc.status_code, 200)
        html_asc = resp_asc.content.decode("utf-8")
        self.assertIn('id="list-view-section"', html_asc)
        pos_a_asc = html_asc.index("Task A - Primeira da Fila FIFO")
        pos_b_asc = html_asc.index("Task B - Segunda da Fila FIFO")
        pos_c_asc = html_asc.index("Task C - Terceira e Mais Recente LIFO")
        self.assertLess(pos_a_asc, pos_b_asc)
        self.assertLess(pos_b_asc, pos_c_asc)

        # 2. Mais Recentes Primeiro LIFO (sort=arrival_desc): Task C -> Task B -> Task A
        resp_desc = self.client.get("/?view=list&workspace=wsa&sort=arrival_desc")
        self.assertEqual(resp_desc.status_code, 200)
        html_desc = resp_desc.content.decode("utf-8")
        pos_c_desc = html_desc.index("Task C - Terceira e Mais Recente LIFO")
        pos_b_desc = html_desc.index("Task B - Segunda da Fila FIFO")
        pos_a_desc = html_desc.index("Task A - Primeira da Fila FIFO")
        self.assertLess(pos_c_desc, pos_b_desc)
        self.assertLess(pos_b_desc, pos_a_desc)

        # 3. Preservação de view=list ao executar ação HTMX (ex: concluir tarefa)
        resp_action = self.client.post(
            f"/tasks/{task_a.id}/status/?workspace=wsa&view=list&sort=arrival_asc&status_filter=all",
            data={"status": "done", "view": "list"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_action.status_code, 200)
        self.assertIn('id="list-view-section"', resp_action.content.decode("utf-8"))

        # 4. Garante zero cores proibidas (roxo/verde) na visão de Lista
        forbidden = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for word in forbidden:
            self.assertNotIn(word, html_desc.lower())

    def test_phase8_vault_encryption_crud_secret_endpoint_and_no_exposed_passwords(self):
        from apps.core.models import CredentialEntry

        ws_ecom = Workspace.objects.get(slug="ecom")
        proj_mercuria = Project.objects.filter(name="Mercuria Shop").first()

        # 1. Criação via POST /vault/save/
        raw_password = "SuperSecret#VaultPassword!2026"
        resp_save = self.client.post(
            "/vault/save/?workspace=ecom&view=vault",
            data={
                "title": "Conta Shopee Ads Secundária",
                "category": "ecommerce",
                "workspace_id": str(ws_ecom.id),
                "project_id": str(proj_mercuria.id) if proj_mercuria else "",
                "username_or_email": "ads@mercuriashop.com.br",
                "secret": raw_password,
                "service_url": "https://seller.shopee.com.br",
                "recovery_info": "Authy 2FA",
                "notes": "Credencial exclusiva para gestão de anúncios",
                "view": "vault",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_save.status_code, 200)

        cred = CredentialEntry.objects.get(title="Conta Shopee Ads Secundária")
        self.assertEqual(cred.workspace.slug, "ecom")
        self.assertEqual(cred.username_or_email, "ads@mercuriashop.com.br")
        # Garante que o valor salvo no banco está criptografado e NUNCA igual à senha pura
        self.assertNotEqual(cred.encrypted_secret, raw_password)
        self.assertNotIn(raw_password, cred.encrypted_secret)
        self.assertEqual(cred.get_secret(), raw_password)

        # 2. Renderização de /?view=vault NÃO deve expor a senha em texto puro no HTML inicial
        #    e deve renderizar tanto o container de Grade (Cards) quanto o container de Lista (Tabela Tátil)
        resp_view = self.client.get("/?view=vault&workspace=ecom")
        self.assertEqual(resp_view.status_code, 200)
        html_vault = resp_view.content.decode("utf-8")
        self.assertIn('id="vault-view-section"', html_vault)
        self.assertIn('id="vault-grid-container"', html_vault)
        self.assertIn('id="vault-list-container"', html_vault)
        self.assertIn(f'data-vault-card-id="{cred.id}"', html_vault)
        self.assertIn(f'data-vault-row-id="{cred.id}"', html_vault)
        self.assertIn("Conta Shopee Ads Secundária", html_vault)
        self.assertIn("ads@mercuriashop.com.br", html_vault)
        self.assertNotIn(raw_password, html_vault)

        # Garante ausência estrita de cores proibidas (roxo / verde)
        forbidden = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for word in forbidden:
            self.assertNotIn(word, html_vault.lower())

        # 3. Endpoint sob demanda GET /vault/<pk>/secret/ retorna a senha descriptografada
        resp_sec = self.client.get(f"/vault/{cred.id}/secret/")
        self.assertEqual(resp_sec.status_code, 200)
        sec_data = resp_sec.json()
        self.assertTrue(sec_data["ok"])
        self.assertEqual(sec_data["secret"], raw_password)

        # 4. Exclusão via POST /vault/<pk>/delete/
        resp_del = self.client.post(
            f"/vault/{cred.id}/delete/?workspace=ecom&view=vault",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertFalse(CredentialEntry.objects.filter(pk=cred.id).exists())

    def test_phase9_production_auth_guard_and_strict_webhook_token(self):
        from django.contrib.auth.models import User
        from django.test import override_settings

        # 1. Tela de login (/login/) segue o Tactile Design System sem cores proibidas
        resp_login_page = self.client.get("/login/")
        self.assertEqual(resp_login_page.status_code, 200)
        login_html = resp_login_page.content.decode("utf-8")
        self.assertIn("COMMAND CENTER", login_html)
        forbidden = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for word in forbidden:
            self.assertNotIn(word, login_html.lower())

        # 2. Com REQUIRE_AUTH=True, acesso anônimo a "/" e "/vault/1/secret/" redireciona para /login/
        with override_settings(REQUIRE_AUTH=True, WEBHOOK_SECRET_TOKEN="prod-token-999"):
            resp_anon_home = self.client.get("/")
            self.assertEqual(resp_anon_home.status_code, 302)
            self.assertIn("/login/", resp_anon_home["Location"])

            resp_anon_vault = self.client.get("/vault/1/secret/")
            self.assertEqual(resp_anon_vault.status_code, 302)
            self.assertIn("/login/", resp_anon_vault["Location"])

            # 3. Webhooks (/api/webhooks/*) NÃO redirecionam para /login/, mas exigem X-Webhook-Token estrito
            resp_wh_no_token = self.client.post(
                "/api/webhooks/whatsapp",
                data=json.dumps({"text": "/task Teste sem token #wsa", "sender": "@caio"}),
                content_type="application/json",
            )
            self.assertEqual(resp_wh_no_token.status_code, 401)

            resp_wh_with_token = self.client.post(
                "/api/webhooks/whatsapp",
                data=json.dumps({"text": "/task Teste com token #wsa", "sender": "@caio"}),
                content_type="application/json",
                HTTP_X_WEBHOOK_TOKEN="prod-token-999",
            )
            self.assertEqual(resp_wh_with_token.status_code, 200)
            self.assertTrue(resp_wh_with_token.json()["ok"])

            # 4. Usuário autenticado acessa o Dashboard normalmente com REQUIRE_AUTH=True
            admin_user = User.objects.create_superuser(
                username="caio_admin",
                email="caio@wsacreative.com.br",
                password="StrongPassword2026!",
            )
            self.client.force_login(admin_user)
            resp_auth_home = self.client.get("/")
            self.assertEqual(resp_auth_home.status_code, 200)




