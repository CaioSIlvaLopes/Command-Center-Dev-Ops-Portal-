import math
from django.db import models
from django.utils import timezone


class TimeLog(models.Model):
    """Registro de sessão de foco/tempo trabalhado em uma tarefa."""

    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="time_logs",
        verbose_name="Tarefa",
    )
    start_time = models.DateTimeField("Início", default=timezone.now)
    end_time = models.DateTimeField("Término", null=True, blank=True)
    duration_minutes = models.PositiveIntegerField("Duração (minutos)", default=0)
    is_running = models.BooleanField("Em Execução", default=True, db_index=True)

    class Meta:
        verbose_name = "Registro de Tempo"
        verbose_name_plural = "Registros de Tempo"
        ordering = ["-start_time"]

    def __str__(self) -> str:
        state = "RODANDO" if self.is_running else f"{self.duration_minutes}m"
        return f"{self.task.title} [{state}]"

    @property
    def elapsed_seconds(self) -> int:
        ref_end = self.end_time or timezone.now()
        delta = (ref_end - self.start_time).total_seconds()
        return max(0, int(delta))

    def stop(self) -> None:
        if not self.is_running:
            return
        self.end_time = timezone.now()
        seconds = max(0.0, (self.end_time - self.start_time).total_seconds())
        self.duration_minutes = max(1, int(math.ceil(seconds / 60.0))) if seconds > 0 else 0
        self.is_running = False
        self.save(update_fields=["end_time", "duration_minutes", "is_running"])

