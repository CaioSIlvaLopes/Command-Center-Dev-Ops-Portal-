from django.conf import settings
from django.db import models
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify


class Task(models.Model):
    """Tarefa central do Command Center com cálculo nativo de prioridade no banco."""

    class Status(models.TextChoices):
        INBOX = "inbox", "Inbox (Triagem)"
        TODO = "todo", "A Fazer"
        DOING = "doing", "Em Execução"
        DONE = "done", "Concluída"

    class Shift(models.TextChoices):
        MORNING = "morning", "Manhã - Deep Work"
        AFTERNOON = "afternoon", "Tarde - Ops/E-com"
        NIGHT = "night", "Noite - Estudos/Games"

    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="tasks",
        verbose_name="Frente de Trabalho",
    )
    project = models.ForeignKey(
        "core.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Projeto / Loja",
    )
    assignee = models.ForeignKey(
        "core.TeamMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Responsável (TeamMember)",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_tasks",
        db_index=True,
        verbose_name="Proprietário (Usuário)",
    )
    title = models.CharField("Título da Tarefa", max_length=255)
    notes = models.TextField("Notas e Especificações", blank=True, default="")
    status = models.CharField(
        "Status",
        max_length=20,
        choices=Status.choices,
        default=Status.TODO,
        db_index=True,
    )
    shift = models.CharField(
        "Turno de Execução",
        max_length=20,
        choices=Shift.choices,
        default=Shift.MORNING,
        db_index=True,
    )
    impact = models.PositiveSmallIntegerField("Impacto (1-5)", default=3)
    urgency = models.PositiveSmallIntegerField("Urgência (1-5)", default=3)
    priority_score = models.GeneratedField(
        expression=F("impact") * F("urgency"),
        output_field=models.PositiveSmallIntegerField(),
        db_persist=True,
    )
    is_top_three = models.BooleanField("Top 3 do Dia", default=False, db_index=True)
    assigned_to = models.CharField("Responsável (@)", max_length=50, blank=True, default="")
    due_date = models.DateField("Data Limite", null=True, blank=True)
    external_ref = models.CharField(
        "Referência Externa (GitHub/SKU)",
        max_length=120,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField("Criada em", auto_now_add=True)
    completed_at = models.DateTimeField("Concluída em", null=True, blank=True)

    class Meta:
        verbose_name = "Tarefa"
        verbose_name_plural = "Tarefas"
        ordering = ["-is_top_three", "-priority_score", "-created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_assignee_id = self.__dict__.get("assignee_id")

    def __str__(self) -> str:
        prefix = "[TOP 3] " if self.is_top_three else ""
        return f"{prefix}{self.title} (#{self.workspace.slug})"

    def _sync_assignee_and_assigned_to(self):
        """
        Sincroniza bidirecionalmente `assignee` (FK TeamMember) e `assigned_to` (string @handle):
        - Se `self.assignee` estiver definido -> `self.assigned_to = f"@{self.assignee.handle}"`
        - Se `self.assignee` foi removido (era != None e agora é None) -> `self.assigned_to = ""`
        - Se `self.assignee` é None mas `self.assigned_to` foi preenchido (ex: via Ctrl+K ou Webhook) ->
          busca ou cria o `TeamMember` correspondente e vincula `self.assignee`.
        """
        from apps.core.models import TeamMember

        if self.assignee_id is not None:
            if self.assignee:
                self.assigned_to = f"@{self.assignee.handle}"
            return

        # Se a tarefa já existia com um assignee e ele foi explicitamente removido (assignee=None)
        if self.pk and self._initial_assignee_id is not None:
            self.assigned_to = ""
            return

        raw_assigned = (self.assigned_to or "").strip()
        if raw_assigned:
            handle_clean = slugify(raw_assigned.lstrip("@").strip().lower())
            if handle_clean:
                member = TeamMember.objects.filter(handle__iexact=handle_clean).first()
                if not member:
                    member = TeamMember.objects.create(
                        handle=handle_clean,
                        name=handle_clean.capitalize(),
                    )
                self.assignee = member
                self.assigned_to = f"@{member.handle}"
            else:
                self.assigned_to = ""
        else:
            self.assigned_to = ""

    def save(self, *args, **kwargs):
        self._demoted_task_title = None
        self.impact = max(1, min(5, int(self.impact or 3)))
        self.urgency = max(1, min(5, int(self.urgency or 3)))

        self._sync_assignee_and_assigned_to()

        if self.status == self.Status.DONE:
            if not self.completed_at:
                self.completed_at = timezone.now()
            self.is_top_three = False
        else:
            if self.completed_at is not None:
                self.completed_at = None

            # Regra de Negócio: Máximo de 3 tarefas ativas (status != 'done') no Top 3 do Dia (isolado por owner)
            if self.is_top_three:
                active_top_qs = (
                    Task.objects.filter(is_top_three=True, owner_id=self.owner_id)
                    .exclude(status=self.Status.DONE)
                    .exclude(pk=self.pk)
                    .order_by("created_at", "pk")
                )
                active_top_list = list(active_top_qs)
                while len(active_top_list) >= 3:
                    oldest_task = active_top_list.pop(0)
                    oldest_task.is_top_three = False
                    Task.objects.filter(pk=oldest_task.pk).update(is_top_three=False)
                    self._demoted_task_title = oldest_task.title

        super().save(*args, **kwargs)
        self._initial_assignee_id = self.assignee_id

    @property
    def is_running_timer(self) -> bool:
        return any(log.is_running for log in self.time_logs.all())

    @property
    def total_logged_minutes(self) -> int:
        return sum(log.duration_minutes for log in self.time_logs.all())

    @property
    def subtasks_total(self) -> int:
        return len(self.subtasks.all())

    @property
    def subtasks_done(self) -> int:
        return sum(1 for st in self.subtasks.all() if st.is_done)

    @property
    def subtasks_progress_pct(self) -> int:
        total = self.subtasks_total
        if total == 0:
            return 0
        return int(round((self.subtasks_done / total) * 100))

    @property
    def origin_badge(self) -> dict:
        """Retorna metadados da origem de captura da tarefa (Web/Ctrl+K, WhatsApp, GitHub, Áudio IA, Gemini Sync)."""
        events = list(self.webhook_events.all())
        notes_str = self.notes or ""
        ext_str = self.external_ref or ""

        if any(wh.event_type == "audio.voice_note" for wh in events) or "[Áudio processado" in notes_str:
            return {"label": "Áudio IA", "code": "audio", "is_urgent": True}
        if any(wh.event_type == "gemini.sync" or wh.source == "custom" for wh in events):
            return {"label": "Gemini Sync", "code": "gemini", "is_urgent": False}
        if any(wh.source == "whatsapp" for wh in events):
            return {"label": "WhatsApp", "code": "whatsapp", "is_urgent": False}
        if any(wh.source == "github" for wh in events) or "github.com" in ext_str or "#" in ext_str:
            return {"label": "GitHub", "code": "github", "is_urgent": False}
        if any(wh.source == "ecommerce" for wh in events):
            return {"label": "E-commerce", "code": "ecommerce", "is_urgent": True}
        return {"label": "Web / Ctrl+K", "code": "web", "is_urgent": False}



class SubTask(models.Model):
    """Item de checklist (subtarefa) vinculado a uma Task."""

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="subtasks",
        verbose_name="Tarefa Principal",
    )
    title = models.CharField("Título da Subtarefa", max_length=200)
    is_done = models.BooleanField("Concluída", default=False)
    created_at = models.DateTimeField("Criada em", auto_now_add=True)

    class Meta:
        verbose_name = "Subtarefa"
        verbose_name_plural = "Subtarefas"
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        check = "x" if self.is_done else " "
        return f"[{check}] {self.title}"
