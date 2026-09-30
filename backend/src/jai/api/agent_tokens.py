"""MFA-owner API for issuing, listing and revoking Agent tokens."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.db import get_session, set_rls_company
from jai.models.agent_token import AgentToken as AgentTokenModel
from jai.models.user import User
from jai.schemas.agent_token import (
    AgentTokenCreate,
    AgentTokenCreated,
    AgentTokenListResponse,
    AgentTokenRead,
    AgentTokenRevoked,
    AgentTokenScope,
)

router = APIRouter(prefix="/api/v1/agent-tokens", tags=["agent-tokens"])


def _require_owner(user: User) -> None:
    if user.role != "owner" or user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner access required.")


@router.get("", response_model=AgentTokenListResponse)
async def list_agent_tokens(
    user: Annotated[User, Depends(current_mfa_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentTokenListResponse:
    """List token metadata; the plaintext secret is never part of this schema."""
    _require_owner(user)
    assert user.company_id is not None
    await set_rls_company(session, user.company_id)
    result = await session.execute(
        select(AgentTokenModel)
        .order_by(AgentTokenModel.created_at.desc(), AgentTokenModel.id)
    )
    return AgentTokenListResponse(items=[_read(row) for row in result.scalars()])


@router.post("", response_model=AgentTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_agent_token(
    body: AgentTokenCreate,
    user: Annotated[User, Depends(current_mfa_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentTokenCreated:
    """Issue one token and return its secret only in this response."""
    _require_owner(user)
    assert user.company_id is not None
    from jai.services.agent_tokens import issue_token

    await set_rls_company(session, user.company_id)
    row, raw = issue_token(body, user.company_id)
    row.creator_id = user.id
    session.add(row)
    await session.commit()
    return AgentTokenCreated(**_read(row).model_dump(), token=raw)


@router.delete("/{token_id}", response_model=AgentTokenRevoked)
async def revoke_agent_token(
    token_id: uuid.UUID,
    user: Annotated[User, Depends(current_mfa_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentTokenRevoked:
    """Revoke a company token immediately and idempotently."""
    _require_owner(user)
    assert user.company_id is not None
    await set_rls_company(session, user.company_id)
    result = await session.execute(select(AgentTokenModel).where(AgentTokenModel.id == token_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent token not found.")
    if row.revoked_at is None:
        from datetime import UTC, datetime

        row.revoked_at = datetime.now(UTC)
        await session.commit()
    return AgentTokenRevoked(id=row.id, revoked_at=row.revoked_at)


def _read(row: AgentTokenModel) -> AgentTokenRead:
    return AgentTokenRead(
        id=row.id,
        name=row.name,
        scopes=[AgentTokenScope(scope) for scope in row.scopes],
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )
