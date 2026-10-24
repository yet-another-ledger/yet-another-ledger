"""The local upload transport shares PostgreSQL intake/replay with remote MCP."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_m15_step4_integration import receipt_tokens  # noqa: F401

import jai.api.agent_receipts as upload
import jai.api.mcp as boundary
import jai.services.agent_receipts as receipts
from jai.config import Settings
from jai.main import create_app
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.receipt_inbox import ReceiptInbox
from jai.schemas.agent_token import AgentTokenScope
from jai.schemas.agent_tools import SubmitReceiptInput
from jai.services.agent_tokens import AgentPrincipal
from jai.services.storage import LocalStorage


@pytest.mark.integration
async def test_upload_originals_cross_transport_replay_and_revocation(
    receipt_tokens: tuple[uuid.UUID, uuid.UUID, str, str, str, AgentToken, AgentToken, AgentToken],  # noqa: F811
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    company, _, secret, _, reader_secret, token, _, _ = receipt_tokens
    storage = LocalStorage(str(tmp_path / "storage"))
    test_settings = Settings(
        max_receipt_bytes=1024,
        base_url="https://example.test",
        scheduler_enabled=False,
    )
    for module in (upload, boundary, receipts):
        monkeypatch.setattr(module, "get_settings", lambda: test_settings)
    monkeypatch.setattr(upload, "get_storage", lambda: storage)
    monkeypatch.setattr(upload, "get_session_maker", lambda: runtime_session_maker)
    monkeypatch.setattr(boundary, "get_session_maker", lambda: runtime_session_maker)
    ai_calls = 0

    async def no_ai(*_args: Any) -> None:
        nonlocal ai_calls
        ai_calls += 1

    monkeypatch.setattr(receipts, "extract_item", no_ai)
    data = [b"%PDF-1.7\noriginal PDF\x00\xff", b"\x89PNG\r\n\x1a\noriginal image\x00\xff"]
    files = [
        ("files", ("receipt.pdf", data[0], "application/pdf")),
        ("files", ("receipt.png", data[1], "image/png")),
    ]
    metadata = {
        "request_id": str(uuid.uuid4()),
        "language": "zh",
        "source": {"platform": "telegram", "chat_id": "123", "message_id": "41"},
    }
    async with AsyncClient(transport=ASGITransport(create_app()), base_url="http://127.0.0.1") as c:

        async def send(secret_value: str, upload_files: Any = files, fields: Any = metadata) -> Any:
            return await c.post(
                "/api/v1/agent/receipt-inbox",
                headers={"authorization": f"Bearer {secret_value}"},
                data={"metadata": json.dumps(fields)},
                files=upload_files,
            )

        first = await send(secret)
        assert first.status_code == 200, first.text
        created = first.json()
        replayed = await send(secret)
        assert replayed.status_code == 200
        expected_replay = {**created, "status": "replayed"}
        assert replayed.json() == expected_replay
        # The remote representation has precisely the same canonical digest.
        remote = SubmitReceiptInput.model_validate(
            {
                **metadata,
                "files": [
                    {
                        "filename": name,
                        "mime_type": mime,
                        "data_base64": base64.b64encode(raw).decode(),
                    }
                    for (_, (name, raw, mime)) in files
                ],
            }
        )
        principal = AgentPrincipal(token.id, company, frozenset({AgentTokenScope.RECEIPT_INTAKE}))
        cross_replay = await receipts.submit_receipt(
            runtime_session_maker, principal, remote, storage
        )
        assert cross_replay.model_dump(mode="json") == expected_replay
        assert ai_calls == 1

        changed = [files[0], ("files", ("receipt.png", data[1] + b"changed", "image/png"))]
        assert (await send(secret, changed)).status_code == 409
        assert (await send(reader_secret)).status_code == 403
        assert (
            await send(
                secret,
                fields={**metadata, "request_id": str(uuid.uuid4())},
                upload_files=[("files", ("bad.pdf", b"not a PDF", "application/pdf"))],
            )
        ).status_code == 422
        assert (
            await send(
                secret,
                fields={**metadata, "request_id": str(uuid.uuid4())},
                upload_files=[("files", ("large.pdf", b"%PDF" + b"x" * 1024, "application/pdf"))],
            )
        ).status_code == 422
        assert (await send(secret, upload_files=files * 3)).status_code == 422

        async with admin_session_maker() as session:
            assert await session.scalar(select(func.count()).select_from(ReceiptInbox)) == 1
            assert await session.scalar(select(func.count()).select_from(AgentOperation)) == 1
            attachments = (
                await session.scalars(
                    select(ExpenseAttachment).order_by(ExpenseAttachment.filename)
                )
            ).all()
            assert [storage.open(file.storage_key) for file in attachments] == data
            saved = await session.get(AgentToken, token.id)
            assert saved is not None
            saved.revoked_at = datetime.now(UTC)
            await session.commit()
        assert (await send(secret)).status_code == 401
    assert secret not in caplog.text and "original PDF" not in caplog.text
