from django.contrib import admin
from .models import TimeLog


@admin.register(TimeLog)
class TimeLogAdmin(admin.ModelAdmin):
    list_display = ("task", "start_time", "end_time", "duration_minutes", "is_running")
    list_filter = ("is_running", "task__workspace")
    search_fields = ("task__title",)

