from typing import Optional
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q, QuerySet

from apps.tasks.models import Task


def get_default_superuser():
    """Retorna o primeiro superusuário cadastrado no sistema (ou None)."""
    User = get_user_model()
    return User.objects.filter(is_superuser=True).order_by("id").first()


def is_general_admin(user) -> bool:
    """
    Retorna True se `user.is_authenticated and user.is_superuser`,
    ou se em modo dev/test local `not settings.REQUIRE_AUTH and not user.is_authenticated`
    para preservar compatibilidade com testes legados que rodam sem login.
    """
    if user and getattr(user, "is_authenticated", False):
        return bool(user.is_superuser)
    if not getattr(settings, "REQUIRE_AUTH", False):
        return True
    return False


def can_user_access_vault(user) -> bool:
    """
    Retorna True se `is_general_admin(user)` OU se `user.is_authenticated`
    e possui `team_profile` com `can_access_vault=True`.
    """
    if is_general_admin(user):
        return True
    if user and getattr(user, "is_authenticated", False):
        if hasattr(user, "team_profile") and user.team_profile and user.team_profile.can_access_vault:
            return True
    return False


def get_visible_tasks_queryset(
    user,
    user_filter: str = "me",
    base_qs: Optional[QuerySet] = None,
) -> QuerySet:
    """
    Filtra o QuerySet de tarefas visíveis de acordo com o perfil do usuário:
    - Usuário Comum (`not is_general_admin(user)`):
      Retorna ESTRITAMENTE `qs.filter(Q(owner=user) | Q(assignee__user=user)).distinct()`.
      Ignora qualquer parâmetro `user_filter` enviado na URL.
    - Admin Geral (`is_general_admin(user)`):
      - `user_filter == "me"` (padrão): tarefas onde `Q(owner=user) | Q(assignee__user=user) | Q(owner__isnull=True)`.
      - `user_filter == "all"`: todas as tarefas do sistema (`qs`).
      - `user_filter` numérico (`user_id`): tarefas daquele usuário específico (`Q(owner_id=uid) | Q(assignee__user_id=uid)`).
    """
    qs = base_qs if base_qs is not None else Task.objects.all()

    if not is_general_admin(user):
        if not user or not getattr(user, "is_authenticated", False):
            return qs.none()
        return qs.filter(Q(owner=user) | Q(assignee__user=user)).distinct()

    # Admin Geral
    uf = str(user_filter or "me").strip().lower()
    if uf == "all":
        return qs
    if uf.isdigit():
        target_uid = int(uf)
        return qs.filter(Q(owner_id=target_uid) | Q(assignee__user_id=target_uid)).distinct()

    # Default: "me"
    if user and getattr(user, "is_authenticated", False):
        return qs.filter(
            Q(owner=user) | Q(assignee__user=user) | Q(owner__isnull=True)
        ).distinct()

    # Em modo dev/test sem login (AnonymousUser com REQUIRE_AUTH=False), exibe todas
    return qs


def user_can_access_task(user, task: Task) -> bool:
    """
    Validação Anti-IDOR para endpoints de tarefa, subtarefa e cronômetro:
    Retorna True se `is_general_admin(user)` ou `task.owner_id == user.id`
    ou `(task.assignee and task.assignee.user_id == user.id)`.
    """
    if is_general_admin(user):
        return True
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if task.owner_id == user.id:
        return True
    if task.assignee_id and task.assignee and task.assignee.user_id == user.id:
        return True
    return False
