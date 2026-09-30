"""PostgreSQL verification of Agent token ownership and RLS enforcement."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.api import mcp as mcp_module
from jai.auth.deps import current_mfa_user
from jai.db import get_session
from jai.main import app
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.models.user import User
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.services.agent_tokens import authenticate_token, issue_token


@pytest.fixture
async def token_owner(
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    company_id, other_company_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="Agent test", base_currency="EUR"),
                Company(id=other_company_id, name="Other tenant", base_currency="EUR"),
                User(
                    id=user_id,
                    email=f"{user_id}@example.test",
                    hashed_password="not-a-real-password-hash",
                    company_id=company_id,
                    role="owner",
                    mfa_enabled=True,
                    totp_secret="test-secret",
                ),
            ]
        )
        await session.commit()
    yield company_id, other_company_id, user_id


@pytest.mark.integration
async def test_runtime_role_raw_sql_obeys_agent_token_company_rls(
    token_owner: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_id, other_company_id, user_id = token_owner
    token_id, selector = uuid.uuid4(), uuid.uuid4()
    async with admin_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO agent_token (id, company_id, creator_id, name, selector, "
                "secret_digest, scopes) VALUES (:id, :company, :creator, 'RLS test', "
                ":selector, :digest, ARRAY['receipt:intake']::text[])"
            ),
            {
                "id": token_id,
                "company": company_id,
                "creator": user_id,
                "selector": selector,
                "digest": b"x" * 32,
            },
        )
        await session.commit()

    async with runtime_session_maker() as session:
        hidden = await session.execute(
            text("SELECT id FROM agent_token WHERE id = :id"), {"id": token_id}
        )
        assert hidden.scalar_one_or_none() is None
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(company_id)},
        )
        visible = await session.execute(
            text("SELECT id FROM agent_token WHERE id = :id"), {"id": token_id}
        )
        assert visible.scalar_one() == token_id
        await session.execute(
            text("SELECT set_config('jai.company_id', :company, true)"),
            {"company": str(other_company_id)},
        )
        isolated = await session.execute(
            text("SELECT id FROM agent_token WHERE id = :id"), {"id": token_id}
        )
        assert isolated.scalar_one_or_none() is None


@pytest.mark.integration
async def test_owner_api_shows_secret_once_and_rejects_non_owner(
    token_owner: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    db_client: AsyncClient,
) -> None:
    company_id, other_company_id, user_id = token_owner
    owner = SimpleNamespace(id=user_id, company_id=company_id, role="owner")
    app.dependency_overrides[current_mfa_user] = lambda: owner
    try:
        created = await db_client.post(
            "/api/v1/agent-tokens",
            json={"name": "Hermes", "scopes": ["receipt:intake"]},
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        assert payload["token"].startswith(f"jai1.{company_id}.")
        assert "secret_digest" not in payload
        token_id = payload["id"]

        listed = await db_client.get("/api/v1/agent-tokens")
        assert listed.status_code == 200
        assert "token" not in listed.text
        assert "secret_digest" not in listed.text
        assert listed.json()["items"][0]["id"] == token_id

        # The API session is the NOBYPASSRLS runtime role. A foreign row with
        # a known id must stay invisible to both owner listing and revocation.
        foreign_row, _ = issue_token(
            AgentTokenCreate(name="Foreign tenant", scopes=[AgentTokenScope.RECEIPT_INTAKE]),
            other_company_id,
        )
        foreign_row.creator_id = user_id
        async with admin_session_maker() as session:
            session.add(foreign_row)
            await session.commit()

        listed = await db_client.get("/api/v1/agent-tokens")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [token_id]
        foreign_revoke = await db_client.delete(f"/api/v1/agent-tokens/{foreign_row.id}")
        assert foreign_revoke.status_code == 404

        other = SimpleNamespace(id=uuid.uuid4(), company_id=company_id, role="user")
        app.dependency_overrides[current_mfa_user] = lambda: other
        forbidden = await db_client.get("/api/v1/agent-tokens")
        assert forbidden.status_code == 403
        app.dependency_overrides[current_mfa_user] = lambda: owner
        revoked = await db_client.delete(f"/api/v1/agent-tokens/{token_id}")
        assert revoked.status_code == 200
        assert revoked.json()["revoked_at"] is not None
    finally:
        app.dependency_overrides.pop(current_mfa_user, None)
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.integration
async def test_bearer_lookup_rejects_wrong_revoked_and_other_company_tokens(
    token_owner: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_id, other_company_id, user_id = token_owner
    request = AgentTokenCreate(name="runtime token", scopes=[AgentTokenScope.RECEIPT_INTAKE])
    first_row, first_secret = issue_token(request, company_id)
    first_row.creator_id = user_id
    second_row, second_secret = issue_token(request, other_company_id)
    second_row.creator_id = user_id
    async with admin_session_maker() as session:
        session.add_all([first_row, second_row])
        await session.commit()

    async with runtime_session_maker() as session:
        authenticated = await authenticate_token(session, first_secret)
        assert authenticated is not None
        assert authenticated.company_id == company_id
    token_prefix, secret_part = first_secret.rsplit(".", 1)
    wrong_secret = f"{token_prefix}.{('A' if secret_part[0] != 'A' else 'B')}{secret_part[1:]}"
    async with runtime_session_maker() as session:
        assert await authenticate_token(session, wrong_secret) is None
    foreign_token = second_secret.replace(str(other_company_id), str(company_id), 1)
    async with runtime_session_maker() as session:
        # Looking up company B's selector under company A's RLS context must
        # fail even though the ORM query filters only by the selector.
        assert await authenticate_token(session, foreign_token) is None

    async with admin_session_maker() as session:
        row = await session.get(AgentToken, first_row.id)
        assert row is not None
        row.revoked_at = datetime.now(UTC)
        await session.commit()
    async with runtime_session_maker() as session:
        assert await authenticate_token(session, first_secret) is None


@pytest.mark.integration
async def test_mcp_boundary_rechecks_bearer_for_each_http_request(
    token_owner: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, _other_company_id, user_id = token_owner
    row, secret = issue_token(
        AgentTokenCreate(name="Boundary", scopes=[AgentTokenScope.CUSTOMER_READ]), company_id
    )
    row.creator_id = user_id
    async with admin_session_maker() as session:
        session.add(row)
        await session.commit()

    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: runtime_session_maker)
    seen_principals: list[object] = []

    async def endpoint(scope: object, receive: object, send: object) -> None:
        del scope, receive
        seen_principals.append(mcp_module.agent_principal.get())
        await send({"type": "http.response.start", "status": 204, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    boundary = mcp_module.ClosedMCPBoundary(endpoint)  # type: ignore[arg-type]
    token_prefix, secret_part = secret.rsplit(".", 1)
    wrong_secret = f"{token_prefix}.{('A' if secret_part[0] != 'A' else 'B')}{secret_part[1:]}"

    async def request(bearer: str | None) -> int:
        headers = [(b"host", b"localhost")]
        if bearer is not None:
            headers.append((b"authorization", f"Bearer {bearer}".encode()))
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        await boundary(
            {"type": "http", "path": "/api/v1/mcp", "headers": headers},
            receive,  # type: ignore[arg-type]
            send,  # type: ignore[arg-type]
        )
        return int(messages[0]["status"])

    assert await request(secret) == 204
    assert seen_principals[-1] is not None
    assert await request(wrong_secret) == 401
    assert await request(None) == 401
    async with admin_session_maker() as session:
        saved = await session.get(AgentToken, row.id)
        assert saved is not None
        saved.revoked_at = datetime.now(UTC)
        await session.commit()
    assert await request(secret) == 401
