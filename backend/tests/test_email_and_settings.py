"""Tests for Step 4: email/SMTP, SMTP settings API, forgot/reset password.

Covers:
- SMTP settings CRUD (GET/PUT /settings/smtp, owner-only, password desensitisation)
- SMTP test email endpoint (POST /settings/smtp/test)
- Password-reset token generation, validation, expiration
- ``POST /auth/forgot-password`` – always 202, anti-enumeration
- ``POST /auth/reset-password`` – token validation, password update
- Email construction (mock SMTP, assert recipient/subject/body)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import jai.services.mfa as mfa_svc
import jai.services.settings as settings_svc
from jai.auth.secret import get_auth_secret
from jai.models._enums import SettingLevel
from jai.schemas.setting import SETTING_KEY_SMTP, SmtpSettings

# ---------------------------------------------------------------------------
# Helpers – full authentication flow (reuse from test_auth)
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
    secret = resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    resp = await client.post("/api/v1/auth/mfa/verify", json={"code": code})
    assert resp.status_code == 204
    return secret


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    """Reset in-process caches and config so each test starts fresh.

    Clears:
    - MFA pending-secret store
    - Settings service in-process cache
    - ``get_settings()`` lru_cache (so env monkeypatches take effect)
    """
    from jai.config import get_settings

    mfa_svc._clear_pending_secrets()
    settings_svc._cache.clear()
    get_settings.cache_clear()
    yield
    mfa_svc._clear_pending_secrets()
    settings_svc._cache.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# SMTP settings API
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSmtpSettingsApi:
    """Tests for ``GET/PUT /api/v1/settings/smtp`` and ``POST /smtp/test``."""

    @pytest.mark.asyncio
    async def test_get_smtp_defaults_when_not_configured(self, db_client: AsyncClient) -> None:
        """GET /settings/smtp returns defaults when no config exists."""
        await _full_auth(db_client)

        resp = await db_client.get("/api/v1/settings/smtp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["host"] == ""
        assert data["password_set"] is False

    @pytest.mark.asyncio
    async def test_put_and_get_smtp_settings(self, db_client: AsyncClient) -> None:
        """PUT then GET returns the saved settings with password masked."""
        await _full_auth(db_client)

        resp = await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "port": 587,
                "username": "user",
                "password": "secret123",
                "from_email": "noreply@example.com",
                "from_name": "JAI",
                "use_tls": True,
                "use_ssl": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["host"] == "smtp.example.com"
        assert data["password_set"] is True
        assert "password" not in data or data.get("password") is None

        # GET should return the same.
        resp = await db_client.get("/api/v1/settings/smtp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["host"] == "smtp.example.com"
        assert data["password_set"] is True
        assert "password" not in data or data.get("password") is None

    @pytest.mark.asyncio
    async def test_put_smtp_preserves_password_when_null(self, db_client: AsyncClient) -> None:
        """PUT with password=None keeps the existing password."""
        await _full_auth(db_client)

        # Save initial config with password.
        await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "secret123",
                "from_email": "noreply@example.com",
            },
        )

        # Update host without providing password.
        resp = await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.newhost.com",
                "password": None,
                "from_email": "noreply@example.com",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["host"] == "smtp.newhost.com"
        assert resp.json()["password_set"] is True

    @pytest.mark.asyncio
    async def test_put_smtp_clears_password_when_empty(self, db_client: AsyncClient) -> None:
        """PUT with password="" clears the password."""
        await _full_auth(db_client)

        # Save initial config with password.
        await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "secret123",
                "from_email": "noreply@example.com",
            },
        )

        # Clear password.
        resp = await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "",
                "from_email": "noreply@example.com",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["password_set"] is False

    @pytest.mark.asyncio
    async def test_smtp_settings_require_auth(self, client: AsyncClient) -> None:
        """SMTP endpoints return 401 without authentication."""
        resp = await client.get("/api/v1/settings/smtp")
        assert resp.status_code == 401

        resp = await client.put(
            "/api/v1/settings/smtp",
            json={"host": "x", "from_email": "x@x.com"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_test_email_returns_202_on_success(self, db_client: AsyncClient) -> None:
        """POST /smtp/test returns 202 when email is sent successfully."""
        await _full_auth(db_client)

        # Configure SMTP.
        await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "secret123",
                "from_email": "noreply@example.com",
            },
        )

        # Mock the actual send.
        with patch("jai.services.email._send_mail", new_callable=AsyncMock):
            resp = await db_client.post("/api/v1/settings/smtp/test")
            assert resp.status_code == 202
            assert resp.json()["status"] == "sent"

    @pytest.mark.asyncio
    async def test_test_email_returns_400_when_smtp_not_configured(
        self, db_client: AsyncClient
    ) -> None:
        """POST /smtp/test returns 400 when SMTP is not configured."""
        await _full_auth(db_client)

        # Clear settings cache so the email service re-reads from DB.
        settings_svc._cache.clear()

        resp = await db_client.post("/api/v1/settings/smtp/test")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "SMTP_SEND_FAILED"

    @pytest.mark.asyncio
    async def test_test_email_returns_400_on_send_failure(self, db_client: AsyncClient) -> None:
        """POST /smtp/test returns 400 when sending fails."""
        await _full_auth(db_client)

        # Configure SMTP.
        await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "secret123",
                "from_email": "noreply@example.com",
            },
        )

        with patch(
            "jai.services.email._send_mail",
            new_callable=AsyncMock,
            side_effect=Exception("Connection refused"),
        ):
            resp = await db_client.post("/api/v1/settings/smtp/test")
            assert resp.status_code == 400
            assert resp.json()["detail"] == "SMTP_SEND_FAILED"


# ---------------------------------------------------------------------------
# Password reset token (unit tests)
# ---------------------------------------------------------------------------


class TestResetToken:
    """Unit tests for password-reset JWT generation and validation."""

    def test_generate_and_verify_valid_token(self) -> None:
        """A freshly generated token verifies and returns the user ID."""
        from jai.api.auth import _generate_reset_token, _verify_reset_token

        uid = str(uuid.uuid4())
        token = _generate_reset_token(uid)
        result = _verify_reset_token(token)
        assert result == uid

    def test_expired_token_returns_none(self) -> None:
        """An expired token returns None."""
        from jai.api.auth import _RESET_TOKEN_AUDIENCE, _verify_reset_token

        uid = str(uuid.uuid4())
        expired = datetime.now(UTC) - timedelta(hours=1)
        payload = {
            "sub": uid,
            "aud": _RESET_TOKEN_AUDIENCE,
            "exp": expired,
            "iat": expired - timedelta(hours=1),
        }
        token = pyjwt.encode(payload, get_auth_secret(), algorithm="HS256")
        assert _verify_reset_token(token) is None

    def test_wrong_audience_returns_none(self) -> None:
        """A token with wrong audience returns None."""
        from jai.api.auth import _verify_reset_token

        uid = str(uuid.uuid4())
        payload = {
            "sub": uid,
            "aud": "wrong-audience",
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
        }
        token = pyjwt.encode(payload, get_auth_secret(), algorithm="HS256")
        assert _verify_reset_token(token) is None

    def test_wrong_secret_returns_none(self) -> None:
        """A token signed with a different secret returns None."""
        from jai.api.auth import _RESET_TOKEN_AUDIENCE, _verify_reset_token

        uid = str(uuid.uuid4())
        payload = {
            "sub": uid,
            "aud": _RESET_TOKEN_AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
        }
        token = pyjwt.encode(payload, "wrong-secret-key", algorithm="HS256")
        assert _verify_reset_token(token) is None

    def test_garbage_token_returns_none(self) -> None:
        """A non-JWT string returns None."""
        from jai.api.auth import _verify_reset_token

        assert _verify_reset_token("not-a-jwt") is None
        assert _verify_reset_token("") is None


# ---------------------------------------------------------------------------
# Forgot-password endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestForgotPassword:
    """Tests for ``POST /api/v1/auth/forgot-password``."""

    @pytest.mark.asyncio
    async def test_returns_202_for_existing_user(self, db_client: AsyncClient) -> None:
        """Existing user + SMTP configured → 202, email sent."""
        await _full_auth(db_client)

        # Configure SMTP so the email service thinks it can send.
        await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "secret123",
                "from_email": "noreply@example.com",
            },
        )

        with patch("jai.services.email._send_mail", new_callable=AsyncMock):
            resp = await db_client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "user@example.com"},
            )
            assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_returns_202_for_nonexistent_user(self, db_client: AsyncClient) -> None:
        """Non-existent user → still 202 (anti-enumeration)."""
        await _full_auth(db_client)

        with patch("jai.services.email._send_mail", new_callable=AsyncMock):
            resp = await db_client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "nobody@example.com"},
            )
            assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_returns_202_when_smtp_not_configured(self, db_client: AsyncClient) -> None:
        """SMTP not configured → still 202 (no email sent, no leak)."""
        await _full_auth(db_client)

        resp = await db_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "user@example.com"},
        )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_returns_202_when_smtp_half_configured(
        self, db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SMTP_HOST set but SMTP_FROM_EMAIL empty → still 202 (no 500, no leak).

        Regression test for the review finding: semi-configured env vars that
        cause SmtpSettings(from_email="") to fail validation must not leak
        through as a 500 error for existing users.
        """
        await _full_auth(db_client)

        # Simulate half-configured env: host is set but from_email is empty/invalid.
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM_EMAIL", "")

        # The endpoint must return 202 for an existing user (no 500).
        resp = await db_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "user@example.com"},
        )
        assert resp.status_code == 202

        # Also for a non-existent user.
        resp = await db_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_email_contains_reset_link(self, db_client: AsyncClient) -> None:
        """The sent email contains a reset-password URL with a token."""
        await _full_auth(db_client)

        await db_client.put(
            "/api/v1/settings/smtp",
            json={
                "host": "smtp.example.com",
                "password": "secret123",
                "from_email": "noreply@example.com",
            },
        )

        sent_args: dict[str, object] = {}

        async def _capture_send(cfg, to, subject, html_body):
            sent_args["to"] = to
            sent_args["subject"] = subject
            sent_args["html"] = html_body

        with patch(
            "jai.services.email._send_mail",
            side_effect=_capture_send,
        ):
            resp = await db_client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "user@example.com"},
            )
            assert resp.status_code == 202

        assert sent_args["to"] == "user@example.com"
        html = str(sent_args["html"])
        # The reset URL must be absolute (email clients can't resolve relative URLs).
        # Read base_url from current settings so the assertion works regardless
        # of the developer's .env configuration.
        from jai.config import get_settings

        expected_prefix = f"{get_settings().base_url.rstrip('/')}/reset-password?token="
        assert expected_prefix in html


# ---------------------------------------------------------------------------
# Reset-password endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestResetPassword:
    """Tests for ``POST /api/v1/auth/reset-password``."""

    @pytest.mark.asyncio
    async def test_reset_password_success(self, db_client: AsyncClient) -> None:
        """Valid token + valid new password → 204, password is updated."""
        await _full_auth(db_client)

        # Generate a reset token for the user.
        me = await db_client.get("/api/v1/users/me")
        user_id = me.json()["id"]

        from jai.api.auth import _generate_reset_token

        token = _generate_reset_token(user_id)

        resp = await db_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpassword1"},
        )
        assert resp.status_code == 204

        # Can login with the new password.
        await db_client.post("/api/v1/auth/logout")
        resp = await db_client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "newpassword1"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_reset_password_invalid_token(self, db_client: AsyncClient) -> None:
        """Invalid token → 400."""
        await _full_auth(db_client)

        resp = await db_client.post(
            "/api/v1/auth/reset-password",
            json={"token": "garbage", "password": "newpassword1"},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"].lower()
        assert "invalid" in detail or "expired" in detail

    @pytest.mark.asyncio
    async def test_reset_password_expired_token(self, db_client: AsyncClient) -> None:
        """Expired token → 400."""
        await _full_auth(db_client)

        me = await db_client.get("/api/v1/users/me")
        user_id = me.json()["id"]

        from jai.api.auth import _RESET_TOKEN_AUDIENCE

        expired = datetime.now(UTC) - timedelta(hours=1)
        payload = {
            "sub": user_id,
            "aud": _RESET_TOKEN_AUDIENCE,
            "exp": expired,
            "iat": expired - timedelta(hours=1),
        }
        token = pyjwt.encode(payload, get_auth_secret(), algorithm="HS256")

        resp = await db_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpassword1"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reset_password_short_password_rejected(self, db_client: AsyncClient) -> None:
        """Short new password → 400 (password validation)."""
        await _full_auth(db_client)

        me = await db_client.get("/api/v1/users/me")
        user_id = me.json()["id"]

        from jai.api.auth import _generate_reset_token

        token = _generate_reset_token(user_id)

        resp = await db_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "short"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reset_password_token_for_deleted_user(self, db_client: AsyncClient) -> None:
        """Token with a user ID that doesn't exist → 400."""
        from jai.api.auth import _generate_reset_token

        token = _generate_reset_token(str(uuid.uuid4()))

        resp = await db_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpassword1"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Email service (unit tests with mock SMTP)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEmailService:
    """Tests for ``services/email.py`` – email construction and sending."""

    @pytest.mark.asyncio
    async def test_send_password_reset_email_calls_send(
        self, db_session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """send_password_reset_email calls _send_mail with correct params."""
        from jai.services import email as email_svc

        # Store SMTP config in DB.
        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_SMTP,
                SmtpSettings(
                    host="smtp.test.com",
                    password="pw",
                    from_email="noreply@test.com",
                ),
                level=SettingLevel.GLOBAL,
            )
            await session.commit()

        sent: dict[str, object] = {}

        async def _mock_send(cfg, to, subject, html_body):
            sent["to"] = to
            sent["subject"] = subject
            sent["html"] = html_body

        with patch("jai.services.email._send_mail", side_effect=_mock_send):
            async with db_session_maker() as session:
                await email_svc.send_password_reset_email(
                    session, "user@test.com", "http://localhost/reset?token=abc"
                )

        assert sent["to"] == "user@test.com"
        assert "Reset" in str(sent["subject"]) or "重置" in str(sent["subject"])
        assert "http://localhost/reset?token=abc" in str(sent["html"])

    @pytest.mark.asyncio
    async def test_send_password_reset_escapes_xss(
        self, db_session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Jinja2 autoescape prevents XSS in the reset URL (red-line 7)."""
        from jai.services import email as email_svc

        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_SMTP,
                SmtpSettings(
                    host="smtp.test.com",
                    from_email="noreply@test.com",
                ),
                level=SettingLevel.GLOBAL,
            )
            await session.commit()

        html: str = ""

        async def _capture(cfg, to, subject, html_body):
            nonlocal html
            html = html_body

        xss_url = 'http://x.com/reset?<script>alert("xss")</script>'
        with patch("jai.services.email._send_mail", side_effect=_capture):
            async with db_session_maker() as session:
                await email_svc.send_password_reset_email(session, "user@test.com", xss_url)

        # The HTML must contain the escaped version, not raw <script>.
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    @pytest.mark.asyncio
    async def test_is_smtp_configured_false_when_no_config(
        self, db_session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """is_smtp_configured returns False when no SMTP settings exist."""
        from jai.services import email as email_svc

        # Clear any cached settings from previous tests.
        settings_svc._cache.clear()

        async with db_session_maker() as session:
            result = await email_svc.is_smtp_configured(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_is_smtp_configured_true_after_saving(
        self, db_session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """is_smtp_configured returns True after saving valid SMTP settings."""
        from jai.services import email as email_svc

        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_SMTP,
                SmtpSettings(
                    host="smtp.test.com",
                    from_email="noreply@test.com",
                ),
                level=SettingLevel.GLOBAL,
            )
            await session.commit()

        settings_svc._cache.clear()
        async with db_session_maker() as session:
            result = await email_svc.is_smtp_configured(session)
        assert result is True

    @pytest.mark.asyncio
    async def test_send_test_email_raises_when_not_configured(
        self, db_session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """send_test_email raises RuntimeError when SMTP is not configured."""
        from jai.services import email as email_svc

        settings_svc._cache.clear()
        async with db_session_maker() as session:
            with pytest.raises(RuntimeError, match="not configured"):
                await email_svc.send_test_email(session, "user@test.com")

    @pytest.mark.asyncio
    async def test_send_test_email_calls_send(
        self, db_session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """send_test_email successfully delegates to _send_mail."""
        from jai.services import email as email_svc

        async with db_session_maker() as session:
            await settings_svc.set_setting(
                session,
                SETTING_KEY_SMTP,
                SmtpSettings(
                    host="smtp.test.com",
                    from_email="noreply@test.com",
                ),
                level=SettingLevel.GLOBAL,
            )
            await session.commit()

        sent: dict[str, object] = {}

        async def _mock_send(cfg, to, subject, html_body):
            sent["to"] = to
            sent["subject"] = subject

        with patch("jai.services.email._send_mail", side_effect=_mock_send):
            async with db_session_maker() as session:
                await email_svc.send_test_email(session, "owner@test.com")

        assert sent["to"] == "owner@test.com"
        assert "Test" in str(sent["subject"])

    @pytest.mark.asyncio
    async def test_get_smtp_config_degrades_on_invalid_env(
        self,
        db_session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_get_smtp_config returns None when env vars are semi-configured.

        Regression test: SMTP_HOST set but SMTP_FROM_EMAIL empty/invalid must
        not raise — it should safely degrade to None.
        """
        from jai.services import email as email_svc

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM_EMAIL", "")

        async with db_session_maker() as session:
            result = await email_svc._get_smtp_config(session)
        assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sent_copy_settings_backward_put_and_password_lifecycle(db_client):
    """Old clients preserve IMAP settings through keep/update/clear password writes."""
    await _full_auth(db_client)
    url = "/api/v1/settings/smtp"
    defaults = (await db_client.get(url)).json()
    assert defaults["sent_copy"]["enabled"] is False
    payload = {
        "host": "smtp.example.com",
        "username": "login",
        "password": "SECRET-ONE",
        "from_email": "from@example.com",
        "sent_copy": {
            "enabled": True,
            "host": "imap.example.com",
            "port": 1143,
            "security": "STARTTLS",
            "folder": " My Sent ",
        },
    }
    saved = await db_client.put(url, json=payload)
    assert saved.status_code == 200, saved.text
    expected = saved.json()["sent_copy"]
    assert expected["folder"] == "My Sent" and expected["port"] == 1143
    assert "SECRET-ONE" not in saved.text
    del payload["sent_copy"]
    for password, is_set in [(None, True), ("SECRET-TWO", True), ("", False)]:
        payload["password"] = password
        response = await db_client.put(url, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["sent_copy"] == expected
        assert response.json()["password_set"] == is_set
        assert "SECRET" not in response.text
        assert (await db_client.get(url)).json()["sent_copy"] == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sent_copy_test_response_warning_and_safe_smtp_error(db_client):
    from jai.schemas.email_log import SentCopyResult

    await _full_auth(db_client)
    with patch(
        "jai.services.email.send_test_email",
        return_value=SentCopyResult(
            sent_copy_status="FAILED", sent_copy_error_code="IMAP_AUTH_FAILED"
        ),
    ):
        response = await db_client.post("/api/v1/settings/smtp/test")
    assert response.status_code == 202
    assert response.json() == {
        "status": "sent",
        "sent_copy_status": "FAILED",
        "sent_copy_error_code": "IMAP_AUTH_FAILED",
        "sent_copy_saved_at": None,
    }
    with patch(
        "jai.services.email.send_test_email", side_effect=RuntimeError("SECRET MIME PASSWORD")
    ):
        response = await db_client.post("/api/v1/settings/smtp/test")
    assert response.status_code == 400
    assert response.json()["detail"] == "SMTP_SEND_FAILED"
    assert "SECRET" not in response.text
