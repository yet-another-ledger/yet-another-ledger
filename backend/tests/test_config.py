"""Tests for ``jai.config`` – Settings loading, caching, and URL assembly."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jai.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Settings unit tests independent from CI's role-provisioning env."""
    for variable in tuple(os.environ):
        if variable == "DATABASE_URL" or variable.startswith(("DATABASE_", "POSTGRES_")):
            monkeypatch.delenv(variable, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestDefaults:
    """Code-level defaults (checked via model_fields, not affected by .env)."""

    def test_postgres_parts(self) -> None:
        """Individual fields should have the expected code defaults."""
        f = Settings.model_fields
        assert f["postgres_host"].default == "localhost"
        assert f["postgres_port"].default == 5432
        assert f["postgres_user"].default == "jai"  # legacy compatibility
        assert f["postgres_password"].default == "jai"
        assert f["postgres_app_user"].default == "jai_app"
        assert f["postgres_app_password"].default == "jai_app"
        assert f["postgres_migration_user"].default == "jai_migrator"
        assert f["postgres_db"].default == "jai"

    def test_static_dir_default_none(self) -> None:
        assert Settings.model_fields["static_dir"].default is None

    def test_app_version_default(self) -> None:
        assert Settings.model_fields["app_version"].default == "0.7.0"

    def test_artifact_upload_limit_is_independent_from_receipt_limit(self) -> None:
        assert Settings.model_fields["max_artifact_bytes"].default == 10 * 1024 * 1024
        assert Settings(max_receipt_bytes=1).max_artifact_bytes == 10 * 1024 * 1024

    def test_database_url_default_is_none(self) -> None:
        """Code default is None (assembled by model_validator)."""
        assert Settings.model_fields["database_url"].default is None
        assert Settings.model_fields["database_migration_url"].default is None


class TestURLAssembly:
    """``database_url`` is auto-assembled from POSTGRES_* parts."""

    def test_parts_produce_correct_url(self) -> None:
        """Verify URL format assembled from default parts."""
        s = Settings(
            postgres_host="db.example.com",
            postgres_port=5432,
            postgres_app_user="myuser",
            postgres_app_password="mypass",
            postgres_db="mydb",
        )
        assert s.database_url == "postgresql+asyncpg://myuser:mypass@db.example.com:5432/mydb"

    def test_custom_port_in_url(self) -> None:
        s = Settings(postgres_port=5433)
        assert ":5433/" in (s.database_url or "")

    def test_special_chars_in_password_encoded(self) -> None:
        """Password with @:/# should be percent-encoded, not break the URL."""
        s = Settings(
            postgres_host="db.example.com",
            postgres_port=5432,
            postgres_user="myuser",
            postgres_password="p@ss:w0rd/#test",
            postgres_db="mydb",
        )
        url = s.database_url
        assert url is not None
        # The raw password must NOT appear verbatim.
        assert "p@ss:w0rd/#test" not in url
        # The encoded form should be present.
        assert "p%40ss%3Aw0rd%2F%23test" in url


class TestURLOverride:
    """``DATABASE_URL`` env var takes precedence over individual parts."""

    def test_explicit_url_wins(self) -> None:
        """If DATABASE_URL is set, individual POSTGRES_* are ignored for URL."""
        s = Settings(
            database_url="postgresql+asyncpg://custom:custom@remote:9999/customdb",
            postgres_host="should-be-ignored",
            postgres_port=1234,
        )
        assert s.database_url == "postgresql+asyncpg://custom:custom@remote:9999/customdb"

    def test_url_not_set_uses_parts(self) -> None:
        """If DATABASE_URL is None, it's assembled from parts."""
        s = Settings(
            database_url=None,
            postgres_host="mypg",
            postgres_port=5432,
        )
        assert s.database_url is not None
        assert "mypg" in s.database_url

    def test_new_role_parts_default_to_runtime_url_only(self) -> None:
        s = Settings(
            postgres_host="db.example.com",
            postgres_port=5432,
            postgres_app_user="runtime",
            postgres_app_password="runtime-password",
            postgres_migration_user="owner",
            postgres_migration_password="owner-password",
        )
        assert s.database_url == (
            "postgresql+asyncpg://runtime:runtime-password@db.example.com:5432/jai"
        )

    def test_app_only_environment_never_falls_back_to_legacy_jai_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented host-dev variables select the runtime app role."""
        for variable in (
            "DATABASE_URL",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_APP_USER",
            "POSTGRES_APP_PASSWORD",
            "POSTGRES_MIGRATION_USER",
            "POSTGRES_MIGRATION_PASSWORD",
        ):
            monkeypatch.delenv(variable, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                (
                    "POSTGRES_HOST=localhost",
                    "POSTGRES_PORT=5433",
                    "POSTGRES_DB=jai",
                    "POSTGRES_APP_USER=jai_app",
                    "POSTGRES_APP_PASSWORD=runtime-secret",
                    "POSTGRES_MIGRATION_USER=jai_migrator",
                    "POSTGRES_MIGRATION_PASSWORD=migration-secret",
                )
            ),
            encoding="utf-8",
        )
        settings = Settings(_env_file=env_file)
        assert settings.database_url == (
            "postgresql+asyncpg://jai_app:runtime-secret@localhost:5433/jai"
        )

    def test_legacy_postgres_user_without_app_parts_stays_compatible(self) -> None:
        s = Settings(postgres_user="legacy", postgres_password="legacy-password")
        assert s.database_url is not None
        assert "legacy:legacy-password" in s.database_url


class TestMigrationURLAssembly:
    """Alembic uses an independent, migration-owner connection URL."""

    def test_migration_parts_safely_encode_reserved_password_characters(self) -> None:
        settings = Settings(
            postgres_host="db.example.com",
            postgres_port=5432,
            postgres_migration_user="owner",
            postgres_migration_password="p@ss:w0rd/#test",
            postgres_db="mydb",
        )
        assert settings.migration_database_url == (
            "postgresql+asyncpg://owner:p%40ss%3Aw0rd%2F%23test@db.example.com:5432/mydb"
        )

    def test_explicit_migration_url_wins_over_runtime_and_parts(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://runtime:pw@runtime:5432/jai",
            database_migration_url="postgresql+asyncpg://owner:pw@owner:5432/jai",
            postgres_migration_user="ignored",
        )
        assert settings.migration_database_url == "postgresql+asyncpg://owner:pw@owner:5432/jai"

    def test_legacy_database_url_remains_migration_fallback(self) -> None:
        settings = Settings(database_url="postgresql+asyncpg://owner:pw@host:5432/jai")
        assert settings.migration_database_url == settings.database_url


class TestEnvFile:
    """Integration with explicit env files without relying on local repo state."""

    def test_env_file_overrides_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An env file can override the code default port (5432 -> 5433)."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("POSTGRES_PORT", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("POSTGRES_PORT=5433\n", encoding="utf-8")

        s = Settings(_env_file=env_file)

        assert "5433" in s.database_url

    def test_env_file_overrides_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An env file can override the code default host."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("POSTGRES_HOST", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("POSTGRES_HOST=postgres\n", encoding="utf-8")

        s = Settings(_env_file=env_file)

        assert "postgres" in (s.database_url or "")


class TestCaching:
    def test_get_settings_cached(self) -> None:
        """``get_settings()`` should return the same object on repeated calls."""
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2


class TestExtraFields:
    def test_extra_fields_ignored(self) -> None:
        """Extra env vars should not cause validation errors (extra='ignore')."""
        s = Settings(UNKNOWN_VAR="hello")  # type: ignore[call-arg]
        assert s.database_url is not None
