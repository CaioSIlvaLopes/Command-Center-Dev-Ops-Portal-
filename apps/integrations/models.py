from django.db import models


class WebhookEvent(models.Model):
    """Log auditável de eventos recebidos via Webhooks externos (GitHub, WhatsApp, E-commerce)."""

    class Source(models.TextChoices):
        GITHUB = "github", "GitHub"
        WHATSAPP = "whatsapp", "WhatsApp"
        ECOMMERCE = "ecommerce", "E-commerce (Shopee/ML)"
        CUSTOM = "custom", "Customizado"

    source = models.CharField(
        "Origem",
        max_length=20,
        choices=Source.choices,
        default=Source.CUSTOM,
        db_index=True,
    )
    event_type = models.CharField(
        "Tipo de Evento",
        max_length=80,
        blank=True,
        default="",
        help_text="Ex: issues.opened, pull_request.closed, messages.upsert",
    )
    payload = models.JSONField("Payload Recebido")
    processed = models.BooleanField("Processado", default=False, db_index=True)
    error_message = models.TextField("Mensagem de Erro", blank=True, default="")
    created_task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        verbose_name="Tarefa Gerada/Vinculada",
    )
    created_at = models.DateTimeField("Recebido em", auto_now_add=True)

    class Meta:
        verbose_name = "Evento de Webhook"
        verbose_name_plural = "Eventos de Webhook"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        ev = f" ({self.event_type})" if self.event_type else ""
        return f"[{self.get_source_display()}{ev}] #{self.pk} - {'Processado' if self.processed else 'Pendente'}"
