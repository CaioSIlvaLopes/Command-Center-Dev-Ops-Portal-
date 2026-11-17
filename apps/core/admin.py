from django.contrib import admin
from .models import Workspace, Project, TeamMember, SystemIntegrationConfig, CredentialEntry


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "accent_color", "shortcut_key", "icon_name")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "workspace",
        "client_or_store",
        "github_repo_name",
        "is_active",
        "repo_url",
        "admin_or_panel_url",
    )
    list_filter = ("workspace", "is_active")
    search_fields = ("name", "client_or_store", "github_repo_name")


@admin.register(TeamMember)
class TeamMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "handle", "role", "avatar_initials", "accent_color", "is_active")
    list_filter = ("is_active", "accent_color")
    search_fields = ("name", "handle", "role")


@admin.register(SystemIntegrationConfig)
class SystemIntegrationConfigAdmin(admin.ModelAdmin):
    list_display = ("id", "ai_audio_enabled", "max_daily_audio_requests", "updated_at")


@admin.register(CredentialEntry)
class CredentialEntryAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "username_or_email", "workspace", "project", "updated_at")
    list_filter = ("workspace", "category")
    search_fields = ("title", "username_or_email", "notes")


