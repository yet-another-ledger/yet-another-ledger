"""Owner-managed Agent token API signatures frozen in M15 Step 1."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from jai.auth.deps import current_mfa_user
from jai.models.user import User
from jai.schemas.agent_token import (
    AgentTokenCreate,
    AgentTokenCreated,
    AgentTokenListResponse,
    AgentTokenRevoked,
)

router = APIRouter(prefix="/api/v1/agent-tokens", tags=["agent-tokens"])


def _require_owner(user: User) -> None:
    if user.role != "owner" or user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner access required.")


@router.get("", response_model=AgentTokenListResponse)
async def list_agent_tokens(
    user: Annotated[User, Depends(current_mfa_user)],
) -> AgentTokenListResponse:
    """List token metadata; the plaintext secret is never part of this schema."""
    _require_owner(user)
    raise HTTPException(status_code=501, detail="Agent token management is not enabled yet.")


@router.post("", response_model=AgentTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_agent_token(
    body: AgentTokenCreate,
    user: Annotated[User, Depends(current_mfa_user)],
) -> AgentTokenCreated:
    """Issue one token and return its secret once after Step 2 is implemented."""
    _require_owner(user)
    raise HTTPException(status_code=501, detail="Agent token management is not enabled yet.")


@router.delete("/{token_id}", response_model=AgentTokenRevoked)
async def revoke_agent_token(
    token_id: uuid.UUID,
    user: Annotated[User, Depends(current_mfa_user)],
) -> AgentTokenRevoked:
    """Revoke a token; the final route shape is reserved for Step 2."""
    _require_owner(user)
    raise HTTPException(status_code=501, detail="Agent token management is not enabled yet.")
