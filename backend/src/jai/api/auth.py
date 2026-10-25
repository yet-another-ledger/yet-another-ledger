"""Authentication API routes – register, login, logout, bootstrap, /me, MFA,
forgot-password, reset-password.

The login flow is custom (JSON body instead of OAuth2 form) while leveraging
fastapi-users' ``JWTStrategy`` for token creation and ``CookieTransport``
parameters for cookie management.

Step 3:
  - Login: password correct → issue pre-auth cookie → ``{next: "mfa_setup"|"mfa_verify"}``.
  - MFA setup: generate pending TOTP secret, return to client.
  - MFA verify: validate TOTP code → upgrade to full session cookie.
    First binding also persists ``totp_secret`` + ``mfa_enabled``.
    (``onboarding.completed`` is set by company first save, not here.)

Step 4:
  - Forgot-password: ``POST /auth/forgot-password`` → always 202 (anti-enumeration).
    Sends a reset-token email when the user exists and SMTP is configured.
  - Reset-password: ``POST /auth/reset-password`` → validates the token and
    sets the new password.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi_users.authentication.strategy.jwt import JWTStrategy
from fastapi_users.authentication.transport.cookie import CookieTransport
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.backends import cookie_backend, pre_auth_backend
from jai.auth.deps import (
    current_mfa_user,
    is_onboarding_completed,
    is_registration_open,
)
from jai.auth.secret import get_auth_secret
from jai.auth.user_manager import UserManager, get_user_manager, verify_password
from jai.config import get_settings
from jai.db import get_session
from jai.models.user import User
from jai.schemas.user import UserCreate, UserRead
from jai.services import email as email_svc
from jai.services import mfa as mfa_svc

logger = logging.getLogger("jai.auth")

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class BootstrapResponse(BaseModel):
    """Returned by ``GET /auth/bootstrap`` – drives frontend routing."""

    registration_open: bool
    onboarding_completed: bool


class RegisterRequest(BaseModel):
    """Body for ``POST /auth/register`` – only the fields a client may set.

    Defined locally (rather than reusing fastapi-users' ``BaseUserCreate``) so
    the public contract is exactly ``{email, password}`` and never exposes
    ``is_active`` / ``is_superuser`` / ``is_verified``.
    """

    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    """Body for ``POST /auth/login``."""

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Returned by ``POST /auth/login``.

    ``next`` indicates the required next step:
    - ``"mfa_setup"``: user has not yet bound TOTP → show setup wizard.
    - ``"mfa_verify"``: user already has TOTP → prompt for verification code.
    """

    next: Literal["mfa_setup", "mfa_verify"]


class MfaSetupResponse(BaseModel):
    """Returned by ``POST /auth/mfa/setup``."""

    secret: str
    otpauth_uri: str


class MfaVerifyRequest(BaseModel):
    """Body for ``POST /auth/mfa/verify``."""

    code: str


class ForgotPasswordRequest(BaseModel):
    """Body for ``POST /auth/forgot-password``."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Body for ``POST /auth/reset-password``."""

    token: str
    password: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

users_router = APIRouter(prefix="/api/v1/users", tags=["users"])

#: Stable advisory-lock key serialising the first-account registration gate.
#: Two concurrent first-boot registrations must not both pass the empty-DB
#: check and create two owners.
_REGISTRATION_LOCK_KEY = 4915_0001

# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

_CookieTransportT = CookieTransport  # alias for line-length
_JWTStrategyT = JWTStrategy[User, object]


async def _set_session_cookie(user: User, response: Response) -> str:
    """Create a full-session JWT and set it as the ``jai_session`` cookie.

    Returns the raw token (rarely needed, but useful for testing).
    """
    strategy = cast(_JWTStrategyT, cookie_backend.get_strategy())
    token = await strategy.write_token(user)
    transport = cast(_CookieTransportT, cookie_backend.transport)
    response.set_cookie(
        transport.cookie_name,
        token,
        max_age=transport.cookie_max_age,
        path=transport.cookie_path,
        domain=transport.cookie_domain,
        secure=transport.cookie_secure,
        httponly=transport.cookie_httponly,
        samesite=transport.cookie_samesite,
    )
    return token


def _clear_session_cookie(response: Response) -> None:
    """Clear the ``jai_session`` cookie."""
    transport = cast(_CookieTransportT, cookie_backend.transport)
    response.set_cookie(
        transport.cookie_name,
        "",
        max_age=0,
        path=transport.cookie_path,
        domain=transport.cookie_domain,
        secure=transport.cookie_secure,
        httponly=transport.cookie_httponly,
        samesite=transport.cookie_samesite,
    )


async def _set_pre_auth_cookie(user: User, response: Response) -> str:
    """Create a short-lived pre-auth JWT and set ``jai_pre_auth`` cookie."""
    strategy = cast(_JWTStrategyT, pre_auth_backend.get_strategy())
    token = await strategy.write_token(user)
    transport = cast(_CookieTransportT, pre_auth_backend.transport)
    response.set_cookie(
        transport.cookie_name,
        token,
        max_age=transport.cookie_max_age,
        path=transport.cookie_path,
        domain=transport.cookie_domain,
        secure=transport.cookie_secure,
        httponly=transport.cookie_httponly,
        samesite=transport.cookie_samesite,
    )
    return token


def _clear_pre_auth_cookie(response: Response) -> None:
    """Clear the ``jai_pre_auth`` cookie."""
    transport = cast(_CookieTransportT, pre_auth_backend.transport)
    response.set_cookie(
        transport.cookie_name,
        "",
        max_age=0,
        path=transport.cookie_path,
        domain=transport.cookie_domain,
        secure=transport.cookie_secure,
        httponly=transport.cookie_httponly,
        samesite=transport.cookie_samesite,
    )


# ---------------------------------------------------------------------------
# Pre-auth user extraction
# ---------------------------------------------------------------------------


async def _get_pre_auth_user(
    request: Request,
    user_manager: UserManager,
) -> User:
    """Extract and validate the user from the ``jai_pre_auth`` cookie."""
    transport = cast(_CookieTransportT, pre_auth_backend.transport)
    token = request.cookies.get(transport.cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Pre-authentication required.",
        )
    strategy = cast(_JWTStrategyT, pre_auth_backend.get_strategy())
    user = await strategy.read_token(token, user_manager)  # type: ignore[arg-type]
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired pre-auth token.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user.",
        )
    return user


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@router.get("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(
    session: AsyncSession = Depends(get_session),
) -> BootstrapResponse:
    """Return the app's bootstrap state for the frontend to decide routing."""
    return BootstrapResponse(
        registration_open=await is_registration_open(session),
        onboarding_completed=await is_onboarding_completed(session),
    )


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
    user_manager: UserManager = Depends(get_user_manager),
) -> UserRead:
    """Register a new user.  Only allowed when no users exist (first-boot)."""
    # Serialise the gate check + create against concurrent first registrations.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)").bindparams(
            key=_REGISTRATION_LOCK_KEY
        )
    )
    if not await is_registration_open(session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed.",
        )
    try:
        user = await user_manager.create(
            UserCreate(email=body.email, password=body.password), safe=True
        )
    except Exception as exc:
        # fastapi-users raises InvalidPasswordException / UserAlreadyExists
        # which we surface as 400.
        detail = getattr(exc, "reason", None) or str(exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        ) from exc
    return UserRead.model_validate(user)


# ---------------------------------------------------------------------------
# Login (step 3: password → pre-auth cookie → {next: mfa_setup|mfa_verify})
# ---------------------------------------------------------------------------


@router.post("/login", response_model=LoginResponse)
async def login(
    response: Response,
    body: LoginRequest,
    user_manager: UserManager = Depends(get_user_manager),
) -> LoginResponse:
    """Verify credentials and issue a pre-auth cookie.

    Returns ``{"next": "mfa_setup"}`` if the user has not yet bound TOTP,
    or ``{"next": "mfa_verify"}`` if TOTP is already configured.
    """
    try:
        user = await user_manager.get_by_email(str(body.email))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid credentials.",
        ) from None
    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid credentials.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user.",
        )

    await _set_pre_auth_cookie(user, response)

    if user.mfa_enabled and user.totp_secret:
        next_step: Literal["mfa_setup", "mfa_verify"] = "mfa_verify"
    else:
        next_step = "mfa_setup"

    return LoginResponse(next=next_step)


# ---------------------------------------------------------------------------
# MFA setup
# ---------------------------------------------------------------------------


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    request: Request,
    user_manager: UserManager = Depends(get_user_manager),
) -> MfaSetupResponse:
    """Generate a pending TOTP secret for the authenticated user.

    Requires the ``jai_pre_auth`` cookie (set by the login endpoint).
    The secret is **not** persisted until the user successfully verifies a
    code via ``POST /auth/mfa/verify``.
    """
    user = await _get_pre_auth_user(request, user_manager)

    secret = mfa_svc.generate_totp_secret()
    mfa_svc.store_pending_secret(str(user.id), secret)
    otpauth_uri = mfa_svc.build_otpauth_uri(secret, user.email)

    return MfaSetupResponse(secret=secret, otpauth_uri=otpauth_uri)


# ---------------------------------------------------------------------------
# MFA verify
# ---------------------------------------------------------------------------


@router.post("/mfa/verify", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_verify(
    request: Request,
    response: Response,
    body: MfaVerifyRequest,
    session: AsyncSession = Depends(get_session),
    user_manager: UserManager = Depends(get_user_manager),
) -> None:
    """Verify a TOTP code and upgrade the pre-auth cookie to a full session.

    Two cases:

    1. **First binding** (user has no ``totp_secret``): the code is verified
       against the pending secret generated by ``/mfa/setup``.  On success,
       ``totp_secret`` is persisted and ``mfa_enabled`` is set to ``True``.
    2. **Subsequent login** (user has ``mfa_enabled=True``): the code is
       verified against the stored ``totp_secret``.

    On success the ``jai_pre_auth`` cookie is cleared and a ``jai_session``
    cookie is set.
    """
    user = await _get_pre_auth_user(request, user_manager)

    code = body.code.strip()

    if user.mfa_enabled and user.totp_secret:
        # Subsequent login – verify against stored secret.
        if not mfa_svc.verify_totp_code(user.totp_secret, code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid verification code.",
            )
    else:
        # First binding – verify against pending secret.
        pending = mfa_svc.get_pending_secret(str(user.id))
        if pending is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No pending MFA setup. Please start setup again.",
            )
        if not mfa_svc.verify_totp_code(pending, code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid verification code.",
            )
        # Persist the secret.
        user.totp_secret = pending
        user.mfa_enabled = True
        mfa_svc.pop_pending_secret(str(user.id))

    await session.commit()

    # Upgrade: clear pre-auth, set full session.
    _clear_pre_auth_cookie(response)
    await _set_session_cookie(user, response)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    """Clear the session cookie and pre-auth cookie.

    Public and idempotent: it always returns cookie-clearing response headers,
    even when the incoming cookies are missing, expired, or no longer
    verifiable (e.g. after a secret rotation).  Since the cookies are
    ``httpOnly`` the frontend cannot clear them on its own, so the server must
    do so unconditionally.
    """
    _clear_session_cookie(response)
    _clear_pre_auth_cookie(response)


# ---------------------------------------------------------------------------
# Password reset (step 4)
# ---------------------------------------------------------------------------

#: JWT audience for password-reset tokens.
_RESET_TOKEN_AUDIENCE = "jai:reset-password"


def _generate_reset_token(user_id: str) -> str:
    """Create a signed JWT for password reset (short-lived)."""
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.reset_password_ttl_minutes
    )
    payload = {
        "sub": user_id,
        "aud": _RESET_TOKEN_AUDIENCE,
        "exp": expires_at,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, get_auth_secret(), algorithm="HS256")


def _verify_reset_token(token: str) -> str | None:
    """Validate a password-reset JWT and return the user ID (or ``None``)."""
    try:
        payload = jwt.decode(
            token,
            get_auth_secret(),
            algorithms=["HS256"],
            audience=_RESET_TOKEN_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    user_id: str | None = payload.get("sub")
    return user_id


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_session),
    user_manager: UserManager = Depends(get_user_manager),
) -> dict[str, str]:
    """Request a password reset.

    **Always returns 202** to prevent email-enumeration.  When the user exists
    and SMTP is configured, a reset-token email is sent in the background.
    """
    try:
        user = await user_manager.get_by_email(str(body.email))
    except Exception:
        # User does not exist – still return 202.
        return {"status": "accepted"}

    if not user or not user.is_active:
        return {"status": "accepted"}

    # Everything below must be wrapped so that SMTP config errors or send
    # failures never produce a non-202 response (anti-enumeration guarantee).
    try:
        if not await email_svc.is_smtp_configured(session):
            logger.warning(
                "SMTP not configured – password-reset email skipped for %s",
                body.email,
            )
            return {"status": "accepted"}

        token = _generate_reset_token(str(user.id))
        # Build an absolute reset URL for the frontend.
        # Email clients cannot resolve relative URLs, so ``base_url`` must be set.
        settings = get_settings()
        reset_url = f"{settings.base_url.rstrip('/')}/reset-password?token={token}"

        await email_svc.send_password_reset_email(
            session, user.email, reset_url
        )
    except Exception:
        logger.warning("Password-reset SMTP send failed", extra={"error_code": "SMTP_SEND_FAILED"})

    return {"status": "accepted"}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
    user_manager: UserManager = Depends(get_user_manager),
) -> None:
    """Reset a user's password using a valid reset token.

    Returns 204 on success, 400 on invalid/expired token or password validation
    failure.
    """
    user_id = _verify_reset_token(body.token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    try:
        user = await user_manager.get(uuid.UUID(user_id))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        ) from None

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    try:
        await user_manager._update(user, {"password": body.password})
    except Exception as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        ) from exc


# ---------------------------------------------------------------------------
# /users/me
# ---------------------------------------------------------------------------


@users_router.get("/me", response_model=UserRead)
async def me(
    user: Annotated[User, Depends(current_mfa_user)],
) -> UserRead:
    """Return the currently authenticated user (requires MFA completed)."""
    return UserRead.model_validate(user)
