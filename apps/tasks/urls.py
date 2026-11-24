from django.urls import path
from . import views

app_name = "tasks"

urlpatterns = [
    path("", views.dashboard_view, name="dashboard"),
    path("tasks/command/", views.command_bar_create_view, name="command_create"),
    path("tasks/gemini-paste/", views.gemini_paste_sync_view, name="gemini_paste"),
    path("tasks/voice-dictation/", views.voice_dictation_create_view, name="voice_dictation"),
    path("tasks/audio-upload/", views.audio_upload_sync_view, name="audio_upload"),
    path("tasks/<int:pk>/ai-subtasks/", views.task_ai_subtasks_view, name="ai_subtasks"),
    path("settings/audio-quota/save/", views.audio_quota_config_save_view, name="audio_quota_save"),
    path("tasks/<int:pk>/detail/", views.task_detail_drawer_view, name="task_detail"),
    path("tasks/<int:pk>/update/", views.task_update_detail_view, name="update_detail"),
    path("tasks/<int:pk>/subtasks/add/", views.subtask_add_view, name="subtask_add"),
    path("tasks/subtasks/<int:subtask_id>/toggle/", views.subtask_toggle_view, name="subtask_toggle"),
    path("tasks/subtasks/<int:subtask_id>/delete/", views.subtask_delete_view, name="subtask_delete"),
    path("tasks/<int:pk>/move/", views.task_move_view, name="move_task"),
    path("tasks/<int:pk>/toggle-top3/", views.task_toggle_top_three_view, name="toggle_top3"),
    path("tasks/<int:pk>/status/", views.task_status_update_view, name="update_status"),
    path("tasks/<int:pk>/shift/", views.task_shift_update_view, name="update_shift"),
    path("tasks/<int:pk>/triage/", views.task_triage_inbox_view, name="triage_inbox"),
    path("tasks/<int:pk>/priority/", views.task_priority_update_view, name="update_priority"),
    path("tasks/<int:pk>/assign/", views.task_assign_member_view, name="assign_member"),
    path("tasks/<int:pk>/delete/", views.task_delete_view, name="delete_task"),
    path("tasks/recalculate/", views.trigger_recalculate_priorities_view, name="recalculate"),
    path("tasks/simulate-webhook/", views.simulate_webhook_view, name="simulate_webhook"),
    path("tasks/webhooks/<int:event_id>/reprocess/", views.reprocess_webhook_event_view, name="reprocess_webhook"),
    # Phase 5: Gestão Dinâmica de Frentes (Workspaces), Projetos/Lojas (Projects) e Responsáveis (TeamMembers)
    path("settings/workspaces/save/", views.workspace_save_view, name="workspace_save"),
    path("settings/workspaces/<int:ws_id>/delete/", views.workspace_delete_view, name="workspace_delete"),
    path("settings/projects/save/", views.project_save_view, name="project_save"),
    path("settings/projects/<int:proj_id>/toggle/", views.project_toggle_active_view, name="project_toggle"),
    path("settings/projects/<int:proj_id>/delete/", views.project_delete_view, name="project_delete"),
    path("settings/members/save/", views.team_member_save_view, name="member_save"),
    path("settings/members/<int:member_id>/toggle/", views.team_member_toggle_active_view, name="member_toggle"),
    path("settings/members/<int:member_id>/delete/", views.team_member_delete_view, name="member_delete"),
    # Cofre de Credenciais & Acessos (Vault)
    path("vault/save/", views.vault_credential_save_view, name="vault_save"),
    path("vault/<int:pk>/delete/", views.vault_credential_delete_view, name="vault_delete"),
    path("vault/<int:pk>/secret/", views.vault_credential_secret_view, name="vault_secret"),
    # Gestão Dedicada de Usuários & Acessos (/?view=users)
    path("users/save/", views.user_account_save_view, name="user_save"),
    path("users/<int:pk>/toggle-active/", views.user_toggle_active_view, name="user_toggle_active"),
    path("users/<int:pk>/toggle-vault/", views.user_toggle_vault_view, name="user_toggle_vault"),
    # Fragmentos parciais individuais
    path("partials/top-three/", views.partial_top_three_view, name="partial_top_three"),
    path("partials/shift-queue/", views.partial_shift_queue_view, name="partial_shift_queue"),
    path("partials/inbox/", views.partial_inbox_list_view, name="partial_inbox"),
    path("partials/analytics/", views.partial_analytics_view, name="partial_analytics"),
    path("partials/integrations/", views.partial_integrations_view, name="partial_integrations"),
    path("partials/styleguide/", views.partial_styleguide_view, name="partial_styleguide"),
    path("partials/settings/", views.partial_settings_view, name="partial_settings"),
    path("partials/users/", views.partial_users_view, name="partial_users"),
]
