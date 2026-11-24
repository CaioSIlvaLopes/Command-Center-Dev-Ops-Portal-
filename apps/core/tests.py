import json
from datetime import date
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from apps.core.models import Workspace, Project, TeamMember
from apps.core.services import parse_and_create_task
from apps.tasks.models import Task, SubTask
from apps.timetracker.models import TimeLog
from apps.integrations.models import WebhookEvent


@override_settings(REQUIRE_AUTH=False)
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

    def test_phase10_multiuser_isolation_anti_idor_top3_timer_and_vault_permissions(self):
        from django.contrib.auth.models import User
        from apps.core.models import CredentialEntry

        ws_wsa = Workspace.objects.get(slug="wsa")
        ws_saas = Workspace.objects.get(slug="saas")
        ws_ecom = Workspace.objects.get(slug="ecom")
        member_pedro = TeamMember.objects.get(handle="pedro")
        member_monica = TeamMember.objects.get(handle="monica")

        admin_user = User.objects.create_superuser(
            username="admin_caio",
            email="caio@wsacreative.com.br",
            password="AdminPass2026!",
        )
        user_pedro = User.objects.create_user(
            username="pedro_dev",
            email="pedro@wsacreative.com.br",
            password="PedroPass2026!",
        )
        user_monica = User.objects.create_user(
            username="monica_ops",
            email="monica@wsacreative.com.br",
            password="MonicaPass2026!",
        )

        member_pedro.user = user_pedro
        member_pedro.can_access_vault = False
        member_pedro.save()

        member_monica.user = user_monica
        member_monica.can_access_vault = True
        member_monica.save()

        # 1. Criação de tarefas via Admin/Webhook vs Usuário Comum
        # Admin criando com @pedro atribui owner = user_pedro
        task_assigned_by_admin = parse_and_create_task(
            "Task delegada pelo Admin #saas @pedro !top3",
            owner=admin_user,
        )
        self.assertEqual(task_assigned_by_admin.owner, user_pedro)
        self.assertEqual(task_assigned_by_admin.assignee, member_pedro)

        # Usuário comum tentando criar tarefa com @monica força owner = user_pedro
        self.client.force_login(user_pedro)
        self.client.post(
            "/tasks/command/",
            {"command": "Task criada por Pedro #saas @monica"},
            HTTP_HX_REQUEST="true",
        )
        pedro_created_task = Task.objects.filter(title="Task criada por Pedro").first()
        self.assertIsNotNone(pedro_created_task)
        self.assertEqual(pedro_created_task.owner, user_pedro)

        # Tarefa exclusiva do Admin (sem relação com Pedro)
        admin_only_task = Task.objects.create(
            workspace=ws_wsa,
            owner=admin_user,
            title="Contrato Confidencial Admin",
            status=Task.Status.TODO,
            impact=5,
            urgency=5,
            is_top_three=True,
        )
        admin_subtask = SubTask.objects.create(
            task=admin_only_task,
            title="Subtarefa Secreta Admin",
            is_done=False,
        )

        # 2. Isolamento de Visibilidade no Dashboard (Pedro não vê tarefa do Admin, mesmo tentando ?user_filter=all)
        resp_pedro_dash = self.client.get("/?user_filter=all")
        self.assertEqual(resp_pedro_dash.status_code, 200)
        pedro_html = resp_pedro_dash.content.decode("utf-8")
        self.assertIn("Task delegada pelo Admin", pedro_html)
        self.assertNotIn("Contrato Confidencial Admin", pedro_html)
        self.assertEqual(resp_pedro_dash.context["current_user_filter"], "me")
        self.assertFalse(resp_pedro_dash.context["is_general_admin"])

        # Admin com ?user_filter=all vê todas, e com ?user_filter=<pedro.id> filtra por Pedro
        self.client.force_login(admin_user)
        resp_admin_all = self.client.get("/?user_filter=all")
        self.assertEqual(resp_admin_all.status_code, 200)
        admin_all_html = resp_admin_all.content.decode("utf-8")
        self.assertIn("Task delegada pelo Admin", admin_all_html)
        self.assertIn("Contrato Confidencial Admin", admin_all_html)

        resp_admin_pedro = self.client.get(f"/?user_filter={user_pedro.id}")
        self.assertEqual(resp_admin_pedro.status_code, 200)
        admin_pedro_html = resp_admin_pedro.content.decode("utf-8")
        self.assertIn("Task delegada pelo Admin", admin_pedro_html)
        self.assertNotIn("Contrato Confidencial Admin", admin_pedro_html)

        # 3. Top 3 Isolado por Usuário (Pedro ter 3 tarefas Top 3 não rebaixa o Top 3 do Admin)
        for idx in range(4):
            Task.objects.create(
                workspace=ws_saas,
                owner=user_pedro,
                title=f"Pedro Top Task {idx}",
                status=Task.Status.TODO,
                is_top_three=True,
            )
        admin_only_task.refresh_from_db()
        self.assertTrue(admin_only_task.is_top_three)
        self.assertEqual(
            Task.objects.filter(owner=user_pedro, is_top_three=True).exclude(status=Task.Status.DONE).count(),
            3,
        )

        # 4. Timer Isolado por Usuário (Play de Pedro não pausa Timer em andamento do Admin)
        self.client.force_login(admin_user)
        self.client.post(f"/timetracker/start/{admin_only_task.id}/")
        admin_log = TimeLog.objects.filter(task=admin_only_task, user=admin_user, is_running=True).first()
        self.assertIsNotNone(admin_log)

        self.client.force_login(user_pedro)
        self.client.post(f"/timetracker/start/{task_assigned_by_admin.id}/")
        pedro_log = TimeLog.objects.filter(task=task_assigned_by_admin, user=user_pedro, is_running=True).first()
        self.assertIsNotNone(pedro_log)

        admin_log.refresh_from_db()
        self.assertTrue(admin_log.is_running)

        # 5. Proteção Anti-IDOR (Pedro tentando acessar/modificar tarefa do Admin recebe 403)
        idor_get = self.client.get(f"/tasks/{admin_only_task.id}/detail/")
        self.assertEqual(idor_get.status_code, 403)

        idor_post_urls = [
            f"/tasks/{admin_only_task.id}/update/",
            f"/tasks/{admin_only_task.id}/move/",
            f"/tasks/{admin_only_task.id}/status/",
            f"/tasks/{admin_only_task.id}/shift/",
            f"/tasks/{admin_only_task.id}/triage/",
            f"/tasks/{admin_only_task.id}/priority/",
            f"/tasks/{admin_only_task.id}/delete/",
            f"/tasks/{admin_only_task.id}/toggle-top3/",
            f"/tasks/{admin_only_task.id}/assign/",
            f"/tasks/{admin_only_task.id}/ai-subtasks/",
            f"/tasks/{admin_only_task.id}/subtasks/add/",
            f"/tasks/subtasks/{admin_subtask.id}/toggle/",
            f"/tasks/subtasks/{admin_subtask.id}/delete/",
            f"/timetracker/start/{admin_only_task.id}/",
            f"/timetracker/manual/{admin_only_task.id}/",
            f"/timetracker/log/{admin_log.id}/delete/",
        ]
        for url in idor_post_urls:
            resp_idor = self.client.post(url)
            self.assertEqual(resp_idor.status_code, 403, f"Falha Anti-IDOR em {url}")

        # 6. Permissões do Cofre (Vault) e Áreas Exclusivas do Admin Geral
        cred = CredentialEntry.objects.create(
            workspace=ws_ecom,
            title="Credencial Teste Multiusuário",
            category=CredentialEntry.Category.ECOMMERCE,
            username_or_email="loja@teste.com",
        )
        cred.set_secret("SegredoCofre123!")
        cred.save()

        # Pedro (can_access_vault=False) -> 403 em tudo do Cofre e em áreas Admin, e UI oculta itens restritos
        self.assertNotIn("admin-user-filter-select", pedro_html)
        self.assertNotIn("btn-audio-ia-modal", pedro_html)
        self.assertNotIn("btn-gemini-clipboard-sync", pedro_html)
        self.assertNotIn("Cofre &amp; Senhas", pedro_html)
        self.assertNotIn("Gestão &amp; Cadastros", pedro_html)
        self.assertNotIn("Integrações &amp; Logs", pedro_html)
        self.assertIn("@pedro_dev", pedro_html)
        self.assertIn("Equipe", pedro_html)

        self.assertEqual(self.client.get("/?view=vault").status_code, 403)
        self.assertEqual(self.client.get(f"/vault/{cred.id}/secret/").status_code, 403)
        self.assertEqual(
            self.client.post("/vault/save/", {"title": "X", "username_or_email": "x@x.com"}).status_code,
            403,
        )
        self.assertEqual(self.client.post(f"/vault/{cred.id}/delete/").status_code, 403)
        self.assertEqual(self.client.get("/?view=integrations").status_code, 403)
        self.assertEqual(self.client.get("/?view=settings").status_code, 403)
        self.assertEqual(self.client.post("/settings/workspaces/save/", {"name": "Hack"}).status_code, 403)
        self.assertEqual(self.client.post("/tasks/simulate-webhook/").status_code, 403)
        self.assertEqual(self.client.post("/tasks/recalculate/").status_code, 403)

        # Mônica (can_access_vault=True, mas não é superuser) -> Pode ver, revelar, criar e editar, mas NÃO pode deletar (403 e botão oculto)
        self.client.force_login(user_monica)
        resp_monica_vault = self.client.get("/?view=vault")
        self.assertEqual(resp_monica_vault.status_code, 200)
        monica_vault_html = resp_monica_vault.content.decode("utf-8")
        self.assertIn("Cofre &amp; Senhas", monica_vault_html)
        self.assertNotIn("Gestão &amp; Cadastros", monica_vault_html)
        self.assertNotIn(f"/vault/{cred.id}/delete/", monica_vault_html)

        self.assertEqual(self.client.get(f"/vault/{cred.id}/secret/").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/vault/save/",
                {
                    "workspace_id": str(ws_ecom.id),
                    "title": "Nova Credencial Monica",
                    "category": "email",
                    "username_or_email": "monica@ecom.com",
                    "secret": "SenhaMonica!",
                },
            ).status_code,
            200,
        )
        new_monica_cred = CredentialEntry.objects.filter(title="Nova Credencial Monica").first()
        self.assertIsNotNone(new_monica_cred)
        # Mônica também pode editar uma credencial existente
        self.assertEqual(
            self.client.post(
                "/vault/save/",
                {
                    "credential_id": str(new_monica_cred.id),
                    "workspace_id": str(ws_ecom.id),
                    "title": "Nova Credencial Monica Editada",
                    "category": "email",
                    "username_or_email": "monica@ecom.com",
                    "secret": "",
                },
            ).status_code,
            200,
        )
        new_monica_cred.refresh_from_db()
        self.assertEqual(new_monica_cred.title, "Nova Credencial Monica Editada")
        self.assertEqual(self.client.post(f"/vault/{cred.id}/delete/").status_code, 403)
        self.assertTrue(CredentialEntry.objects.filter(pk=cred.id).exists())

        # 7. Gestão de Logins e Permissões do Cofre pelo Admin Geral em /settings/members/save/
        self.client.force_login(admin_user)
        self.assertIn("admin-user-filter-select", admin_all_html)
        self.assertIn("btn-audio-ia-modal", admin_all_html)
        self.assertIn("btn-gemini-clipboard-sync", admin_all_html)

        resp_create_member = self.client.post(
            "/settings/members/save/",
            {
                "view": "settings",
                "name": "Lucas Ops",
                "handle": "lucas",
                "role": "DevOps",
                "accent_color": "#f97316",
                "username": "lucas_login",
                "password": "LucasStrongPassword2026!",
                "can_access_vault": "1",
                "is_superuser": "0",
                "user_is_active": "1",
            },
        )
        self.assertEqual(resp_create_member.status_code, 200)
        member_lucas = TeamMember.objects.select_related("user").filter(handle="lucas").first()
        self.assertIsNotNone(member_lucas)
        self.assertIsNotNone(member_lucas.user)
        self.assertEqual(member_lucas.user.username, "lucas_login")
        self.assertTrue(member_lucas.user.check_password("LucasStrongPassword2026!"))
        self.assertTrue(member_lucas.can_access_vault)
        self.assertFalse(member_lucas.user.is_superuser)

        # Proteção contra auto-bloqueio do próprio Admin Geral
        member_caio = TeamMember.objects.filter(handle="caio").first()
        if member_caio:
            member_caio.user = admin_user
            member_caio.save()
            self.client.post(
                "/settings/members/save/",
                {
                    "member_id": str(member_caio.id),
                    "name": member_caio.name,
                    "handle": member_caio.handle,
                    "username": admin_user.username,
                    "is_superuser": "0",
                    "user_is_active": "0",
                },
            )
            admin_user.refresh_from_db()
            self.assertTrue(admin_user.is_superuser)
            self.assertTrue(admin_user.is_active)

        # 8. Verificação estrita de zero cores proibidas em todas as telas renderizadas
        resp_settings = self.client.get("/?view=settings")
        self.assertEqual(resp_settings.status_code, 200)
        settings_html = resp_settings.content.decode("utf-8")
        forbidden = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for html_doc in (pedro_html, monica_vault_html, admin_all_html, settings_html):
            for word in forbidden:
                self.assertNotIn(word, html_doc.lower())

    def test_phase11_users_access_view_crud_toggles_anti_lockout_and_permissions(self):
        from django.contrib.auth.models import User

        admin_user = User.objects.create_superuser(
            username="caio_root",
            email="caio@wsacreative.com.br",
            password="RootPassword2026!",
        )
        regular_user = User.objects.create_user(
            username="colab_comum",
            email="colab@wsacreative.com.br",
            password="ColabPassword2026!",
        )

        # 1. Colaborador comum recebe redirecionamento (302 -> /?view=focus) em GET /?view=users
        #    e HTTP 403 em /users/save/, /users/<pk>/toggle-active/ e /users/<pk>/toggle-vault/
        self.client.force_login(regular_user)
        resp_get_users_non_admin = self.client.get("/?view=users")
        self.assertEqual(resp_get_users_non_admin.status_code, 302)
        self.assertIn("view=focus", resp_get_users_non_admin["Location"])

        self.assertEqual(
            self.client.post(
                "/users/save/",
                {"username": "hack_user", "password": "HackPassword123!", "full_name": "Hack"},
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post(f"/users/{admin_user.id}/toggle-active/").status_code, 403)
        self.assertEqual(self.client.post(f"/users/{regular_user.id}/toggle-vault/").status_code, 403)

        # 2. Admin Geral acessa /?view=users e verifica zero cores proibidas
        self.client.force_login(admin_user)
        resp_users_view = self.client.get("/?view=users")
        self.assertEqual(resp_users_view.status_code, 200)
        users_html = resp_users_view.content.decode("utf-8")
        self.assertIn('id="users-access-view-section"', users_html)
        self.assertIn("Usuários &amp; Acessos", users_html)

        forbidden = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for word in forbidden:
            self.assertNotIn(word, users_html.lower())

        # 3. Admin Geral cria um novo usuário com TeamMember sincronizado via POST /users/save/
        resp_create = self.client.post(
            "/users/save/",
            {
                "view": "users",
                "full_name": "Rafael Designer",
                "handle": "rafael",
                "role": "UI/UX Designer",
                "username": "rafael_wsa",
                "email": "rafael@wsacreative.com.br",
                "password": "RafaelPassword2026!",
                "accent_color": "#f97316",
                "can_access_vault": "0",
                "is_superuser": "0",
                "is_active": "1",
            },
        )
        self.assertEqual(resp_create.status_code, 200)
        rafael_user = User.objects.select_related("team_profile").filter(username="rafael_wsa").first()
        self.assertIsNotNone(rafael_user)
        self.assertTrue(rafael_user.is_active)
        self.assertFalse(rafael_user.is_superuser)
        self.assertEqual(rafael_user.team_profile.handle, "rafael")
        self.assertEqual(rafael_user.team_profile.role, "UI/UX Designer")
        self.assertFalse(rafael_user.team_profile.can_access_vault)

        # Valida login real com a nova conta criada
        self.assertTrue(self.client.login(username="rafael_wsa", password="RafaelPassword2026!"))
        self.client.force_login(admin_user)

        # 4. Admin Geral edita dados e redefine senha via POST /users/save/
        resp_edit = self.client.post(
            "/users/save/",
            {
                "view": "users",
                "user_id": str(rafael_user.id),
                "full_name": "Rafael Lead Designer",
                "handle": "rafael",
                "role": "Lead Designer",
                "username": "rafael_wsa",
                "email": "rafael.lead@wsacreative.com.br",
                "password": "NovaSenhaRafael2026!",
                "accent_color": "#3b82f6",
                "can_access_vault": "1",
                "is_superuser": "0",
                "is_active": "1",
            },
        )
        self.assertEqual(resp_edit.status_code, 200)
        rafael_user.refresh_from_db()
        rafael_user.team_profile.refresh_from_db()
        self.assertEqual(rafael_user.team_profile.name, "Rafael Lead Designer")
        self.assertEqual(rafael_user.team_profile.role, "Lead Designer")
        self.assertTrue(rafael_user.team_profile.can_access_vault)
        self.assertTrue(rafael_user.check_password("NovaSenhaRafael2026!"))

        # 5. Alternância 1-clique de toggle-vault e toggle-active
        resp_tv = self.client.post(f"/users/{rafael_user.id}/toggle-vault/")
        self.assertEqual(resp_tv.status_code, 200)
        rafael_user.team_profile.refresh_from_db()
        self.assertFalse(rafael_user.team_profile.can_access_vault)

        resp_ta = self.client.post(f"/users/{rafael_user.id}/toggle-active/")
        self.assertEqual(resp_ta.status_code, 200)
        rafael_user.refresh_from_db()
        self.assertFalse(rafael_user.is_active)

        # 6. Trava anti-auto-bloqueio impede o Admin logado de desativar a si mesmo ou remover seu is_superuser
        self.client.post(f"/users/{admin_user.id}/toggle-active/")
        admin_user.refresh_from_db()
        self.assertTrue(admin_user.is_active)

        self.client.post(
            "/users/save/",
            {
                "user_id": str(admin_user.id),
                "full_name": "Caio Root",
                "handle": "caioroot",
                "username": admin_user.username,
                "is_superuser": "0",
                "is_active": "1",
            },
        )
        admin_user.refresh_from_db()
        self.assertTrue(admin_user.is_superuser)

        self.client.post(
            "/users/save/",
            {
                "user_id": str(admin_user.id),
                "full_name": "Caio Root",
                "handle": "caioroot",
                "username": admin_user.username,
                "is_superuser": "1",
                "is_active": "0",
            },
        )
        admin_user.refresh_from_db()
        self.assertTrue(admin_user.is_active)

    def test_phase12_whatsapp_reminders_gemini_extraction_celery_dispatch_drawer_and_auto_cancel(self):
        from datetime import timedelta
        from unittest.mock import patch
        from django.contrib.auth.models import User
        from django.utils import timezone
        from apps.core.models import SystemIntegrationConfig
        from apps.tasks.models import TaskReminder
        from apps.tasks.tasks import dispatch_due_reminders

        config = SystemIntegrationConfig.get_solo()
        config.evolution_api_url = "http://127.0.0.1:8080"
        config.evolution_api_key = "evo-test-key-2026"
        config.evolution_instance_name = "cc-prod-instance"
        config.default_whatsapp_number = "5521999998888"
        config.whatsapp_notifications_enabled = True
        config.save()

        admin_user = User.objects.create_superuser(
            username="caio_wa_admin",
            email="caio@wsacreative.com.br",
            password="AdminWaPassword2026!",
        )
        owner_user = User.objects.create_user(
            username="henrique_wa",
            email="henrique@wsacreative.com.br",
            password="HenriqueWaPassword2026!",
        )
        other_user = User.objects.create_user(
            username="intruso_wa",
            email="intruso@wsacreative.com.br",
            password="IntrusoWaPassword2026!",
        )

        member_henrique = TeamMember.objects.get(handle="henrique")
        member_henrique.user = owner_user
        member_henrique.whatsapp_number = "5521988887777"
        member_henrique.save()

        # 1. Criação de tarefa via WhatsApp/Áudio JSON contendo `reminders` cria TaskReminder
        #    no banco e dispara confirmação automática via Evolution API v2
        remind_iso = (timezone.localtime(timezone.now()) - timedelta(minutes=2)).strftime(
            "%Y-%m-%dT%H:%M:%S-03:00"
        )
        gemini_payload = {
            "action": "upsert",
            "title": "Subir campanha Black Friday no Portal MS CREW",
            "workspace": "wsa",
            "project": "Portal MS CREW",
            "assignee": "henrique",
            "shift": "afternoon",
            "impact": 5,
            "urgency": 5,
            "is_top_three": True,
            "notes": "Áudio com pedido de lembrete periódico a cada 60 min.",
            "reminders": [
                {
                    "remind_at": remind_iso,
                    "repeat_interval_minutes": 60,
                    "max_repeats": 2,
                    "custom_message": "Conferir aprovação dos criativos",
                }
            ],
        }

        with patch("apps.core.whatsapp_service.send_whatsapp_text") as mock_send_wa:
            mock_send_wa.return_value = {"ok": True, "number": "5521988887777"}
            resp_wh = self.client.post(
                "/api/webhooks/whatsapp",
                data=json.dumps(
                    {
                        "text": json.dumps(gemini_payload),
                        "sender": "5521988887777@s.whatsapp.net",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(resp_wh.status_code, 200)
            self.assertTrue(mock_send_wa.called)
            confirm_num, confirm_text = mock_send_wa.call_args[0]
            self.assertEqual(confirm_num, "5521988887777")
            self.assertIn("Subir campanha Black Friday no Portal MS CREW", confirm_text)
            self.assertIn("Lembretes Agendados", confirm_text)

        created_task = Task.objects.filter(
            title="Subir campanha Black Friday no Portal MS CREW"
        ).first()
        self.assertIsNotNone(created_task)
        self.assertEqual(created_task.owner, owner_user)

        reminder = TaskReminder.objects.filter(task=created_task).first()
        self.assertIsNotNone(reminder)
        self.assertTrue(reminder.is_active)
        self.assertEqual(reminder.repeat_interval_minutes, 60)
        self.assertEqual(reminder.max_repeats, 2)
        self.assertEqual(reminder.target_whatsapp, "5521988887777")

        # 2. dispatch_due_reminders() envia lembrete vencido, reagenda periódico e desativa ao atingir max_repeats
        fixed_daytime = timezone.localtime(timezone.now()).replace(
            hour=14, minute=30, second=0, microsecond=0
        )
        reminder.remind_at = fixed_daytime - timedelta(minutes=5)
        reminder.save(update_fields=["remind_at"])

        with (
            patch("apps.tasks.tasks.timezone.now", return_value=fixed_daytime),
            patch("apps.tasks.tasks.send_whatsapp_text") as mock_dispatch_send,
        ):
            mock_dispatch_send.return_value = {"ok": True, "number": "5521988887777"}

            # 1º disparo (1/2) -> envia e reagenda (+60m), permanecendo ativo
            res1 = dispatch_due_reminders()
            self.assertEqual(res1["sent"], 1)
            self.assertEqual(res1["rescheduled"], 1)
            reminder.refresh_from_db()
            self.assertEqual(reminder.attempts_sent, 1)
            self.assertTrue(reminder.is_active)
            self.assertFalse(reminder.is_sent)
            self.assertGreater(reminder.remind_at, fixed_daytime)

            # Retrocede remind_at para forçar o 2º disparo (2/2 == max_repeats) -> desativa e marca is_sent=True
            reminder.remind_at = fixed_daytime - timedelta(minutes=1)
            reminder.save(update_fields=["remind_at"])
            res2 = dispatch_due_reminders()
            self.assertEqual(res2["sent"], 1)
            self.assertEqual(res2["rescheduled"], 0)
            reminder.refresh_from_db()
            self.assertEqual(reminder.attempts_sent, 2)
            self.assertFalse(reminder.is_active)
            self.assertTrue(reminder.is_sent)

        # 3. Endpoints manuais do Drawer (/tasks/<pk>/reminders/add/ e /delete/) + Anti-IDOR (403)
        self.client.force_login(other_user)
        resp_idor_add = self.client.post(
            f"/tasks/{created_task.id}/reminders/add/",
            {"remind_at": "2026-11-25T10:00", "repeat_interval_minutes": "30"},
        )
        self.assertEqual(resp_idor_add.status_code, 403)

        self.client.force_login(owner_user)
        resp_owner_add = self.client.post(
            f"/tasks/{created_task.id}/reminders/add/",
            {
                "remind_at": "2026-11-25T10:00",
                "repeat_interval_minutes": "30",
                "custom_message": "Lembrete manual criado pelo Drawer",
            },
        )
        self.assertEqual(resp_owner_add.status_code, 200)
        manual_rem = TaskReminder.objects.filter(
            task=created_task,
            custom_message="Lembrete manual criado pelo Drawer",
        ).first()
        self.assertIsNotNone(manual_rem)
        self.assertEqual(manual_rem.repeat_interval_minutes, 30)
        self.assertTrue(manual_rem.is_active)

        # Intruso tentando deletar o lembrete manual recebe 403
        self.client.force_login(other_user)
        self.assertEqual(
            self.client.post(f"/tasks/reminders/{manual_rem.id}/delete/").status_code,
            403,
        )

        # Dono cria um segundo lembrete e exclui o primeiro pelo endpoint
        self.client.force_login(owner_user)
        second_rem = TaskReminder.objects.create(
            task=created_task,
            user=owner_user,
            target_whatsapp="5521988887777",
            remind_at=timezone.now() + timedelta(hours=2),
            is_active=True,
        )
        resp_del_rem = self.client.post(f"/tasks/reminders/{manual_rem.id}/delete/")
        self.assertEqual(resp_del_rem.status_code, 200)
        self.assertFalse(TaskReminder.objects.filter(pk=manual_rem.id).exists())

        # 4. Concluir a tarefa (status="done") desativa automaticamente todos os TaskReminder pendentes
        self.assertTrue(second_rem.is_active)
        created_task.status = Task.Status.DONE
        created_task.save()
        second_rem.refresh_from_db()
        self.assertFalse(second_rem.is_active)

        # 5. Verificação estrita de zero cores proibidas nos templates atualizados
        self.client.force_login(admin_user)
        drawer_html = self.client.get(f"/tasks/{created_task.id}/detail/").content.decode("utf-8")
        users_html = self.client.get("/?view=users").content.decode("utf-8")
        integrations_html = self.client.get("/?view=integrations").content.decode("utf-8")
        forbidden = ["purple", "violet", "indigo", "emerald", "teal", "bg-green", "text-green"]
        for doc in (drawer_html, users_html, integrations_html):
            for word in forbidden:
                self.assertNotIn(word, doc.lower())





