"""M15 token-format and scope authorization boundary tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from jai.auth.deps import current_mfa_user
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.services.agent_tokens import AgentPrincipal, issue_token, parse_token, require_agent_scope


@pytest.mark.parametrize("scope", list(AgentTokenScope))
def test_each_typed_scope_allows_only_itself(scope: AgentTokenScope) -> None:
    principal = AgentPrincipal(uuid.uuid4(), uuid.uuid4(), frozenset({scope}))
    require_agent_scope(scope, principal)
    for other in AgentTokenScope:
        if other == scope:
            continue
        with pytest.raises(HTTPException) as exc:
            require_agent_scope(other, principal)
        assert exc.value.status_code == 403
        assert exc.value.detail == {"code": "MCP_INSUFFICIENT_SCOPE"}


def test_token_has_company_and_selector_but_only_random_secret_is_digestable() -> None:
    company_id = uuid.uuid4()
    row, value = issue_token(
        AgentTokenCreate(name="Hermes", scopes=[AgentTokenScope.RECEIPT_INTAKE]), company_id
    )
    parsed = parse_token(value)
    assert parsed is not None
    assert parsed[0] == company_id
    assert parsed[1] == row.selector
    assert len(parsed[2]) == 43
    assert len(row.secret_digest) == 32
    assert parsed[2].encode("ascii") not in row.secret_digest
    assert "Hermes" not in value
    assert parse_token(value + "x") is None


def test_missing_principal_is_unauthenticated() -> None:
    with pytest.raises(HTTPException) as exc:
        require_agent_scope(AgentTokenScope.CATALOG_READ, None)
    assert exc.value.status_code == 401
    assert exc.value.detail == {"code": "MCP_UNAUTHENTICATED"}


async def test_mcp_cookie_does_not_authenticate(client: AsyncClient) -> None:
    client.cookies.set("jai-auth", "browser-session")
    response = await client.post(
        "/api/v1/mcp",
        headers={"host": "localhost"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MCP_UNAUTHENTICATED"


async def test_mfa_dependency_rejects_unenrolled_session() -> None:
    from types import SimpleNamespace

    with pytest.raises(HTTPException) as exc:
        await current_mfa_user(SimpleNamespace(mfa_enabled=False, totp_secret=None))  # type: ignore[arg-type]
    assert exc.value.status_code == 401
