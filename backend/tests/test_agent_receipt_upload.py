"""Multipart transport regressions without any database or live application."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import jai.api.agent_receipts as upload
import jai.api.mcp as boundary
from jai.config import Settings
from jai.main import create_app
from jai.schemas.agent_token import AgentTokenScope
from jai.schemas.agent_tools import SubmitReceiptOutput
from jai.services.agent_receipts import AgentReceiptError
from jai.services.agent_tokens import AgentPrincipal


@pytest.fixture
def upload_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = Settings(max_receipt_bytes=128, scheduler_enabled=False)
    monkeypatch.setattr(boundary, "get_settings", lambda: settings)
    monkeypatch.setattr(upload, "get_settings", lambda: settings)

    @asynccontextmanager
    async def session() -> Any:
        yield None

    monkeypatch.setattr(boundary, "get_session_maker", lambda: session)

    async def authenticate(_session: Any, secret: str) -> AgentPrincipal | None:
        if secret == "denied":
            return None
        scopes = (
            frozenset({AgentTokenScope.RECEIPT_INTAKE})
            if secret == "valid"
            else (frozenset({AgentTokenScope.CUSTOMER_READ}))
        )
        return AgentPrincipal(uuid.uuid4(), uuid.uuid4(), scopes)

    monkeypatch.setattr(boundary, "authenticate_token", authenticate)
    monkeypatch.setattr(upload, "get_session_maker", lambda: None)
    monkeypatch.setattr(upload, "get_storage", lambda: None)
    return create_app()


def metadata(**changes: Any) -> str:
    values = {
        "request_id": str(uuid.uuid4()),
        "source": {"platform": "telegram", "chat_id": "123", "message_id": "41"},
        "language": "zh",
    }
    values.update(changes)
    return json.dumps(values)


@pytest.mark.parametrize("token,status", [(None, 401), ("denied", 401), ("reader", 403)])
async def test_upload_rejects_before_reading_body(
    upload_app: Any,
    token: str | None,
    status: int,
) -> None:
    async def never_read() -> Any:
        raise AssertionError("Unauthorized upload body was read")
        yield b""  # pragma: no cover

    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            content=never_read(),
            headers={"authorization": f"Bearer {token}"} if token else {},
        )
    assert response.status_code == status


async def test_upload_preserves_original_bytes_and_metadata(
    upload_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[Any] = []
    inbox_id = uuid.uuid4()

    async def submit(
        _maker: Any, principal: AgentPrincipal, request: Any, parts: Any, _s: Any
    ) -> Any:
        received.append((principal, request, parts))
        return SubmitReceiptOutput(
            operation_id=uuid.uuid4(),
            status="created",
            target_id=inbox_id,
            inbox_id=inbox_id,
            ai_state="NOT_RUN",
            review_url=f"https://example.test/receipt-inbox/{inbox_id}",
        )

    monkeypatch.setattr(upload, "submit_receipt_parts", submit)
    original = b"%PDF-1.7\n\x00\xff\x01original bytes\n"
    raw_metadata = metadata()
    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            headers={"authorization": "Bearer valid"},
            data={"metadata": raw_metadata},
            files=[("files", ("test.pdf", original, "application/pdf"))],
        )
    assert response.status_code == 200, response.text
    assert received[0][1].model_dump(mode="json") == json.loads(raw_metadata)
    assert received[0][2] == [(original, "application/pdf", "test.pdf")]
    assert response.json()["inbox_id"] == str(inbox_id)


@pytest.mark.parametrize(
    "raw_metadata",
    ["private secret", metadata(language="private secret"), metadata(extra="secret")],
)
async def test_upload_validation_never_echoes_inputs(upload_app: Any, raw_metadata: str) -> None:
    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            headers={"authorization": "Bearer valid"},
            data={"metadata": raw_metadata},
            files=[("files", ("test.pdf", b"%PDF", "application/pdf"))],
        )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "MCP_VALIDATION_FAILED"}}


async def test_upload_framework_validation_is_sanitized(upload_app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            headers={"authorization": "Bearer valid"},
            data={"metadata": "secret", "files": "secret"},
        )
    assert response.status_code == 422
    assert "secret" not in response.text


@pytest.mark.parametrize("field", ["metadata", "confirm_expense"])
async def test_upload_rejects_duplicate_or_extra_multipart_fields(
    upload_app: Any,
    field: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            headers={"authorization": "Bearer valid"},
            files=[
                ("metadata", (None, metadata())),
                (field, (None, "secret")),
                ("files", ("test.pdf", b"%PDF", "application/pdf")),
            ],
        )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "MCP_VALIDATION_FAILED"}}


@pytest.mark.parametrize(
    "headers,status",
    [({"host": "evil.test"}, 421), ({"origin": "https://evil.test"}, 403)],
)
async def test_upload_keeps_the_private_host_origin_boundary(
    upload_app: Any,
    headers: dict[str, str],
    status: int,
) -> None:
    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            headers={**headers, "authorization": "Bearer valid"},
        )
    assert response.status_code == status


async def test_chunked_upload_body_is_bounded(upload_app: Any) -> None:
    async def oversized() -> Any:
        yield b'--bound\r\nContent-Disposition: form-data; name="files"; filename="a.pdf"\r\n\r\n'
        for _ in range(20):
            yield b"a" * 8192
        yield b"\r\n--bound--\r\n"

    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            content=oversized(),
            headers={
                "authorization": "Bearer valid",
                "content-type": "multipart/form-data; boundary=bound",
            },
        )
    assert response.status_code == 413
    assert "a" * 20 not in response.text


@pytest.mark.parametrize(
    "code,status", [("MCP_REQUEST_ID_CONFLICT", 409), ("MCP_UNAUTHENTICATED", 401)]
)
async def test_shared_service_errors_are_stable(
    upload_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
) -> None:
    async def fail(*_args: Any) -> Any:
        raise AgentReceiptError(code)

    monkeypatch.setattr(upload, "submit_receipt_parts", fail)
    async with AsyncClient(transport=ASGITransport(upload_app), base_url="http://127.0.0.1") as c:
        response = await c.post(
            "/api/v1/agent/receipt-inbox",
            headers={"authorization": "Bearer valid"},
            data={"metadata": metadata()},
            files=[("files", ("test.pdf", b"%PDF", "application/pdf"))],
        )
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code}}
