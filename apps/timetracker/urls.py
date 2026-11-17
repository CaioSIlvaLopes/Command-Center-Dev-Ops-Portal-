from django.urls import path
from . import views

app_name = "timetracker"

urlpatterns = [
    path("start/<int:task_id>/", views.start_timer_view, name="start"),
    path("stop/", views.stop_timer_view, name="stop"),
    path("manual/<int:task_id>/", views.add_manual_time_view, name="add_manual"),
    path("log/<int:log_id>/delete/", views.delete_timelog_view, name="delete_log"),
    path("active/", views.active_timer_partial_view, name="active_partial"),
]
