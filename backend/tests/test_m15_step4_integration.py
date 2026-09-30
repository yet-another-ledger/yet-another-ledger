"""PostgreSQL and real Streamable HTTP checks for M15 receipt tools."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Receive, Scope, Send

import jai.api.mcp as mcp_module
import jai.services.agent_receipts as agent_receipts
import jai.services.ai as ai_service
import jai.services.receipt_inbox as receipt_inbox_service
from jai.config import Settings
from jai.db import set_rls_company
from jai.main import app
from jai.models.agent_operation import AgentOperation
from jai.models.agent_token import AgentToken
from jai.models.company import Company
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.receipt_inbox import ReceiptInbox
from jai.models.user import User
from jai.schemas.agent_token import AgentTokenCreate, AgentTokenScope
from jai.schemas.agent_tools import AgentSource, ReceiptFileInput, SubmitReceiptInput
from jai.services.agent_operations import run_agent_mutation
from jai.services.agent_tokens import AgentPrincipal, issue_token
from jai.services.storage import LocalStorage


@pytest.fixture
async def receipt_tokens(
    admin_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[
    tuple[uuid.UUID, uuid.UUID, str, str, str, AgentToken, AgentToken, AgentToken]
]:
    company_id, other_company_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scopes = [AgentTokenScope.RECEIPT_INTAKE]
    token1, secret1 = issue_token(AgentTokenCreate(name="Receipt one", scopes=scopes), company_id)
    token1.creator_id = user_id
    token2, secret2 = issue_token(
        AgentTokenCreate(name="Receipt two", scopes=scopes), other_company_id
    )
    token2.creator_id = user_id
    token3, secret3 = issue_token(
        AgentTokenCreate(
            name="Customer reader", scopes=[AgentTokenScope.CUSTOMER_READ]
        ),
        company_id,
    )
    token3.creator_id = user_id
    async with admin_session_maker() as session:
        session.add_all(
            [
                Company(id=company_id, name="Receipt company", base_currency="EUR"),
                Company(id=other_company_id, name="Other receipt company", base_currency="EUR"),
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
        session.add_all([token1, token2, token3])
        await session.commit()
    yield company_id, other_company_id, secret1, secret2, secret3, token1, token2, token3


def _wire_result(response: Response) -> Any:
    payload = json.loads(response.text.split("data: ", 1)[1])
    assert "error" not in payload, payload
    return payload["result"]


async def _initialize(client: AsyncClient, secret: str) -> str:
    headers = {
        "authorization": f"Bearer {secret}",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    response = await client.post(
        "/api/v1/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "step4-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.headers["mcp-session-id"]


async def _call(
    client: AsyncClient,
    session_id: str,
    secret: str,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, str]:
    response = await client.post(
        "/api/v1/mcp",
        headers={
            "authorization": f"Bearer {secret}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-session-id": session_id,
            "mcp-protocol-version": "2025-11-25",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    if response.status_code != 200:
        return response.status_code, None, response.text
    return response.status_code, _wire_result(response), response.text


@pytest.mark.integration
async def test_mcp_receipt_submit_replay_status_and_per_request_bearer(
    receipt_tokens: tuple[
        uuid.UUID, uuid.UUID, str, str, str, AgentToken, AgentToken, AgentToken
    ],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        company_id,
        other_company_id,
        secret1,
        secret2,
        secret3,
        token1,
        token2,
        token3,
    ) = receipt_tokens
    storage = LocalStorage(str(tmp_path / "storage"))
    monkeypatch.setattr(mcp_module, "get_storage", lambda: storage)
    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: runtime_session_maker)
    test_settings = Settings(
        max_receipt_bytes=1024,
        base_url="https://jai.example.test",
        storage_root=str(tmp_path / "storage"),
        scheduler_enabled=False,
    )
    monkeypatch.setattr(mcp_module, "get_settings", lambda: test_settings)
    monkeypatch.setattr(agent_receipts, "get_settings", lambda: test_settings)
    original_run_agent_mutation = run_agent_mutation
    ai_error = "AI_DISABLED"
    ai_calls: list[tuple[uuid.UUID, int]] = []

    async def disabled(
        _session: AsyncSession,
        company: uuid.UUID,
        files: list[tuple[str, bytes]],
        **_kwargs: Any,
    ) -> None:
        ai_calls.append((company, len(files)))
        if ai_error == "AI_DISABLED":
            raise ai_service.AIUnavailableError(ai_error)
        raise ai_service.AIExtractionError(ai_error)

    monkeypatch.setattr(ai_service, "extract_from_files", disabled)
    receipt_files = [
        ("../../bill.pdf", "application/pdf", b"%PDF-1.7\nreceipt bytes\n"),
        ("scan.png", "image/png", b"\x89PNG\r\n\x1a\nPNG bytes"),
        ("phone.jpg", "image/jpeg", b"\xff\xd8\xff\xe0JPEG bytes"),
        ("camera.webp", "image/webp", b"RIFF\x0c\x00\x00\x00WEBPwebp bytes"),
        ("extra.pdf", "application/pdf", b"%PDF-1.7\nsecond page\n"),
    ]
    encoded_files = [
        {
            "filename": filename,
            "mime_type": mime_type,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
        for filename, mime_type, data in receipt_files
    ]
    request_id = str(uuid.uuid4())
    arguments = {
        "request_id": request_id,
        "source": {"platform": "telegram", "chat_id": "12", "message_id": "34"},
        "language": "en",
        "files": encoded_files,
    }

    async with mcp_module.server.session_manager.run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            session_id = await _initialize(client, secret1)
            listed = await client.post(
                "/api/v1/mcp",
                headers={
                    "authorization": f"Bearer {secret1}",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "mcp-session-id": session_id,
                    "mcp-protocol-version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            listed_result = _wire_result(listed)
            assert {tool["name"] for tool in listed_result["tools"]} == {
                "submit_receipt",
                "get_receipt_status",
            }

            bad_envelope_secret = "SENSITIVE_BASE64_ENVELOPE_AQIDBAUG"
            invalid_envelope = await client.post(
                "/api/v1/mcp",
                headers={
                    "authorization": f"Bearer {secret1}",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "mcp-session-id": session_id,
                    "mcp-protocol-version": "2025-11-25",
                },
                json={
                    "jsonrpc": bad_envelope_secret,
                    "id": 99,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert invalid_envelope.status_code == 400
            assert bad_envelope_secret not in invalid_envelope.text
            assert bad_envelope_secret not in caplog.text

            sensitive_value = "SENSITIVE_RECEIPT_PAYLOAD_6d7f1a"
            status, bad_result, bad_wire = await _call(
                client,
                session_id,
                secret1,
                20,
                "submit_receipt",
                {
                    "request_id": str(uuid.uuid4()),
                    "source": {"platform": "telegram"},
                    "files": sensitive_value,
                },
            )
            assert status == 200 and bad_result is not None
            assert bad_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in bad_wire
            assert sensitive_value not in bad_wire
            assert sensitive_value not in caplog.text

            status, extra_result, extra_wire = await _call(
                client,
                session_id,
                secret1,
                22,
                "submit_receipt",
                {
                    "request_id": str(uuid.uuid4()),
                    "source": {"platform": "telegram"},
                    "files": encoded_files[:1],
                    "confirm_expense": True,
                },
            )
            assert status == 200 and extra_result is not None
            assert extra_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in extra_wire

            status, extra_status_result, extra_status_wire = await _call(
                client,
                session_id,
                secret1,
                27,
                "get_receipt_status",
                {"inbox_id": str(uuid.uuid4()), "confirm_expense": True},
            )
            assert status == 200 and extra_status_result is not None
            assert extra_status_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in extra_status_wire

            mismatched_data = base64.b64encode(b"\x89PNG\r\n\x1a\nnot a PDF").decode("ascii")
            status, spoofed_result, spoofed_wire = await _call(
                client,
                session_id,
                secret1,
                21,
                "submit_receipt",
                {
                    "request_id": str(uuid.uuid4()),
                    "source": {"platform": "telegram"},
                    "files": [
                        {
                            "filename": "receipt.pdf",
                            "mime_type": "application/pdf",
                            "data_base64": mismatched_data,
                        }
                    ],
                },
            )
            assert status == 200 and spoofed_result is not None
            assert spoofed_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in spoofed_wire
            assert mismatched_data not in spoofed_wire
            assert mismatched_data not in caplog.text

            status, result, _wire = await _call(
                client, session_id, secret1, 3, "submit_receipt", arguments
            )
            assert status == 200
            assert result is not None
            stable = result.get("structuredContent") or json.loads(
                result["content"][0]["text"]
            )
            assert stable["ai_state"] == "NOT_RUN"
            assert stable["ai_error_code"] is None
            assert stable["status"] == "created"
            assert stable["review_url"] == (
                f"https://jai.example.test/receipt-inbox/{stable['inbox_id']}"
            )
            inbox_id = stable["inbox_id"]

            # Status is a separate read after the durable operation and AI call.
            status, status_result, _wire = await _call(
                client,
                session_id,
                secret1,
                4,
                "get_receipt_status",
                {"inbox_id": inbox_id},
            )
            assert status == 200 and status_result is not None
            current = status_result.get("structuredContent") or json.loads(
                status_result["content"][0]["text"]
            )
            expected_state = "UNAVAILABLE" if ai_error == "AI_DISABLED" else "FAILED"
            assert current["ai_state"] == expected_state
            assert current["ai_error_code"] == ai_error

            # Same operation returns the original snapshot and does not repeat
            # intake/AI; the raw file content is only represented by the digest.
            status, replay_result, _wire = await _call(
                client, session_id, secret1, 5, "submit_receipt", arguments
            )
            assert status == 200 and replay_result is not None
            replay = replay_result.get("structuredContent") or json.loads(
                replay_result["content"][0]["text"]
            )
            assert replay["status"] == "replayed"
            assert replay["ai_state"] == "NOT_RUN"
            assert replay["inbox_id"] == inbox_id

            changed_arguments = {
                **arguments,
                "files": [{**encoded_files[0], "filename": "different-name.pdf"}],
            }
            status, conflict_result, conflict_wire = await _call(
                client, session_id, secret1, 23, "submit_receipt", changed_arguments
            )
            assert status == 200 and conflict_result is not None
            assert conflict_result["isError"] is True
            assert "MCP_REQUEST_ID_CONFLICT" in conflict_wire

            stored_files_before_failures = {
                path
                for path in (tmp_path / "storage" / "receipts").rglob("*")
                if path.is_file()
            }
            failing_payload = b"%PDF-1.7\nfailed storage write\n"
            failing_arguments = {
                "request_id": str(uuid.uuid4()),
                "source": {"platform": "telegram"},
                "files": [
                    {
                        "filename": "failure.pdf",
                        "mime_type": "application/pdf",
                        "data_base64": base64.b64encode(failing_payload).decode("ascii"),
                    }
                ],
            }
            sensitive_storage_error = "SENSITIVE_STORAGE_EXCEPTION_VALUE"
            original_save = storage.save

            def failing_save(*_args: Any, **_kwargs: Any) -> str:
                raise OSError(sensitive_storage_error)

            monkeypatch.setattr(storage, "save", failing_save)
            status, storage_result, storage_wire = await _call(
                client, session_id, secret1, 25, "submit_receipt", failing_arguments
            )
            monkeypatch.setattr(storage, "save", original_save)
            assert status == 200 and storage_result is not None
            assert storage_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in storage_wire
            assert sensitive_storage_error not in storage_wire
            assert sensitive_storage_error not in caplog.text

            async def fail_transaction_after_intake(
                session: AsyncSession,
                principal: AgentPrincipal,
                **kwargs: Any,
            ) -> Any:
                async with session.begin():
                    await set_rls_company(session, principal.company_id)
                    async with session.begin_nested():
                        await kwargs["mutate"](session)
                    raise RuntimeError("simulated database commit failure")

            monkeypatch.setattr(
                agent_receipts, "run_agent_mutation", fail_transaction_after_intake
            )
            status, database_result, database_wire = await _call(
                client,
                session_id,
                secret1,
                26,
                "submit_receipt",
                failing_arguments,
            )
            monkeypatch.setattr(
                agent_receipts, "run_agent_mutation", original_run_agent_mutation
            )
            assert status == 200 and database_result is not None
            assert database_result["isError"] is True
            assert "MCP_VALIDATION_FAILED" in database_wire
            assert {
                path
                for path in (tmp_path / "storage" / "receipts").rglob("*")
                if path.is_file()
            } == stored_files_before_failures
            assert not any(
                failing_payload == path.read_bytes()
                for path in (tmp_path / "storage" / "receipts").rglob("*")
                if path.is_file()
            )

            concurrent_results = await asyncio.gather(
                _call(client, session_id, secret1, 8, "submit_receipt", arguments),
                _call(client, session_id, secret1, 9, "submit_receipt", arguments),
            )
            for concurrent_status, concurrent_result, _ in concurrent_results:
                assert concurrent_status == 200 and concurrent_result is not None
                concurrent_snapshot = concurrent_result.get("structuredContent") or json.loads(
                    concurrent_result["content"][0]["text"]
                )
                assert concurrent_snapshot["status"] == "replayed"
                assert concurrent_snapshot["inbox_id"] == inbox_id

            # Make two first deliveries overlap before either can claim the
            # idempotency key. Each HTTP request has its own MCP session and
            # the application opens a separate runtime database session.
            concurrent_request_id = uuid.UUID("f2e47153-1698-4ab7-b9fd-cf1378f641db")
            concurrent_files = [
                ("parallel-a.pdf", "application/pdf", b"%PDF-1.7\nparallel A\n"),
                ("parallel-b.png", "image/png", b"\x89PNG\r\n\x1a\nparallel B"),
            ]
            concurrent_arguments = {
                "request_id": str(concurrent_request_id),
                "source": {
                    "platform": "telegram",
                    "chat_id": "parallel-chat",
                    "message_id": "parallel-message",
                },
                "language": "en",
                "files": [
                    {
                        "filename": filename,
                        "mime_type": mime_type,
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }
                    for filename, mime_type, data in concurrent_files
                ],
            }
            arrival_count = 0
            both_arrived = asyncio.Event()

            async def wait_for_both_first_deliveries(*args: Any, **kwargs: Any) -> Any:
                nonlocal arrival_count
                arrival_count += 1
                if arrival_count == 2:
                    both_arrived.set()
                await asyncio.wait_for(both_arrived.wait(), timeout=5)
                return await original_run_agent_mutation(*args, **kwargs)

            stored_before_concurrent = {
                path
                for path in (tmp_path / "storage" / "receipts").rglob("*")
                if path.is_file()
            }
            ai_calls_before_concurrent = len(ai_calls)
            monkeypatch.setattr(
                agent_receipts, "run_agent_mutation", wait_for_both_first_deliveries
            )
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://localhost"
            ) as second_client:
                second_session_id = await _initialize(second_client, secret1)
                first_delivery, second_delivery = await asyncio.gather(
                    _call(
                        client,
                        session_id,
                        secret1,
                        80,
                        "submit_receipt",
                        concurrent_arguments,
                    ),
                    _call(
                        second_client,
                        second_session_id,
                        secret1,
                        81,
                        "submit_receipt",
                        concurrent_arguments,
                    ),
                )
            monkeypatch.setattr(
                agent_receipts, "run_agent_mutation", original_run_agent_mutation
            )
            concurrent_outputs: list[dict[str, Any]] = []
            for delivery_status, delivery_result, delivery_wire in (
                first_delivery,
                second_delivery,
            ):
                assert delivery_status == 200, delivery_wire
                assert delivery_result is not None
                assert delivery_result["isError"] is False
                concurrent_outputs.append(
                    delivery_result.get("structuredContent")
                    or json.loads(delivery_result["content"][0]["text"])
                )
            assert {output["status"] for output in concurrent_outputs} == {
                "created",
                "replayed",
            }
            created_output = next(
                output for output in concurrent_outputs if output["status"] == "created"
            )
            replayed_output = next(
                output for output in concurrent_outputs if output["status"] == "replayed"
            )
            assert created_output["operation_id"] == replayed_output["operation_id"]
            assert created_output["target_id"] == replayed_output["target_id"]
            assert created_output["inbox_id"] == replayed_output["inbox_id"]
            snapshot_fields = (
                "inbox_id",
                "ai_state",
                "ai_error_code",
                "review_url",
            )
            assert {
                field: created_output[field] for field in snapshot_fields
            } == {field: replayed_output[field] for field in snapshot_fields}
            assert created_output["ai_state"] == "NOT_RUN"
            assert len(ai_calls) == ai_calls_before_concurrent + 1
            assert ai_calls[-1] == (company_id, len(concurrent_files))

            concurrent_inbox_id = uuid.UUID(created_output["inbox_id"])
            async with admin_session_maker() as session:
                concurrent_operations = list(
                    (
                        await session.scalars(
                            select(AgentOperation).where(
                                AgentOperation.token_id == token1.id,
                                AgentOperation.request_id == str(concurrent_request_id),
                            )
                        )
                    ).all()
                )
                assert len(concurrent_operations) == 1
                concurrent_inboxes = list(
                    (
                        await session.scalars(
                            select(ReceiptInbox).where(
                                ReceiptInbox.id == concurrent_inbox_id,
                                ReceiptInbox.company_id == company_id,
                            )
                        )
                    ).all()
                )
                assert len(concurrent_inboxes) == 1
                concurrent_attachments = list(
                    (
                        await session.scalars(
                            select(ExpenseAttachment).where(
                                ExpenseAttachment.receipt_inbox_id == concurrent_inbox_id
                            )
                        )
                    ).all()
                )
                assert len(concurrent_attachments) == len(concurrent_files)
                attachments_by_name = {
                    attachment.filename: attachment for attachment in concurrent_attachments
                }
                assert set(attachments_by_name) == {
                    filename for filename, _, _ in concurrent_files
                }
                for filename, mime_type, original in concurrent_files:
                    attachment = attachments_by_name[filename]
                    assert attachment.mime_type == mime_type
                    assert storage.open(attachment.storage_key) == original
                persisted_snapshot = concurrent_operations[0].result_snapshot
                assert persisted_snapshot == {
                    field: created_output[field] for field in snapshot_fields
                }
            stored_after_concurrent = {
                path
                for path in (tmp_path / "storage" / "receipts").rglob("*")
                if path.is_file()
            }
            new_stored_files = stored_after_concurrent - stored_before_concurrent
            assert len(new_stored_files) == len(concurrent_files)
            assert {path.read_bytes() for path in new_stored_files} == {
                original for _, _, original in concurrent_files
            }

            # AI failures happen after the durable submit snapshot. The status
            # tool reports each safe result while submit remains successful.
            for failure in ("AI_TIMEOUT", "AI_PROVIDER_ERROR"):
                ai_error = failure
                failure_payload = b"%PDF-1.7\nAI failure test\n"
                failure_args = {
                    "request_id": str(uuid.uuid4()),
                    "source": {"platform": "telegram"},
                    "files": [
                        {
                            "filename": "ai-failure.pdf",
                            "mime_type": "application/pdf",
                            "data_base64": base64.b64encode(failure_payload).decode("ascii"),
                        }
                    ],
                }
                failure_status, failure_result, _ = await _call(
                    client, session_id, secret1, 30, "submit_receipt", failure_args
                )
                assert failure_status == 200 and failure_result is not None
                submitted = failure_result.get("structuredContent") or json.loads(
                    failure_result["content"][0]["text"]
                )
                assert submitted["status"] == "created"
                assert submitted["ai_state"] == "NOT_RUN"
                failed_status, failed_result, _ = await _call(
                    client,
                    session_id,
                    secret1,
                    31,
                    "get_receipt_status",
                    {"inbox_id": submitted["inbox_id"]},
                )
                assert failed_status == 200 and failed_result is not None
                reported = failed_result.get("structuredContent") or json.loads(
                    failed_result["content"][0]["text"]
                )
                assert reported["ai_state"] == "FAILED"
                assert reported["ai_error_code"] == failure

            ai_error = "AI_DISABLED"

            # The MCP session ID is not an identity credential: every HTTP
            # request authenticates the supplied Bearer token afresh.
            status, foreign_result, foreign_wire = await _call(
                client,
                session_id,
                secret2,
                6,
                "get_receipt_status",
                {"inbox_id": inbox_id},
            )
            assert status == 200 and foreign_result is not None
            assert foreign_result["isError"] is True
            assert "MCP_TARGET_NOT_FOUND" in foreign_wire

            status, scope_result, scope_wire = await _call(
                client,
                session_id,
                secret3,
                24,
                "get_receipt_status",
                {"inbox_id": inbox_id},
            )
            assert status == 200 and scope_result is not None
            assert scope_result["isError"] is True
            assert "MCP_INSUFFICIENT_SCOPE" in scope_wire

            async with admin_session_maker() as session:
                row = await session.get(AgentToken, token1.id)
                assert row is not None
                row.revoked_at = datetime.now(UTC)
                await session.commit()
            rejected, _, _ = await _call(
                client, session_id, secret1, 7, "get_receipt_status", {"inbox_id": inbox_id}
            )
            assert rejected == 401

    async with admin_session_maker() as session:
        inbox = await session.get(ReceiptInbox, uuid.UUID(inbox_id))
        assert inbox is not None and inbox.company_id == company_id
        assert inbox.status == "PENDING" and inbox.expense_id is None
        attachments = list(
            (
                await session.scalars(
                    select(ExpenseAttachment)
                    .where(ExpenseAttachment.receipt_inbox_id == inbox.id)
                    .order_by(ExpenseAttachment.created_at, ExpenseAttachment.id)
                )
            ).all()
        )
        assert len(attachments) == len(receipt_files)
        assert all(attachment.expense_id is None for attachment in attachments)
        attachments_by_name = {attachment.filename: attachment for attachment in attachments}
        assert set(attachments_by_name) == {filename for filename, _, _ in receipt_files}
        for filename, mime_type, original in receipt_files:
            attachment = attachments_by_name[filename]
            assert attachment.filename == filename
            assert attachment.mime_type == mime_type
            assert ".." not in attachment.storage_key
            assert storage.open(attachment.storage_key) == original
        operation = await session.scalar(
            select(AgentOperation).where(
                AgentOperation.token_id == token1.id,
                AgentOperation.request_id == request_id,
            )
        )
        assert operation is not None
        snapshot_json = json.dumps(operation.result_snapshot)
        for _, _, original in receipt_files:
            assert original.decode("latin-1") not in snapshot_json
            assert base64.b64encode(original).decode("ascii") not in snapshot_json
            assert original.decode("latin-1") not in caplog.text
            assert base64.b64encode(original).decode("ascii") not in caplog.text
        assert operation.result_snapshot["ai_state"] == "NOT_RUN"


def test_receipt_base64_validation_is_canonical_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_receipts,
        "get_settings",
        lambda: Settings(max_receipt_bytes=8, scheduler_enabled=False),
    )
    valid = SubmitReceiptInput(
        request_id=uuid.uuid4(),
        source=AgentSource(platform="test"),
        files=[
            ReceiptFileInput(
                filename="receipt.pdf",
                mime_type="application/pdf",
                data_base64=base64.b64encode(b"%PDF-1\n").decode("ascii"),
            )
        ],
    )
    assert agent_receipts.decode_receipt_files(valid.files, 8)[0][0] == b"%PDF-1\n"
    for value in ("%%%", "YQ===", base64.b64encode(b"123456789").decode("ascii")):
        request = SubmitReceiptInput(
            request_id=uuid.uuid4(),
            source=AgentSource(platform="test"),
            files=[
                ReceiptFileInput(
                    filename="x.pdf", mime_type="application/pdf", data_base64=value
                )
            ],
        )
        with pytest.raises(Exception, match="MCP_VALIDATION_FAILED"):
            agent_receipts.decode_receipt_files(request.files, 8)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["query", "cancel"])
async def test_intake_read_failure_and_cancellation_compensate_files(
    receipt_tokens: tuple[
        uuid.UUID, uuid.UUID, str, str, str, AgentToken, AgentToken, AgentToken
    ],
    admin_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    company_id, _, _, _, _, token1, _, _ = receipt_tokens
    storage = LocalStorage(str(tmp_path / "storage"))
    test_settings = Settings(
        max_receipt_bytes=1024,
        base_url="https://jai.example.test",
        storage_root=str(tmp_path / "storage"),
        scheduler_enabled=False,
    )
    monkeypatch.setattr(agent_receipts, "get_settings", lambda: test_settings)
    payload = b"%PDF-1.7\nread failure payload\n"
    parts = [(payload, "application/pdf", "failure.pdf")]

    async def fail_file_read(*_args: Any, **_kwargs: Any) -> Any:
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("simulated inbox file read failure")

    _original_files = receipt_inbox_service._files
    monkeypatch.setattr(receipt_inbox_service, "_files", fail_file_read)
    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    async with runtime_session_maker() as session:
        with pytest.raises(expected):
            await receipt_inbox_service.create_item(
                session,
                company_id,
                token1.creator_id,
                parts,
                test_settings.max_receipt_bytes,
                storage,
            )
    assert not any(
        path.is_file() for path in (tmp_path / "storage" / "receipts").rglob("*")
    )

    request_id = uuid.uuid4()
    request = SubmitReceiptInput(
        request_id=request_id,
        source=AgentSource(platform="telegram"),
        files=[
            ReceiptFileInput(
                filename="failure.pdf",
                mime_type="application/pdf",
                data_base64=base64.b64encode(payload).decode("ascii"),
            )
        ],
    )
    principal = AgentPrincipal(
        token_id=token1.id,
        company_id=company_id,
        scopes=frozenset({AgentTokenScope.RECEIPT_INTAKE}),
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await agent_receipts.submit_receipt(
                runtime_session_maker, principal, request, storage
            )
    else:
        with pytest.raises(agent_receipts.AgentReceiptError) as raised:
            await agent_receipts.submit_receipt(
                runtime_session_maker, principal, request, storage
            )
        assert raised.value.code == "MCP_VALIDATION_FAILED"
    assert not any(
        path.is_file() for path in (tmp_path / "storage" / "receipts").rglob("*")
    )

    monkeypatch.setattr(receipt_inbox_service, "_files", _original_files)
    async with admin_session_maker() as session:
        inbox_count = await session.scalar(
            select(ReceiptInbox).where(ReceiptInbox.company_id == company_id)
        )
        assert inbox_count is None
        operation = await session.scalar(
            select(AgentOperation).where(
                AgentOperation.token_id == token1.id,
                AgentOperation.request_id == str(request_id),
            )
        )
        assert operation is None


@pytest.mark.parametrize("content_length", [None, b"1"])
async def test_mcp_boundary_limits_actual_body_before_sdk_parse(
    monkeypatch: pytest.MonkeyPatch,
    content_length: bytes | None,
) -> None:
    monkeypatch.setattr(
        mcp_module,
        "get_settings",
        lambda: Settings(max_receipt_bytes=8, scheduler_enabled=False),
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr(mcp_module, "get_session_maker", lambda: lambda: SessionContext())

    async def authenticated(_session: object, _token: str) -> Any:
        from jai.services.agent_tokens import AgentPrincipal

        return AgentPrincipal(uuid.uuid4(), uuid.uuid4(), frozenset())

    monkeypatch.setattr(mcp_module, "authenticate_token", authenticated)
    body = b"SENSITIVE_RAW_BODY" * 1200
    events = iter([{"type": "http.request", "body": body, "more_body": False}])
    sent: list[dict[str, Any]] = []
    endpoint_called = False

    async def receive() -> dict[str, Any]:
        return next(events)

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    async def endpoint(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal endpoint_called
        endpoint_called = True

    headers = [(b"host", b"localhost"), (b"authorization", b"Bearer test")]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = cast(
        Scope,
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mcp",
            "headers": headers,
        },
    )
    await mcp_module.ClosedMCPBoundary(endpoint)(scope, receive, send)

    assert not endpoint_called
    assert sent[0]["status"] == 413
    assert b"SENSITIVE_RAW_BODY" not in sent[1]["body"]
