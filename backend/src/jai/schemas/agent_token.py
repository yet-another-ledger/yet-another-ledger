"""Frozen owner-managed Agent token API contract (M15)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentTokenScope(StrEnum):
    """Capabilities a token may receive."""

    RECEIPT_INTAKE = "receipt:intake"
    CUSTOMER_READ = "customer:read"
    CUSTOMER_CREATE = "customer:create"
    CATALOG_READ = "catalog:read"
    QUOTE_READ = "quote:read"
    QUOTE_WRITE_DRAFT = "quote:write_draft"
    INVOICE_READ = "invoice:read"
    INVOICE_WRITE_DRAFT = "invoice:write_draft"


class AgentTokenCreate(BaseModel):
    """Create a named token with a non-empty set of allowed capabilities."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)]
    scopes: Annotated[list[AgentTokenScope], Field(min_length=1)]

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Token name must not be blank.")
        return value

    @model_validator(mode="after")
    def require_unique_scopes(self) -> AgentTokenCreate:
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("Token scopes must be unique.")
        return self


class AgentTokenRead(BaseModel):
    """Non-secret token metadata returned by list and creation endpoints."""

    id: uuid.UUID
    name: str
    scopes: list[AgentTokenScope]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class AgentTokenCreated(AgentTokenRead):
    """One-time creation response; `token` is never returned by list routes."""

    token: Annotated[str, Field(json_schema_extra={"writeOnly": True})]


class AgentTokenListResponse(BaseModel):
    items: list[AgentTokenRead]


class AgentTokenRevoked(BaseModel):
    id: uuid.UUID
    revoked_at: datetime
