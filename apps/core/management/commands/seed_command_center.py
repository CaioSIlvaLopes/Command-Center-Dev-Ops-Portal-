from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import Workspace, Project, TeamMember, CredentialEntry
from apps.tasks.models import Task, SubTask
from apps.integrations.models import WebhookEvent
from apps.timetracker.models import TimeLog


class Command(BaseCommand):
    help = "Popula o Command Center com Workspaces, Projetos reais, Equipe (TeamMember), Subtarefas, TimeLogs e 8 tarefas iniciais."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            action="store_true",
            help="Não limpa tarefas existentes antes de popular.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not options.get("keep"):
            TimeLog.objects.all().delete()
            WebhookEvent.objects.all().delete()
            SubTask.objects.all().delete()
            Task.objects.all().delete()

        # 1. Workspaces (Frentes de Trabalho)
        workspaces_data = [
            {
                "slug": "wsa",
                "name": "WSA Creative",
                "accent_color": "#3b82f6",
                "icon_name": "globe",
                "shortcut_key": "Alt+1",
            },
            {
                "slug": "saas",
                "name": "SaaS & Dev",
                "accent_color": "#3b82f6",
                "icon_name": "code",
                "shortcut_key": "Alt+2",
            },
            {
                "slug": "ecom",
                "name": "E-commerce",
                "accent_color": "#f97316",
                "icon_name": "shopping-bag",
                "shortcut_key": "Alt+3",
            },
            {
                "slug": "facul",
                "name": "Faculdade SI",
                "accent_color": "#3b82f6",
                "icon_name": "academic-cap",
                "shortcut_key": "Alt+4",
            },
            {
                "slug": "games",
                "name": "Indie Games",
                "accent_color": "#f97316",
                "icon_name": "cpu",
                "shortcut_key": "Alt+5",
            },
        ]

        workspaces = {}
        for ws_data in workspaces_data:
            ws, _ = Workspace.objects.update_or_create(
                slug=ws_data["slug"],
                defaults={
                    "name": ws_data["name"],
                    "accent_color": ws_data["accent_color"],
                    "icon_name": ws_data["icon_name"],
                    "shortcut_key": ws_data["shortcut_key"],
                },
            )
            workspaces[ws.slug] = ws

        # 2. Projetos Reais Vinculados (com github_repo_name, repo_url e admin_or_panel_url para o Launchpad)
        projects_data = [
            {
                "name": "Deskify",
                "workspace": workspaces["saas"],
                "client_or_store": "SaaS B2B Helpdesk",
                "github_repo_name": "Sistema-de-Chamado",
                "repo_url": "https://github.com/caio/Sistema-de-Chamado",
                "admin_or_panel_url": "https://app.deskify.io/admin",
            },
            {
                "name": "Date & Mate",
                "workspace": workspaces["saas"],
                "client_or_store": "Mobile & Web App",
                "github_repo_name": "date-and-mate",
                "repo_url": "https://github.com/caio/date-and-mate",
                "admin_or_panel_url": "https://console.firebase.google.com/",
            },
            {
                "name": "Portal MS CREW",
                "workspace": workspaces["wsa"],
                "client_or_store": "MS CREW Agency",
                "github_repo_name": "MSCREW-Site",
                "repo_url": "https://github.com/wsa-creative/MSCREW-Site",
                "admin_or_panel_url": "https://mscrew.com.br/wp-admin",
            },
            {
                "name": "Info Mundi Office",
                "workspace": workspaces["ecom"],
                "client_or_store": "Mercado Livre Full / Shopee",
                "github_repo_name": "",
                "repo_url": "",
                "admin_or_panel_url": "https://www.mercadolivre.com.br/vendas/omni/lista",
            },
            {
                "name": "Mercuria Shop",
                "workspace": workspaces["ecom"],
                "client_or_store": "Shopee Oficial",
                "github_repo_name": "",
                "repo_url": "",
                "admin_or_panel_url": "https://seller.shopee.com.br/",
            },
            {
                "name": "NorthVale",
                "workspace": workspaces["games"],
                "client_or_store": "Steam Indie RPG",
                "github_repo_name": "NorthVale",
                "repo_url": "https://github.com/caio/NorthVale",
                "admin_or_panel_url": "https://partner.steamgames.com/",
            },
        ]

        projects = {}
        for proj_data in projects_data:
            proj, _ = Project.objects.update_or_create(
                name=proj_data["name"],
                workspace=proj_data["workspace"],
                defaults={
                    "client_or_store": proj_data["client_or_store"],
                    "github_repo_name": proj_data["github_repo_name"],
                    "repo_url": proj_data["repo_url"],
                    "admin_or_panel_url": proj_data["admin_or_panel_url"],
                    "is_active": True,
                },
            )
            projects[proj.name] = proj

        # 2.5 Responsáveis / Equipe (TeamMember)
        members_data = [
            {
                "handle": "caio",
                "name": "Caio",
                "role": "Head & Full-Stack",
                "accent_color": "#3b82f6",
                "avatar_initials": "CA",
            },
            {
                "handle": "pedro",
                "name": "Pedro",
                "role": "Dev / Backend",
                "accent_color": "#3b82f6",
                "avatar_initials": "PE",
            },
            {
                "handle": "henrique",
                "name": "Henrique",
                "role": "Parceiro / Projetos",
                "accent_color": "#3b82f6",
                "avatar_initials": "HE",
            },
            {
                "handle": "monica",
                "name": "Mônica",
                "role": "Ops / E-commerce",
                "accent_color": "#f97316",
                "avatar_initials": "MO",
            },
        ]
        members = {}
        for m_data in members_data:
            member, _ = TeamMember.objects.update_or_create(
                handle=m_data["handle"],
                defaults={
                    "name": m_data["name"],
                    "role": m_data["role"],
                    "accent_color": m_data["accent_color"],
                    "avatar_initials": m_data["avatar_initials"],
                    "is_active": True,
                },
            )
            members[member.handle] = member

        today = date.today()
        now = timezone.now()

        # 3. 8 Tarefas Realistas Distribuídas entre Top 3, Turnos e Inbox
        tasks_data = [
            # --- TOP 3 DO DIA ---
            {
                "workspace": workspaces["saas"],
                "project": projects["Deskify"],
                "title": "Refatorar motor de SLA e filas multi-tenant no Deskify v2",
                "notes": (
                    "Separar workers Celery por fila de prioridade (high/default) e garantir "
                    "isolamento por tenant_id nas queries de tickets abertos."
                ),
                "status": Task.Status.DOING,
                "shift": Task.Shift.MORNING,
                "impact": 5,
                "urgency": 5,
                "is_top_three": True,
                "assigned_to": "@pedro",
                "due_date": today,
                "external_ref": "GH#142",
                "_subtasks": [
                    ("Criar índice composto (tenant_id, status, sla_due_at)", True),
                    ("Implementar cálculo de pausa de SLA fora do expediente", True),
                    ("Validar testes de carga com 5k tickets simultâneos", False),
                ],
                "_logged_minutes": [55, 40],
            },
            {
                "workspace": workspaces["ecom"],
                "project": projects["Info Mundi Office"],
                "title": "Reposição urgente de estoque Full e ajuste de margem (Cadeira Ergonômica Pro)",
                "notes": (
                    "Conferir nota fiscal de entrada do fornecedor, emitir etiquetas térmicas "
                    "FBM e recalcular margem líquida após novo frete Full."
                ),
                "status": Task.Status.TODO,
                "shift": Task.Shift.AFTERNOON,
                "impact": 5,
                "urgency": 5,
                "is_top_three": True,
                "assigned_to": "@monica",
                "due_date": today,
                "external_ref": "SKU-IMO-8841",
                "_subtasks": [
                    ("Auditar contagem física no galpão (48 unidades)", True),
                    ("Gerar plano de envio Mercado Envios Full", False),
                    ("Atualizar planilha de margem líquida p/ 24.5%", False),
                ],
                "_logged_minutes": [45],
            },
            {
                "workspace": workspaces["wsa"],
                "project": projects["Portal MS CREW"],
                "title": "Deploy do checkout transparente e painel de membros no Portal MS CREW",
                "notes": (
                    "Revisar webhooks de confirmação PIX/Cartão e liberar acesso imediato "
                    "ao módulo de membros após pagamento aprovado."
                ),
                "status": Task.Status.TODO,
                "shift": Task.Shift.MORNING,
                "impact": 5,
                "urgency": 4,
                "is_top_three": True,
                "assigned_to": "@henrique",
                "due_date": today,
                "external_ref": "GH#89",
                "_subtasks": [
                    ("Configurar chaves de produção do gateway", True),
                    ("Testar webhook de renovação de assinatura", True),
                    ("Homologar fluxo mobile iOS/Android", False),
                ],
                "_logged_minutes": [60, 30],
            },
            # --- FILA DE TURNOS ---
            {
                "workspace": workspaces["saas"],
                "project": projects["Date & Mate"],
                "title": "Implementar algoritmo de geolocalização e push notifications no Date & Mate",
                "notes": "Utilizar PostGIS / Haversine otimizado com cache Redis para raio de 25km.",
                "status": Task.Status.TODO,
                "shift": Task.Shift.MORNING,
                "impact": 4,
                "urgency": 4,
                "is_top_three": False,
                "assigned_to": "@pedro",
                "due_date": today + timedelta(days=1),
                "external_ref": "GH#204",
                "_subtasks": [
                    ("Configurar payload FCM para notificações silenciosas", True),
                    ("Implementar endpoint de atualização de coordenadas", False),
                ],
                "_logged_minutes": [35],
            },
            {
                "workspace": workspaces["ecom"],
                "project": projects["Mercuria Shop"],
                "title": "Otimizar campanha Shopee Ads e revisar criativos da semana na Mercuria Shop",
                "notes": "Pausar palavras-chave com ROAS < 4.5 e elevar lance nos 3 SKUs curva A.",
                "status": Task.Status.TODO,
                "shift": Task.Shift.AFTERNOON,
                "impact": 4,
                "urgency": 3,
                "is_top_three": False,
                "assigned_to": "@monica",
                "due_date": today + timedelta(days=2),
                "external_ref": "SKU-MRC-310",
                "_subtasks": [
                    ("Exportar relatório de buscas dos últimos 7 dias", False),
                    ("Subir 3 novos vídeos curtos de demonstração", False),
                ],
                "_logged_minutes": [50],
            },
            {
                "workspace": workspaces["facul"],
                "project": None,
                "title": "Entregar modelagem relacional e artigo de Engenharia de Software II",
                "notes": "Incluir diagramas MER, especificação de requisitos não-funcionais e revisão ABNT.",
                "status": Task.Status.TODO,
                "shift": Task.Shift.NIGHT,
                "impact": 4,
                "urgency": 4,
                "is_top_three": False,
                "assigned_to": "",
                "due_date": today + timedelta(days=1),
                "external_ref": "SI-2026-T2",
                "_subtasks": [
                    ("Finalizar diagrama de arquitetura em 4 camadas", True),
                    ("Revisar referências bibliográficas", False),
                ],
                "_logged_minutes": [40],
            },
            {
                "workspace": workspaces["games"],
                "project": projects["NorthVale"],
                "title": "Prototipar sistema de iluminação dinâmica e shaders de neblina em NorthVale",
                "notes": "Testar performance do shader volumétrico mantendo 60 FPS estáveis em GPUs integradas.",
                "status": Task.Status.TODO,
                "shift": Task.Shift.NIGHT,
                "impact": 3,
                "urgency": 3,
                "is_top_three": False,
                "assigned_to": "@henrique",
                "due_date": today + timedelta(days=4),
                "external_ref": "NV-047",
                "_subtasks": [
                    ("Ajustar curva de transição ciclo dia/noite", True),
                    ("Integrar partículas de neve com direção do vento", False),
                ],
                "_logged_minutes": [45],
            },
            # --- INBOX (Triagem de Webhooks) ---
            {
                "workspace": workspaces["ecom"],
                "project": projects["Info Mundi Office"],
                "title": "[Alerta Estoque] Ruptura iminente em Suporte Articulado Dual Monitor (Restam 3 un.)",
                "notes": "Disparado automaticamente pelo monitor de estoque Shopee/Mercado Livre.",
                "status": Task.Status.INBOX,
                "shift": Task.Shift.AFTERNOON,
                "impact": 4,
                "urgency": 5,
                "is_top_three": False,
                "assigned_to": "@monica",
                "due_date": today,
                "external_ref": "SKU-IMO-2099",
                "_webhook_source": WebhookEvent.Source.ECOMMERCE,
                "_webhook_payload": {
                    "store": "Info Mundi",
                    "sku": "SKU-IMO-2099",
                    "issue": "Ruptura iminente em Suporte Articulado Dual Monitor (Restam 3 un.)",
                },
                "_subtasks": [],
                "_logged_minutes": [],
            },
        ]

        created_tasks = []
        offset_hours = 1
        for item in tasks_data:
            wh_source = item.pop("_webhook_source", None)
            wh_payload = item.pop("_webhook_payload", None)
            subtasks_list = item.pop("_subtasks", [])
            logged_minutes_list = item.pop("_logged_minutes", [])

            task = Task.objects.create(**item)
            created_tasks.append(task)

            for st_title, st_done in subtasks_list:
                SubTask.objects.create(task=task, title=st_title, is_done=st_done)

            for mins in logged_minutes_list:
                end_dt = now - timedelta(hours=offset_hours)
                start_dt = end_dt - timedelta(minutes=mins)
                TimeLog.objects.create(
                    task=task,
                    start_time=start_dt,
                    end_time=end_dt,
                    duration_minutes=mins,
                    is_running=False,
                )
                offset_hours += 1

            if wh_source and wh_payload:
                WebhookEvent.objects.create(
                    source=wh_source,
                    event_type="stock.alert",
                    payload=wh_payload,
                    processed=True,
                    created_task=task,
                )

        # Iniciar cronômetro ativo na primeira tarefa Top 3 (Deskify)
        first_doing = created_tasks[0]
        TimeLog.objects.create(
            task=first_doing,
            start_time=now - timedelta(minutes=12, seconds=34),
            is_running=True,
        )

        # 5. Cofre de Credenciais & Acessos (Vault)
        vault_seed_data = [
            {
                "title": "E-mail Corporativo WSA Creative",
                "workspace": workspaces["wsa"],
                "project": projects.get("Portal MS CREW"),
                "category": CredentialEntry.Category.EMAIL,
                "username_or_email": "contato@wsacreative.com.br",
                "secret": "Wsa@Creative#2026!Secure",
                "service_url": "https://mail.google.com",
                "recovery_info": "2FA Google Authenticator (Celular Caio)",
                "notes": "Conta central de disparo de propostas comerciais e acessos de clientes da agência.",
            },
            {
                "title": "Seller Centre — Mercuria Shop",
                "workspace": workspaces["ecom"],
                "project": projects.get("Mercuria Shop"),
                "category": CredentialEntry.Category.ECOMMERCE,
                "username_or_email": "operacao@mercuriashop.com.br",
                "secret": "Shopee$Mercuria#9920",
                "service_url": "https://seller.shopee.com.br",
                "recovery_info": "SMS / WhatsApp Operacional (@monica)",
                "notes": "Painel principal para gestão de estoque, Shopee Ads e emissão de etiquetas.",
            },
            {
                "title": "Mercado Livre Full — Info Mundi Office",
                "workspace": workspaces["ecom"],
                "project": projects.get("Info Mundi Office"),
                "category": CredentialEntry.Category.ECOMMERCE,
                "username_or_email": "vendas@infomundioffice.com.br",
                "secret": "Meli@InfoMundi#Full2026",
                "service_url": "https://www.mercadolivre.com.br/vendas/omni/lista",
                "recovery_info": "App Mercado Pago c/ Biometria",
                "notes": "Acesso de gestão Full e conciliação de repasses.",
            },
            {
                "title": "VPS Produção & PostgreSQL — Deskify",
                "workspace": workspaces["saas"],
                "project": projects.get("Deskify"),
                "category": CredentialEntry.Category.INFRA,
                "username_or_email": "deploy@deskify.io",
                "secret": "Vps#Deskify$Root2026!Key",
                "service_url": "https://app.deskify.io/admin",
                "recovery_info": "Chave SSH ed25519 no cofre local",
                "notes": "Servidor principal Docker + Redis + PostgreSQL multi-tenant.",
            },
        ]

        for v_item in vault_seed_data:
            raw_sec = v_item.pop("secret")
            cred, _ = CredentialEntry.objects.update_or_create(
                title=v_item["title"],
                workspace=v_item["workspace"],
                defaults=v_item,
            )
            cred.set_secret(raw_sec)
            cred.save(update_fields=["encrypted_secret"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Command Center populado com sucesso: {len(workspaces)} Workspaces, "
                f"{len(projects)} Projetos, {len(members)} Responsáveis, {len(created_tasks)} Tarefas e {len(vault_seed_data)} Credenciais no Cofre!"
            )
        )
