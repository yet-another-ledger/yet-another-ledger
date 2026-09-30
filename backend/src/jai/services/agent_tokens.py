"""Token creation and per-request verification for the M15 Agent boundary."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models.agent_token import AgentToken
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope


@dataclass(frozen=True)
class AgentPrincipal:
    token_id: uuid.UUID
    company_id: uuid.UUID
    scopes: frozenset[AgentTokenScope]


def issue_token(body: AgentTokenCreate, company_id: uuid.UUID) -> tuple[AgentToken, str]:
    """Build a token with an opaque selector and a 256-bit random secret."""
    selector = uuid.uuid4()
    secret = secrets.token_bytes(32)
    secret_text = base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")
    raw = f"jai1.{company_id}.{selector}.{secret_text}"
    token = AgentToken(
        company_id=company_id,
        name=body.name,
        selector=selector,
        secret_digest=hashlib.sha256(secret_text.encode("ascii")).digest(),
        scopes=[scope.value for scope in body.scopes],
        created_at=datetime.now(UTC),
    )
    return token, raw


def parse_token(value: str) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != "jai1" or len(parts[3]) != 43:
        return None
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(char not in allowed for char in parts[3]):
        return None
    try:
        secret_bytes = base64.urlsafe_b64decode(parts[3] + "=")
        company_id = uuid.UUID(parts[1])
        selector = uuid.UUID(parts[2])
    except (ValueError, TypeError, UnicodeError):
        return None
    if (
        len(secret_bytes) != 32
        or base64.urlsafe_b64encode(secret_bytes).decode("ascii").rstrip("=") != parts[3]
    ):
        return None
    return company_id, selector, parts[3]


async def authenticate_token(session: AsyncSession, value: str) -> AgentPrincipal | None:
    """Set company RLS before selector lookup, then verify the secret digest."""
    parsed = parse_token(value)
    if parsed is None:
        return None
    company_id, selector, secret = parsed
    await set_rls_company(session, company_id)
    result = await session.execute(
        select(AgentToken).where(
            AgentToken.selector == selector,
            AgentToken.revoked_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    candidate = hashlib.sha256(secret.encode("ascii")).digest()
    if not hmac.compare_digest(row.secret_digest, candidate):
        return None
    row.last_used_at = datetime.now(UTC)
    await session.commit()
    return AgentPrincipal(
        token_id=row.id,
        company_id=company_id,
        scopes=frozenset(AgentTokenScope(scope) for scope in row.scopes),
    )


def require_agent_scope(scope: AgentTokenScope, principal: AgentPrincipal | None) -> None:
    """Typed authorization guard for each future MCP tool invocation."""
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "MCP_UNAUTHENTICATED"})
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail={"code": "MCP_INSUFFICIENT_SCOPE"})
