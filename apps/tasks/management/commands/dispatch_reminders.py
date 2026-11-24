from django.core.management.base import BaseCommand
from apps.tasks.tasks import dispatch_due_reminders


class Command(BaseCommand):
    help = "Processa e dispara imediatamente todos os lembretes de tarefas vencidos via WhatsApp (Evolution API v2)."

    def handle(self, *args, **options):
        result = dispatch_due_reminders()
        self.stdout.write(
            self.style.SUCCESS(
                f"Disparo de lembretes concluído: "
                f"{result.get('sent', 0)} enviado(s), "
                f"{result.get('rescheduled', 0)} reagendado(s), "
                f"{result.get('deferred_quiet_hours', 0)} adiado(s) na janela de silêncio, "
                f"{result.get('failed', 0)} falha(s)."
            )
        )
