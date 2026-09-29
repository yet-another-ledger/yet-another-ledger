"""Isolated PostgreSQL checks for M14 Step 3 stale extraction protection."""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.db import set_rls_company
from jai.models.dictionary import ExpenseCategory
from jai.models.vat import VatRate
from jai.schemas.expense import ExpenseAIPrefill
from jai.services import ai as ai_service
from jai.services.receipt_inbox import append_files, create_item, extract_item, get_item
from jai.services.storage import Storage

pytestmark = pytest.mark.integration


class _MemoryStorage(Storage):
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        del content_type
        stored = f"{namespace}/{key}"
        self.data[stored] = content
        return stored

    def open(self, key: str) -> bytes:
        return self.data[key]

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.data


@pytest.mark.asyncio
async def test_old_group_result_cannot_replace_new_file_revision(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text

    company_id, creator_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as setup:
        await setup.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 Step 3', 'EUR')"),
            {"id": company_id},
        )
        await setup.execute(
            text(
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:id, :email, 'test-hash', :company_id)"
            ),
            {"id": creator_id, "email": f"{creator_id}@example.test", "company_id": company_id},
        )
        await setup.commit()

    async with db_session_maker() as setup:
        setup.add(ExpenseCategory(company_id=company_id, name="Office", active=False))
        setup.add(
            VatRate(company_id=company_id, label="Standard", percent=Decimal("21"), active=True)
        )
        setup.add(
            VatRate(
                company_id=company_id,
                label="Inactive reduced",
                percent=Decimal("19"),
                active=False,
            )
        )
        await setup.commit()

    storage = _MemoryStorage()
    png = b"\x89PNG\r\n\x1a\nreceipt"
    async with db_session_maker() as session:
        item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "one.png")], 1024, storage
        )

    old_started = asyncio.Event()
    release_old = asyncio.Event()
    calls = 0

    async def fake_extract(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        nonlocal calls
        calls += 1
        if calls == 1:
            old_started.set()
            await release_old.wait()
            return ExpenseAIPrefill(summary="stale")
        return ExpenseAIPrefill(summary="current")

    monkeypatch.setattr(ai_service, "extract_from_files", fake_extract)

    async def run_old() -> None:
        async with db_session_maker() as session:
            await extract_item(session, item.id, company_id, storage)

    old_task = asyncio.create_task(run_old())
    await old_started.wait()

    async with db_session_maker() as session:
        await append_files(
            session, item.id, company_id, creator_id,
            [(png, "image/png", "two.png")], 1024, storage,
        )
        await extract_item(session, item.id, company_id, storage)

    release_old.set()
    await old_task

    async with db_session_maker() as verify:
        current = await get_item(verify, item.id, company_id)
        assert current.ai_suggestion is not None
        assert current.ai_suggestion.summary == "current"
        assert current.ai_state.value == "READY"
        assert len(current.files) == 2

    async def unavailable(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        raise ai_service.AIUnavailableError("AI_NOT_CONFIGURED")

    monkeypatch.setattr(ai_service, "extract_from_files", unavailable)
    async with db_session_maker() as session:
        another = await create_item(
            session, company_id, creator_id, [(png, "image/png", "offline.png")], 1024, storage
        )
        saved = await extract_item(session, another.id, company_id, storage)
        assert saved.ai_state.value == "UNAVAILABLE"
        assert saved.ai_error_code is not None
        assert saved.ai_error_code.value == "AI_NOT_CONFIGURED"
        assert saved.ai_suggestion is None

    async def empty_prefill(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        return ExpenseAIPrefill(confidence="low", raw_model_note="unusable")

    monkeypatch.setattr(ai_service, "extract_from_files", empty_prefill)
    async with db_session_maker() as session:
        empty_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "empty.json.png")], 1024, storage
        )
        empty_result = await extract_item(session, empty_item.id, company_id, storage)
        assert empty_result.ai_state.value == "FAILED"
        assert empty_result.ai_error_code is not None
        assert empty_result.ai_error_code.value == "AI_INVALID_RESPONSE"
        assert empty_result.ai_suggestion is None

    async def check_unusable(prefill: ExpenseAIPrefill, filename: str) -> None:
        async def response(*args: object, **kwargs: object) -> ExpenseAIPrefill:
            del args, kwargs
            return prefill

        monkeypatch.setattr(ai_service, "extract_from_files", response)
        async with db_session_maker() as session:
            candidate = await create_item(
                session, company_id, creator_id, [(png, "image/png", filename)], 1024, storage
            )
            result = await extract_item(session, candidate.id, company_id, storage)
            assert result.ai_state.value == "FAILED"
            assert result.ai_error_code is not None
            assert result.ai_error_code.value == "AI_INVALID_RESPONSE"
            assert result.ai_suggestion is None

    await check_unusable(ExpenseAIPrefill(suggested_category_name="Unknown"), "unknown.png")
    await check_unusable(ExpenseAIPrefill(supplier_name="  "), "whitespace.png")
    await check_unusable(ExpenseAIPrefill(vat_rate_percent=Decimal("17")), "unknown-rate.png")

    async def usable_prefill(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        return ExpenseAIPrefill(suggested_category_name="office")

    monkeypatch.setattr(ai_service, "extract_from_files", usable_prefill)
    async with db_session_maker() as session:
        valid_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "known.png")], 1024, storage
        )
        valid_result = await extract_item(session, valid_item.id, company_id, storage)
        assert valid_result.ai_state.value == "READY"
        assert valid_result.ai_suggestion is not None

    async def inactive_rate_prefill(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        return ExpenseAIPrefill(vat_rate_percent=Decimal("19"))

    monkeypatch.setattr(ai_service, "extract_from_files", inactive_rate_prefill)
    async with db_session_maker() as session:
        inactive_rate_item = await create_item(
            session,
            company_id,
            creator_id,
            [(png, "image/png", "inactive-rate.png")],
            1024,
            storage,
        )
        inactive_rate_result = await extract_item(
            session, inactive_rate_item.id, company_id, storage
        )
        assert inactive_rate_result.ai_state.value == "READY"
        assert inactive_rate_result.ai_suggestion is not None
        assert inactive_rate_result.ai_suggestion.vat_rate_percent == Decimal("19")

    async def provider_error(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        raise ai_service.AIExtractionError("AI_TIMEOUT")

    monkeypatch.setattr(ai_service, "extract_from_files", provider_error)
    async with db_session_maker() as session:
        failed = await extract_item(session, another.id, company_id, storage)
        assert failed.ai_state.value == "FAILED"
        assert failed.ai_error_code is not None
        assert failed.ai_error_code.value == "AI_TIMEOUT"
        assert failed.ai_suggestion is None
        assert "AI_TIMEOUT" in str(failed.ai_error_code)

    confirmed = asyncio.Event()
    release_confirmed = asyncio.Event()

    async def finishing(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        confirmed.set()
        await release_confirmed.wait()
        return ExpenseAIPrefill(summary="must not be saved")

    monkeypatch.setattr(ai_service, "extract_from_files", finishing)
    async with db_session_maker() as session:
        final_item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "confirmed.png")], 1024, storage
        )

    async def in_flight() -> None:
        async with db_session_maker() as session:
            await extract_item(session, final_item.id, company_id, storage)

    extraction = asyncio.create_task(in_flight())
    await confirmed.wait()
    async with db_session_maker() as session:
        await set_rls_company(session, company_id)
        await session.execute(
            text("UPDATE receipt_inbox SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": final_item.id},
        )
        await session.commit()
    release_confirmed.set()
    await extraction

    async with db_session_maker() as verify:
        completed = await get_item(verify, final_item.id, company_id)
        assert completed.status.value == "CONFIRMED"
        assert completed.ai_suggestion is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["storage", "provider"])
async def test_extraction_failure_log_excludes_private_data(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_source: str,
) -> None:
    company_id, creator_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as setup:
        await setup.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 log', 'EUR')"),
            {"id": company_id},
        )
        await setup.execute(
            text(
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:id, :email, 'test-hash', :company_id)"
            ),
            {"id": creator_id, "email": f"{creator_id}@example.test", "company_id": company_id},
        )
        await setup.commit()

    storage = _MemoryStorage()
    async with db_session_maker() as session:
        item = await create_item(
            session, company_id, creator_id,
            [(b"\x89PNG\r\n\x1a\nsecret receipt bytes", "image/png", "receipt.png")],
            1024, storage,
        )
    storage_key = next(iter(storage.data))
    private_text = (
        f"{storage_key} /private/receipt.png provider-response-secret "
        "secret receipt bytes"
    )

    if failure_source == "storage":
        def missing_file(key: str) -> bytes:
            raise FileNotFoundError(f"Storage key not found: {key}; {private_text}")

        monkeypatch.setattr(storage, "open", missing_file)
    else:
        async def broken_provider(*args: object, **kwargs: object) -> ExpenseAIPrefill:
            del args, kwargs
            raise RuntimeError(private_text)

        monkeypatch.setattr(ai_service, "extract_from_files", broken_provider)

    with caplog.at_level(logging.WARNING, logger="jai.services.receipt_inbox"):
        async with db_session_maker() as session:
            failed = await extract_item(session, item.id, company_id, storage)

    assert failed.ai_state.value == "FAILED"
    assert failed.ai_error_code is not None
    assert failed.ai_error_code.value == "AI_PROVIDER_ERROR"
    assert failed.ai_suggestion is None
    records = [
        record for record in caplog.records
        if record.name == "jai.services.receipt_inbox"
    ]
    assert len(records) == 1
    assert records[0].getMessage() == "Receipt inbox AI extraction failed: AI_PROVIDER_ERROR"
    assert records[0].exc_info is None
    for secret in (
        storage_key,
        "/private/receipt.png",
        "provider-response-secret",
        "secret receipt bytes",
    ):
        assert secret not in caplog.text
