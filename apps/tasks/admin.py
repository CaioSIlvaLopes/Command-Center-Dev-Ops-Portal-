from django.contrib import admin
from .models import Task, SubTask


class SubTaskInline(admin.TabularInline):
    model = SubTask
    extra = 1


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "workspace",
        "project",
        "status",
        "shift",
        "impact",
        "urgency",
        "priority_score",
        "is_top_three",
        "assigned_to",
        "due_date",
    )
    list_filter = ("workspace", "status", "shift", "is_top_three")
    search_fields = ("title", "notes", "assigned_to", "external_ref")
    readonly_fields = ("priority_score", "created_at", "completed_at")
    inlines = [SubTaskInline]


@admin.register(SubTask)
class SubTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "task", "is_done", "created_at")
    list_filter = ("is_done", "task__workspace")
    search_fields = ("title", "task__title")
