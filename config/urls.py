from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from apps.integrations.api import api

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(next_page="/login/"),
        name="logout",
    ),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("timetracker/", include("apps.timetracker.urls", namespace="timetracker")),
    path("", include("apps.tasks.urls", namespace="tasks")),
]


