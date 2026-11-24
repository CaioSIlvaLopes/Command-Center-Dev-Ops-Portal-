from django.conf import settings
from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils.text import slugify


class Workspace(models.Model):
    """Frentes de trabalho do Command Center (ex: WSA Creative, SaaS & Dev, E-commerce)."""

    name = models.CharField("Nome da Frente", max_length=100)
    slug = models.SlugField("Slug (#tag)", max_length=50, unique=True)
    accent_color = models.CharField(
        "Cor de Destaque (Hex)",
        max_length=20,
        default="#3b82f6",
        help_text="Permitido pelo Design System: #3b82f6 (Azul) ou #f97316 (Laranja).",
    )
    icon_name = models.CharField("Ícone", max_length=50, default="briefcase")
    shortcut_key = models.CharField("Atalho de Teclado", max_length=20, blank=True, default="Alt+1")

    class Meta:
        verbose_name = "Frente de Trabalho (Workspace)"
        verbose_name_plural = "Frentes de Trabalho (Workspaces)"
        ordering = ["shortcut_key", "name"]

    def __str__(self) -> str:
        return f"{self.name} (#{self.slug})"

    def save(self, *args, **kwargs):
        if self.slug:
            self.slug = slugify(self.slug.lstrip("#")).lower()
        elif self.name:
            self.slug = slugify(self.name).lower()
        if str(self.accent_color).lower() == "#f97316":
            self.accent_color = "#f97316"
        else:
            self.accent_color = "#3b82f6"
        super().save(*args, **kwargs)

    @property
    def is_orange_accent(self) -> bool:
        return self.accent_color.lower() == "#f97316"


class Project(models.Model):
    """Projetos ou Lojas vinculados a uma Frente de Trabalho."""

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="projects",
        verbose_name="Frente de Trabalho",
    )
    name = models.CharField("Nome do Projeto", max_length=150)
    client_or_store = models.CharField("Cliente ou Loja", max_length=150, blank=True)
    github_repo_name = models.CharField(
        "Nome do Repositório GitHub",
        max_length=150,
        blank=True,
        default="",
        help_text="Ex: Sistema-de-Chamado, MSCREW-Site, date-and-mate",
    )
    repo_url = models.URLField("URL do Repositório", blank=True)
    admin_or_panel_url = models.URLField(
        "URL do Painel / Seller Centre",
        blank=True,
        null=True,
    )
    is_active = models.BooleanField("Ativo", default=True)

    class Meta:
        verbose_name = "Projeto"
        verbose_name_plural = "Projetos"
        ordering = ["workspace", "name"]

    def __str__(self) -> str:
        if self.client_or_store:
            return f"{self.name} ({self.client_or_store})"
        return self.name


class TeamMember(models.Model):
    """Membros da equipe / parceiros para atribuição rápida (@handle) nas tarefas."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="team_profile",
        verbose_name="Usuário do Sistema",
    )
    name = models.CharField("Nome", max_length=80)
    handle = models.SlugField("Handle (sem @)", max_length=50, unique=True)
    role = models.CharField("Função / Cargo", max_length=100, blank=True, default="")
    avatar_initials = models.CharField("Iniciais do Avatar", max_length=3, blank=True, default="")
    accent_color = models.CharField(
        "Cor do Avatar",
        max_length=7,
        default="#3b82f6",
        help_text="Permitido pelo Design System: #3b82f6 (Azul) ou #f97316 (Laranja).",
    )
    can_access_vault = models.BooleanField(
        "Acesso ao Cofre",
        default=False,
        help_text="Permite ver, copiar, criar e editar credenciais no Cofre.",
    )
    whatsapp_number = models.CharField(
        "WhatsApp (DDI+DDD+Número)",
        max_length=30,
        blank=True,
        default="",
        help_text="WhatsApp com DDI+DDD, somente números. Ex: 5521999999999",
    )
    is_active = models.BooleanField("Ativo", default=True)
    created_at = models.DateTimeField("Criado em", auto_now_add=True)

    class Meta:
        verbose_name = "Responsável (Team Member)"
        verbose_name_plural = "Responsáveis (Team Members)"
        ordering = ["name", "handle"]

    def __str__(self) -> str:
        return f"{self.name} (@{self.handle})"

    def save(self, *args, **kwargs):
        raw_handle = (self.handle or self.name or "").lstrip("@").strip().lower()
        self.handle = slugify(raw_handle) or "membro"

        if str(self.accent_color).lower() == "#f97316":
            self.accent_color = "#f97316"
        else:
            self.accent_color = "#3b82f6"

        if self.whatsapp_number:
            raw_wa = str(self.whatsapp_number).split("@")[0]
            self.whatsapp_number = "".join(ch for ch in raw_wa if ch.isdigit())[:30]
        else:
            self.whatsapp_number = ""

        if not self.avatar_initials:
            words = [w for w in (self.name or "").strip().split() if w]
            if len(words) >= 2:
                self.avatar_initials = (words[0][0] + words[1][0]).upper()
            elif words:
                self.avatar_initials = words[0][:2].upper()
            else:
                self.avatar_initials = self.handle[:2].upper()
        else:
            self.avatar_initials = self.avatar_initials.strip().upper()[:3]

        super().save(*args, **kwargs)

    @property
    def is_orange_accent(self) -> bool:
        return self.accent_color.lower() == "#f97316"

    @property
    def formatted_handle(self) -> str:
        return f"@{self.handle}"


@receiver(pre_delete, sender=TeamMember)
def clear_assigned_to_on_member_delete(sender, instance: TeamMember, **kwargs):
    """Ao excluir um TeamMember, limpa assigned_to e assignee nas tarefas vinculadas."""
    instance.tasks.all().update(assignee=None, assigned_to="")


class SystemIntegrationConfig(models.Model):
    """
    Configuração Singleton de Integrações & Disjuntor de Cota Diária (Phase 7 & 7.1):
    Controla o Kill-Switch de Áudio IA (`ai_audio_enabled`), a chave `gemini_api_key`,
    o limite diário estrito (`max_daily_audio_requests`, padrão 20 áudios/dia) e
    a conexão com a Evolution API v2 (`getBase64FromMediaMessage` para áudios citados).
    """

    gemini_api_key = models.CharField(
        "Chave API Gemini (Free Tier)",
        max_length=255,
        blank=True,
        default="",
    )
    max_daily_audio_requests = models.PositiveSmallIntegerField(
        "Limite Diário de Áudios IA",
        default=20,
    )
    ai_audio_enabled = models.BooleanField(
        "Processamento de Áudio IA Ativo (Kill-Switch)",
        default=True,
    )
    evolution_api_url = models.CharField(
        "Evolution API URL",
        max_length=255,
        blank=True,
        default="http://127.0.0.1:8080",
    )
    evolution_api_key = models.CharField(
        "Evolution API Key",
        max_length=255,
        blank=True,
        default="",
    )
    evolution_instance_name = models.CharField(
        "Evolution Instance Name",
        max_length=120,
        blank=True,
        default="",
    )
    default_whatsapp_number = models.CharField(
        "WhatsApp Padrão (Fallback)",
        max_length=30,
        blank=True,
        default="",
        help_text="Número padrão de fallback para lembretes e confirmações.",
    )
    whatsapp_notifications_enabled = models.BooleanField(
        "Notificações e Lembretes WhatsApp Ativos",
        default=True,
        help_text="Kill-Switch global para envio de confirmações e lembretes via Evolution API.",
    )
    updated_at = models.DateTimeField("Atualizado em", auto_now=True)

    class Meta:
        verbose_name = "Configuração de Integração & Cota IA"
        verbose_name_plural = "Configurações de Integração & Cota IA"

    def __str__(self) -> str:
        status_str = "Ativo" if self.ai_audio_enabled else "Pausado (Kill-Switch)"
        return f"Motor de Áudio IA ({status_str} - Máx {self.max_daily_audio_requests}/dia)"

    @classmethod
    def get_solo(cls) -> "SystemIntegrationConfig":
        import os

        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "gemini_api_key": os.getenv("GEMINI_API_KEY", "").strip(),
                "max_daily_audio_requests": 20,
                "ai_audio_enabled": True,
                "evolution_api_url": (
                    os.getenv("EVOLUTION_API_URL", "http://127.0.0.1:8080").strip()
                    or "http://127.0.0.1:8080"
                ),
                "evolution_api_key": os.getenv("EVOLUTION_API_KEY", "").strip(),
                "evolution_instance_name": os.getenv("EVOLUTION_INSTANCE_NAME", "").strip(),
                "default_whatsapp_number": os.getenv("DEFAULT_WHATSAPP_NUMBER", "").strip(),
                "whatsapp_notifications_enabled": True,
            },
        )
        return obj


def _get_vault_fernet_key() -> bytes:
    """
    Deriva uma chave Fernet de 32 bytes (URL-safe base64) a partir de `settings.VAULT_ENCRYPTION_KEY`
    ou `settings.SECRET_KEY` via SHA-256.
    """
    import base64
    import hashlib
    from django.conf import settings

    raw_key = (
        getattr(settings, "VAULT_ENCRYPTION_KEY", "")
        or getattr(settings, "SECRET_KEY", "command-center-vault-default-key")
    )
    digest = hashlib.sha256(str(raw_key).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_vault_secret(raw_secret: str) -> str:
    """
    Criptografa uma senha/token do Cofre usando Fernet (AES-128-CBC + HMAC-SHA256)
    com fallback autenticado via stdlib (SHA256 CTR + HMAC) caso `cryptography` não esteja instalado.
    """
    import base64
    import hashlib
    import hmac
    import secrets

    if not raw_secret:
        return ""

    key_b64 = _get_vault_fernet_key()
    try:
        from cryptography.fernet import Fernet

        f = Fernet(key_b64)
        token = f.encrypt(raw_secret.encode("utf-8")).decode("ascii")
        return f"fernet:{token}"
    except Exception:
        key_bytes = base64.urlsafe_b64decode(key_b64)
        nonce = secrets.token_bytes(16)
        plain_bytes = raw_secret.encode("utf-8")
        stream = bytearray()
        counter = 0
        while len(stream) < len(plain_bytes):
            block = hashlib.sha256(key_bytes + nonce + counter.to_bytes(4, "big")).digest()
            stream.extend(block)
            counter += 1
        cipher_bytes = bytes(b ^ s for b, s in zip(plain_bytes, stream[: len(plain_bytes)]))
        mac = hmac.new(key_bytes, nonce + cipher_bytes, hashlib.sha256).digest()
        payload = base64.urlsafe_b64encode(nonce + mac + cipher_bytes).decode("ascii")
        return f"ccv1:{payload}"


def decrypt_vault_secret(encrypted_secret: str) -> str:
    """Descriptografa uma senha/token armazenada em `CredentialEntry.encrypted_secret`."""
    import base64
    import hashlib
    import hmac

    if not encrypted_secret:
        return ""

    key_b64 = _get_vault_fernet_key()
    if encrypted_secret.startswith("fernet:"):
        token = encrypted_secret[len("fernet:") :]
        try:
            from cryptography.fernet import Fernet

            f = Fernet(key_b64)
            return f.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception:
            return ""

    if encrypted_secret.startswith("ccv1:"):
        payload_b64 = encrypted_secret[len("ccv1:") :]
        try:
            key_bytes = base64.urlsafe_b64decode(key_b64)
            raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
            nonce, mac, cipher_bytes = raw[:16], raw[16:48], raw[48:]
            expected_mac = hmac.new(key_bytes, nonce + cipher_bytes, hashlib.sha256).digest()
            if not hmac.compare_digest(mac, expected_mac):
                return ""
            stream = bytearray()
            counter = 0
            while len(stream) < len(cipher_bytes):
                block = hashlib.sha256(key_bytes + nonce + counter.to_bytes(4, "big")).digest()
                stream.extend(block)
                counter += 1
            plain_bytes = bytes(c ^ s for c, s in zip(cipher_bytes, stream[: len(cipher_bytes)]))
            return plain_bytes.decode("utf-8")
        except Exception:
            return ""

    # Caso seja token Fernet puro sem prefixo
    try:
        from cryptography.fernet import Fernet

        f = Fernet(key_b64)
        return f.decrypt(encrypted_secret.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


class CredentialEntry(models.Model):
    """
    Cofre de Credenciais & Acessos (Vault):
    Armazena e-mails, contas de lojas/marketplaces, servidores VPS, domínios e chaves de API
    vinculados a uma Frente (`Workspace`) e opcionalmente a um `Project`, com senha criptografada.
    """

    class Category(models.TextChoices):
        EMAIL = "email", "E-mail & Conta"
        ECOMMERCE = "ecommerce", "Lojas & Marketplaces"
        INFRA = "infra", "Servidor, VPS & Cloud"
        DOMAIN = "domain", "Domínio, DNS & Hosting"
        API = "api", "Chaves de API & Tokens"
        OTHER = "other", "Outros Acessos"

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="credentials",
        verbose_name="Frente de Trabalho",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credentials",
        verbose_name="Projeto / Loja",
    )
    title = models.CharField("Título do Acesso", max_length=150)
    category = models.CharField(
        "Categoria",
        max_length=30,
        choices=Category.choices,
        default=Category.EMAIL,
        db_index=True,
    )
    username_or_email = models.CharField("E-mail / Usuário / Login", max_length=200)
    encrypted_secret = models.TextField("Senha / Token Criptografado", blank=True, default="")
    service_url = models.URLField("URL de Acesso / Painel", blank=True, default="")
    recovery_info = models.CharField(
        "Recuperação / Dica 2FA",
        max_length=255,
        blank=True,
        default="",
    )
    notes = models.TextField("Notas Adicionais", blank=True, default="")
    created_at = models.DateTimeField("Criado em", auto_now_add=True)
    updated_at = models.DateTimeField("Atualizado em", auto_now=True)

    class Meta:
        verbose_name = "Credencial do Cofre (Vault)"
        verbose_name_plural = "Credenciais do Cofre (Vault)"
        ordering = ["workspace", "category", "title"]

    def __str__(self) -> str:
        return f"{self.title} ({self.username_or_email}) [#{self.workspace.slug}]"

    def set_secret(self, raw_secret: str) -> None:
        """Criptografa e define a senha/token em `encrypted_secret`."""
        self.encrypted_secret = encrypt_vault_secret(raw_secret or "")

    def get_secret(self) -> str:
        """Descriptografa e retorna a senha/token original."""
        return decrypt_vault_secret(self.encrypted_secret or "")

    @property
    def is_urgent_category(self) -> bool:
        return self.category in (self.Category.ECOMMERCE, self.Category.INFRA, self.Category.API)


