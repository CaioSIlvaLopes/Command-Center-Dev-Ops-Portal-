from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect


class CommandCenterAuthMiddleware:
    """
    Single-Tenant Guard para Produção (Fase 9):
    Quando `settings.REQUIRE_AUTH` é True ou `settings.DEBUG` é False, exige sessão autenticada
    (`request.user.is_authenticated`) para acessar todas as rotas do Dashboard, Tasks, TimeTracker e Vault.
    Mantém `/api/webhooks/*`, `/login/`, `/logout/` e `/static/` isentos (os webhooks utilizam
    autenticação própria via `X-Webhook-Token` ou `X-Hub-Signature-256`).
    """

    EXEMPT_PREFIXES = (
        "/login/",
        "/logout/",
        "/api/webhooks/",
        "/static/",
        "/admin/login/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        require_auth = bool(getattr(settings, "REQUIRE_AUTH", False))
        if require_auth:
            path = request.path or "/"
            if not any(path.startswith(prefix) for prefix in self.EXEMPT_PREFIXES):
                user = getattr(request, "user", None)
                if not user or not user.is_authenticated:
                    login_url = getattr(settings, "LOGIN_URL", "/login/")
                    next_url = request.get_full_path()
                    if request.headers.get("HX-Request"):
                        return HttpResponse(
                            status=401,
                            headers={"HX-Redirect": f"{login_url}?next={next_url}"},
                        )
                    return redirect(f"{login_url}?next={next_url}")

        return self.get_response(request)
