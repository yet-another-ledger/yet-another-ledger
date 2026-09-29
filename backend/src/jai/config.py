"""Application settings loaded from environment variables via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


def _find_env_file() -> str:
    """Search upward from *this file* for a repo-root ``.env``.

    The repo root is identified by ``docker-compose.yml`` being present.
    Falls back to ``".env"`` (CWD-relative) so that pydantic-settings
    gracefully degrades when the marker file is absent (e.g. in CI where
    ``DATABASE_URL`` is set as a plain environment variable).
    """
    start = Path(__file__).resolve().parent
    for p in [start, *start.parents]:
        if (p / "docker-compose.yml").is_file():
            return str(p / ".env")
    return ".env"


class Settings(BaseSettings):
    """Central configuration.  All fields have sensible defaults.

    **Priority (high → low)**:
      1. OS environment variables  (``POSTGRES_HOST=...``)
      2. ``.env`` file in the repo root  (found via :func:`_find_env_file`)
      3. Code defaults below

    The database connection is configured via individual ``POSTGRES_*`` fields.
    ``database_url`` is auto-assembled from those fields.  If you need full
    control, set ``DATABASE_URL`` directly — it takes precedence over the
    individual parts.

    Typical overrides:
      - **Local dev**: ``POSTGRES_PORT=5433`` in ``.env``
        (container mapped to host port 5433, rest uses defaults).
      - **Production Docker**: ``POSTGRES_HOST=postgres`` in Compose env
        (internal network, rest uses defaults).
    """

    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    _database_url_was_explicit: bool = PrivateAttr(default=False)

    # -- PostgreSQL connection parts -----------------------------------------
    # Runtime credentials are deliberately separate from the PostgreSQL
    # bootstrap/migration owner.  New deployments pass these as separate
    # fields so SQLAlchemy can encode credentials safely at the process edge.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    # The old single-role pair remains accepted for existing deployments.
    # New installations use the explicit least-privilege role pairs below.
    postgres_user: str = "jai"
    postgres_password: str = "jai"
    postgres_db: str = "jai"
    postgres_app_user: str = "jai_app"
    postgres_app_password: str = "jai_app"
    postgres_migration_user: str = "jai_migrator"
    postgres_migration_password: str = "jai_migrator"
    postgres_admin_user: str = "jai_admin"
    postgres_admin_password: str = "jai_admin"

    # -- Full URL overrides (external/legacy escape hatches) ----------------
    # If set, these take precedence over the corresponding individual parts.
    database_url: str | None = None
    database_migration_url: str | None = None

    # -- Frontend static files (deployment mode) ----------------------------
    # When set, FastAPI serves the SPA from this directory.
    static_dir: str | None = None

    # -- Application metadata -----------------------------------------------
    app_version: str = "0.7.0"

    # -- Base URL (for generating absolute links in emails) -----------------
    # Must be set in production (e.g. ``https://invoice.example.com``).
    # Defaults to ``http://localhost:8000`` for local dev.
    base_url: str = "http://localhost:8000"

    # -- Authentication / session --------------------------------------------
    auth_secret: str = (
        "change-me-in-production-use-at-least-32-chars"
    )  # JWT signing key (≥32 bytes for HMAC-SHA256)
    cookie_secure: bool = False  # True in production (HTTPS only)
    session_ttl_days: int = 7  # Full session cookie lifetime
    pre_auth_ttl_minutes: int = 5  # Pre-auth (post-password, pre-MFA) window
    reset_password_ttl_minutes: int = 60  # Password-reset token lifetime

    # -- APScheduler (M6 step 3) -------------------------------------------
    # Set SCHEDULER_ENABLED=false in test environments or when running the
    # scheduler externally.
    scheduler_enabled: bool = True
    # Hour (0–23 UTC) at which the daily quote-expiry job fires.
    scheduler_expire_quotes_hour: int = 1
    # Hour (0–23 UTC) at which the daily recurring-expense generation job fires.
    # Deliberately offset from quote-expiry (1:00) to avoid contention.
    scheduler_recurring_expenses_hour: int = 2

    # -- Storage (M8 step 2) ------------------------------------------------
    # Root directory for local file storage (receipts, future PDF attachments).
    # Dev default: ./var/storage (relative to CWD where uvicorn is launched).
    # Production/container default: /data/storage (separate volume).
    storage_root: str = "./var/storage"
    # Maximum allowed size for receipt uploads (bytes). Default: 10 MB.
    max_receipt_bytes: int = 10 * 1024 * 1024
    # Formal historical PDFs are retained in PostgreSQL, independently of
    # receipt storage.  Do not couple this policy to max_receipt_bytes.
    max_artifact_bytes: int = 10 * 1024 * 1024

    # -- SMTP env fallback (step 4) -----------------------------------------
    # These are used as fallback when no SMTP settings exist in the DB.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    # -- AI / receipt extraction (M8 step 4) --------------------------------
    # These serve as env fallback when no AI settings exist in the DB.
    # ``ai_enabled=false`` by default: AI features must be explicitly enabled.
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = ""
    ai_enabled: bool = False
    # Maximum number of PDF pages to rasterise when extracting from a PDF receipt.
    ai_pdf_max_pages: int = 3
    # Scale factor for pypdfium2 page rendering (higher = more detail, more tokens).
    # 2.0 gives approximately 150 DPI for A4 pages, sufficient for OCR-quality reading.
    ai_pdf_render_scale: float = 2.0

    @model_validator(mode="after")
    def _assemble_database_url(self) -> Settings:
        """Build ``database_url`` from parts if not explicitly provided.

        Uses ``URL.create()`` so that special characters in the password
        (``@``, ``:``, ``/``, ``#`` …) are safely percent-encoded.
        """
        # Compose passes DATABASE_URL through even when the optional variable
        # is unset.  Treat an empty string as absent and use POSTGRES_* then.
        # DATABASE_URL is the established explicit runtime override.  If an
        # old deployment only supplies POSTGRES_USER/PASSWORD, preserve that
        # connection shape; otherwise runtime deliberately defaults to the
        # non-owner application credentials.
        self._database_url_was_explicit = bool(self.database_url)
        legacy_parts_explicit = bool(
            {"postgres_user", "postgres_password"} & self.model_fields_set
        ) and not bool(
            {"postgres_app_user", "postgres_app_password"} & self.model_fields_set
        )
        if not self.database_url:
            runtime_user = self.postgres_user if legacy_parts_explicit else self.postgres_app_user
            runtime_password = (
                self.postgres_password if legacy_parts_explicit else self.postgres_app_password
            )
            url = URL.create(
                drivername="postgresql+asyncpg",
                username=runtime_user,
                password=runtime_password,
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_db,
            )
            self.database_url = url.render_as_string(hide_password=False)
        return self

    @property
    def migration_database_url(self) -> str:
        """Return the Alembic connection URL using the migration owner.

        ``DATABASE_MIGRATION_URL`` is the explicit, role-specific escape
        hatch.  A legacy command that supplied only ``DATABASE_URL`` keeps
        working, while new installations assemble an encoded URL from the
        migration role's independent connection parts.
        """
        if self.database_migration_url:
            return self.database_migration_url
        if self._database_url_was_explicit and self.database_url:
            return self.database_url
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_migration_user,
            password=self.postgres_migration_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @property
    def database_admin_url(self) -> str:
        """Admin-only URL used by isolated test/database provisioning tools."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_admin_user,
            password=self.postgres_admin_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` singleton."""
    return Settings()
