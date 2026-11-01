"""Integration tests for numbering config and user preferences API endpoints.

Covers:
- GET/PUT /settings/numbering (COMPANY level, owner-only)
- GET/PUT /settings/me (USER level with three-layer fallback)
- Three-layer fallback via real DB and via API (USER → COMPANY → GLOBAL)
- Owner-only protection
- Missing company guard
- Invalid input validation
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import jai.services.mfa as mfa_svc
import jai.services.settings as settings_svc
from jai.models._enums import SettingLevel
from jai.models.setting import Setting
from jai.models.user import User
from jai.schemas.setting import (
    SETTING_KEY_USER_PREFERENCES,
    UserPreferences,
)

# Every test in this module exercises DB-backed endpoints via ``db_client`` /
# real sessions, so the whole module is integration-only (needs PostgreSQL).
pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers – full authentication flow
# ---------------------------------------------------------------------------


async def _register(
    client: AsyncClient,
    email: str = "user@example.com",
    password: str = "testpassword1",
) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 201


async def _login(
    client: AsyncClient,
    email: str = "user@example.com",
    password: str = "testpassword1",
) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200


async def _full_auth(client: AsyncClient) -> str:
    """Register → login → MFA setup → MFA verify → return TOTP secret."""
    await _register(client)
    await _login(client)
    resp = await client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 200
    secret: str = resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    resp = await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    assert resp.status_code == 204
    return secret


async def _create_company(client: AsyncClient) -> None:
    """Create the singleton company (required for numbering config)."""
    resp = await client.put(
        "/api/v1/company",
        json={"name": "Test Co", "base_currency": "EUR"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_caches() -> Generator[None, None, None]:
    """Reset in-process caches."""
    from jai.config import get_settings

    mfa_svc._clear_pending_secrets()
    settings_svc._cache.clear()
    get_settings.cache_clear()
    yield
    mfa_svc._clear_pending_secrets()
    settings_svc._cache.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Numbering config API
# ---------------------------------------------------------------------------


class TestNumberingConfigAPI:
    """GET/PUT /settings/numbering — COMPANY level, owner-only."""

    async def test_get_defaults_before_create(self, db_client: AsyncClient) -> None:
        """GET /numbering returns defaults when no company exists."""
        await _full_auth(db_client)

        resp = await db_client.get("/api/v1/settings/numbering")
        assert resp.status_code == 200
        data = resp.json()
        assert data["template"] == "{{SERIES:INV}}-{{SEQUENCE:6}}"
        assert data["sequence_start"] == 1

    async def test_put_requires_company(self, db_client: AsyncClient) -> None:
        """PUT /numbering returns 400 when no company exists."""
        await _full_auth(db_client)

        resp = await db_client.put(
            "/api/v1/settings/numbering",
            json={"template": "INV-001", "sequence_start": 100},
        )
        assert resp.status_code == 400
        assert "company" in resp.json()["detail"].lower()

    async def test_round_trip(self, db_client: AsyncClient) -> None:
        """PUT then GET returns the stored config."""
        await _full_auth(db_client)
        await _create_company(db_client)

        config = {"template": "QUO-{{SEQUENCE:4}}", "sequence_start": 42}
        resp = await db_client.put("/api/v1/settings/numbering", json=config)
        assert resp.status_code == 200
        assert resp.json()["template"] == "QUO-{{SEQUENCE:4}}"
        assert resp.json()["sequence_start"] == 42

        # GET confirms persistence.
        resp = await db_client.get("/api/v1/settings/numbering")
        assert resp.status_code == 200
        assert resp.json()["template"] == "QUO-{{SEQUENCE:4}}"
        assert resp.json()["sequence_start"] == 42

    async def test_update_overwrites(self, db_client: AsyncClient) -> None:
        """Second PUT overwrites the first config."""
        await _full_auth(db_client)
        await _create_company(db_client)

        resp = await db_client.put(
            "/api/v1/settings/numbering",
            json={"template": "A-{{SEQUENCE:3}}", "sequence_start": 1},
        )
        assert resp.status_code == 200

        resp = await db_client.put(
            "/api/v1/settings/numbering",
            json={"template": "B-{{SEQUENCE:5}}", "sequence_start": 99},
        )
        assert resp.status_code == 200

        resp = await db_client.get("/api/v1/settings/numbering")
        assert resp.json()["template"] == "B-{{SEQUENCE:5}}"
        assert resp.json()["sequence_start"] == 99

    async def test_invalid_sequence_start(self, db_client: AsyncClient) -> None:
        """sequence_start < 1 is rejected by Pydantic."""
        await _full_auth(db_client)
        await _create_company(db_client)

        resp = await db_client.put(
            "/api/v1/settings/numbering",
            json={"template": "INV-001", "sequence_start": 0},
        )
        assert resp.status_code == 422

    async def test_unauthenticated(self, db_client: AsyncClient) -> None:
        """Unauthenticated requests are rejected."""
        resp = await db_client.get("/api/v1/settings/numbering")
        assert resp.status_code in (401, 403)

        resp = await db_client.put(
            "/api/v1/settings/numbering",
            json={"template": "INV-001", "sequence_start": 1},
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# User preferences API
# ---------------------------------------------------------------------------


class TestUserPreferencesAPI:
    """GET/PUT /settings/me — USER level with three-layer fallback."""

    async def test_get_defaults(self, db_client: AsyncClient) -> None:
        """GET /settings/me returns defaults when no preference set."""
        await _full_auth(db_client)

        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["theme"] == "system"
        assert body["locale"] == "en"
        assert body["show_battery_assets"] is False

    async def test_battery_navigation_round_trip(self, db_client: AsyncClient) -> None:
        """Navigation visibility persists without changing other preferences."""
        await _full_auth(db_client)
        for enabled in (True, False):
            preferences = {"theme": "dark", "locale": "zh", "show_battery_assets": enabled}
            response = await db_client.put("/api/v1/settings/me", json=preferences)
            assert response.status_code == 200
            assert response.json() == preferences
            settings_svc._cache.clear()
            response = await db_client.get("/api/v1/settings/me")
            assert response.status_code == 200
            assert response.json() == preferences

    async def test_old_preferences_hide_battery_navigation(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Existing JSON values without the new key keep theme and locale."""
        await _full_auth(db_client)
        user = (await db_client.get("/api/v1/users/me")).json()
        async with db_session_maker() as session:
            session.add(
                Setting(
                    key=SETTING_KEY_USER_PREFERENCES,
                    value={"theme": "dark", "locale": "zh"},
                    level=SettingLevel.USER,
                    scope_id=uuid.UUID(user["id"]),
                )
            )
            await session.commit()
        settings_svc._cache.clear()
        response = await db_client.get("/api/v1/settings/me")
        assert response.status_code == 200
        assert response.json() == {
            "theme": "dark", "locale": "zh", "show_battery_assets": False
        }

    async def test_put_and_get_round_trip(self, db_client: AsyncClient) -> None:
        """PUT then GET returns the stored preference."""
        await _full_auth(db_client)

        resp = await db_client.put("/api/v1/settings/me", json={"theme": "dark"})
        assert resp.status_code == 200
        assert resp.json()["theme"] == "dark"

        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code == 200
        assert resp.json()["theme"] == "dark"

    async def test_update_overwrites(self, db_client: AsyncClient) -> None:
        """Second PUT overwrites the first preference."""
        await _full_auth(db_client)

        resp = await db_client.put("/api/v1/settings/me", json={"theme": "dark"})
        assert resp.status_code == 200

        resp = await db_client.put("/api/v1/settings/me", json={"theme": "light"})
        assert resp.status_code == 200

        resp = await db_client.get("/api/v1/settings/me")
        assert resp.json()["theme"] == "light"

    async def test_all_valid_themes(self, db_client: AsyncClient) -> None:
        """All three valid theme values are accepted."""
        await _full_auth(db_client)

        for theme in ("system", "light", "dark"):
            resp = await db_client.put("/api/v1/settings/me", json={"theme": theme})
            assert resp.status_code == 200
            assert resp.json()["theme"] == theme

    async def test_invalid_theme_rejected(self, db_client: AsyncClient) -> None:
        """Invalid theme value is rejected by Pydantic."""
        await _full_auth(db_client)

        resp = await db_client.put("/api/v1/settings/me", json={"theme": "blue"})
        assert resp.status_code == 422

    async def test_locale_round_trip(self, db_client: AsyncClient) -> None:
        """Theme + locale persist together (PUT replaces the whole object)."""
        await _full_auth(db_client)

        resp = await db_client.put(
            "/api/v1/settings/me", json={"theme": "dark", "locale": "zh"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["theme"] == "dark"
        assert body["locale"] == "zh"

        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["theme"] == "dark"
        assert body["locale"] == "zh"

    async def test_invalid_locale_rejected(self, db_client: AsyncClient) -> None:
        """Invalid locale value is rejected by Pydantic."""
        await _full_auth(db_client)

        resp = await db_client.put("/api/v1/settings/me", json={"locale": "fr"})
        assert resp.status_code == 422

    async def test_unauthenticated(self, db_client: AsyncClient) -> None:
        """Unauthenticated requests are rejected."""
        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code in (401, 403)

        resp = await db_client.put("/api/v1/settings/me", json={"theme": "dark"})
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Three-layer fallback with real DB
# ---------------------------------------------------------------------------


class TestThreeLayerFallbackIntegration:
    """Verify USER → COMPANY → GLOBAL fallback via API and service layer."""

    async def test_api_falls_back_to_company(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /settings/me returns COMPANY value when no USER setting exists."""
        await _full_auth(db_client)
        await _create_company(db_client)

        # Get user/company IDs via API.
        user_resp = await db_client.get("/api/v1/users/me")
        user_data = user_resp.json()
        company_id = uuid.UUID(user_data["company_id"])

        # Set COMPANY-level preference directly via DB session.
        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                UserPreferences(theme="light"),
                level=SettingLevel.COMPANY,
                scope_id=company_id,
            )
            await session.commit()

        settings_svc._cache.clear()

        # API should return COMPANY-level value (no USER setting exists).
        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code == 200
        assert resp.json()["theme"] == "light"

        # Now set USER-level preference via API.
        resp = await db_client.put("/api/v1/settings/me", json={"theme": "dark"})
        assert resp.status_code == 200

        # USER-level should now override COMPANY.
        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code == 200
        assert resp.json()["theme"] == "dark"

    async def test_api_falls_back_to_global(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /settings/me returns GLOBAL value when no USER/COMPANY setting."""
        await _full_auth(db_client)
        await _create_company(db_client)

        user_resp = await db_client.get("/api/v1/users/me")
        user_data = user_resp.json()
        user_id = uuid.UUID(user_data["id"])
        company_id = uuid.UUID(user_data["company_id"])

        # Set GLOBAL-level preference.
        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                UserPreferences(theme="light"),
                level=SettingLevel.GLOBAL,
            )
            await session.commit()

        settings_svc._cache.clear()

        # API should return GLOBAL value (no USER/COMPANY set).
        resp = await db_client.get("/api/v1/settings/me")
        assert resp.status_code == 200
        assert resp.json()["theme"] == "light"

        # Set COMPANY-level: should override GLOBAL.
        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                UserPreferences(theme="dark"),
                level=SettingLevel.COMPANY,
                scope_id=company_id,
            )
            await session.commit()

        settings_svc._cache.clear()

        resp = await db_client.get("/api/v1/settings/me")
        assert resp.json()["theme"] == "dark"

        # Set USER-level: should override both.
        async with db_session_maker() as session:
            user = (
                await session.execute(select(User).where(User.id == user_id))  # type: ignore[arg-type]
            ).scalar_one()
            await settings_svc.set_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                UserPreferences(theme="system"),
                level=SettingLevel.USER,
                scope_id=user.id,
            )
            await session.commit()

        settings_svc._cache.clear()

        resp = await db_client.get("/api/v1/settings/me")
        assert resp.json()["theme"] == "system"

    async def test_get_effective_setting_service_layer(
        self,
        db_client: AsyncClient,
        db_session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_effective_setting resolves USER → COMPANY → GLOBAL correctly."""
        await _full_auth(db_client)
        await _create_company(db_client)

        user_resp = await db_client.get("/api/v1/users/me")
        user_data = user_resp.json()
        user_id = uuid.UUID(user_data["id"])

        # Set GLOBAL-level preference.
        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                UserPreferences(theme="light"),
                level=SettingLevel.GLOBAL,
            )
            await session.commit()

        settings_svc._cache.clear()

        # get_effective_setting should return GLOBAL value since no USER/COMPANY set.
        async with db_session_maker() as session:
            user = (
                await session.execute(select(User).where(User.id == user_id))  # type: ignore[arg-type]
            ).scalar_one()
            result = await settings_svc.get_effective_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )
            assert result is not None
            assert result.theme == "light"

        # Now set USER-level override.
        async with db_session_maker() as session:
            user = (
                await session.execute(select(User).where(User.id == user_id))  # type: ignore[arg-type]
            ).scalar_one()
            await settings_svc.set_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                UserPreferences(theme="dark"),
                level=SettingLevel.USER,
                scope_id=user.id,
            )
            await session.commit()

        settings_svc._cache.clear()

        # Now effective should return USER-level "dark".
        async with db_session_maker() as session:
            user = (
                await session.execute(select(User).where(User.id == user_id))  # type: ignore[arg-type]
            ).scalar_one()
            result = await settings_svc.get_effective_setting(
                session,
                SETTING_KEY_USER_PREFERENCES,
                user=user,
                value_type=UserPreferences,
            )
            assert result is not None
            assert result.theme == "dark"
