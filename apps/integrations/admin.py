from django.contrib import admin
from .models import WebhookEvent


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "source", "event_type", "processed", "created_task", "created_at")
    list_filter = ("source", "processed", "event_type")
    search_fields = ("event_type", "error_message", "created_task__title")
    readonly_fields = ("created_at",)
