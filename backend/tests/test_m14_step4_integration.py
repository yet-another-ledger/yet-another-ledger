"""Exactly-once receipt inbox confirmation on isolated PostgreSQL."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.models._enums import VatTreatmentEffect, VatTreatmentSide
from jai.models.dictionary import ExpenseCategory
from jai.models.expense import Expense
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.expense import ExpenseAIPrefill, ExpenseInput
from jai.services import ai as ai_service
from jai.services import receipt_inbox as inbox_service
from jai.services.expense import delete_expense, list_expenses
from jai.services.receipt_inbox import (
    InboxConflict,
    confirm_item,
    create_item,
    extract_item,
    get_item,
)
from jai.services.storage import Storage

pytestmark = pytest.mark.integration


class MemoryStorage(Storage):
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


async def seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, ExpenseInput]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    category_id, rate_id, treatment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 confirm', 'EUR')"),
        {"id": company_id},
    )
    await session.execute(
        text('INSERT INTO "user" (id, email, hashed_password, company_id) '
             'VALUES (:id, :email, \'test-hash\', :company_id)'),
        {"id": user_id, "email": f"{user_id}@example.test", "company_id": company_id},
    )
    session.add_all([
        ExpenseCategory(
            id=category_id, company_id=company_id, name="Supplies", default_deductible=True
        ),
        VatRate(id=rate_id, company_id=company_id, label="21%", percent=Decimal("21")),
        VatTreatment(
            id=treatment_id, company_id=company_id, code="NL_STANDARD", label="Standard",
            side=VatTreatmentSide.PURCHASE, effect=VatTreatmentEffect.APPLY_RATE,
        ),
    ])
    await session.commit()
    return company_id, user_id, ExpenseInput(
        expense_date=date(2026, 9, 10), category_id=category_id, supplier_name="Shop",
        vat_treatment_id=treatment_id, vat_rate_id=rate_id,
        net_amount=Decimal("10.005"), vat_amount=Decimal("2.105"),
    )


async def new_pending(session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID,
                      storage: MemoryStorage, files: list[bytes] | None = None):
    png = b"\x89PNG\r\n\x1a\nreceipt-original"
    return await create_item(
        session, company_id, user_id,
        [(data, "image/png", f"receipt-{i}.png") for i, data in enumerate(files or [png])],
        1024, storage,
    )


@pytest.mark.asyncio
async def test_confirmation_transfers_originals_once_and_delete_keeps_history(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_id, user_id, body = await _setup(db_session_maker)
    storage = MemoryStorage()
    originals = [b"\x89PNG\r\n\x1a\none", b"\x89PNG\r\n\x1a\ntwo"]
    async with db_session_maker() as session:
        pending = await new_pending(session, company_id, user_id, storage, originals)
        before = await list_expenses(session, company_id)
        assert before.total == 0
        file_hashes = {hashlib.sha256(v).hexdigest() for v in storage.data.values()}
        result = await confirm_item(session, pending.id, company_id, user_id, body)
        assert result.expense.kind.value == "PURCHASE"
        assert result.expense.is_draft is False
        assert result.expense.net_amount == Decimal("10.01")
        assert result.expense.vat_amount == Decimal("2.11")
        assert result.expense.gross_amount == Decimal("12.12")
        assert result.expense.attachment_count == 2
        assert result.inbox.status.value == "CONFIRMED"
        assert result.inbox.expense_id == result.expense.id
        after = await list_expenses(session, company_id)
        assert after.total == 1
        assert [row.id for row in after.items] == [result.expense.id]
        attachments = list((await session.execute(
            select(ExpenseAttachment).where(ExpenseAttachment.expense_id == result.expense.id)
        )).scalars())
        assert len(attachments) == 2
        assert all(att.receipt_inbox_id is None for att in attachments)
        transferred_hashes = {
            hashlib.sha256(storage.open(att.storage_key)).hexdigest() for att in attachments
        }
        assert transferred_hashes == file_hashes
        try:
            await confirm_item(session, pending.id, company_id, user_id, body)
            pytest.fail("repeat confirmation should conflict")
        except InboxConflict as exc:
            assert exc.code == "ALREADY_CONFIRMED"
            assert exc.expense_id == result.expense.id
        assert (await session.scalar(select(func.count()).select_from(Expense))) == 1
        await delete_expense(session, result.expense.id, company_id, storage)
        history = await get_item(session, pending.id, company_id)
        assert history.status.value == "CONFIRMED"
        assert history.expense_id is None
        assert history.confirmed_at is not None


@pytest.mark.asyncio
async def test_validation_and_transfer_failures_leave_pending_group_untouched(
    db_session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, user_id, body = await _setup(db_session_maker)
    storage = MemoryStorage()
    async with db_session_maker() as session:
        pending = await new_pending(session, company_id, user_id, storage)
        invalid = body.model_copy(update={"category_id": uuid.uuid4()})
        with pytest.raises(LookupError):
            await confirm_item(session, pending.id, company_id, user_id, invalid)
        invalid_rate = body.model_copy(update={"vat_rate_id": uuid.uuid4()})
        with pytest.raises(LookupError):
            await confirm_item(session, pending.id, company_id, user_id, invalid_rate)
        sales_treatment = VatTreatment(
            company_id=company_id,
            code="SALES_ONLY",
            label="Sales only",
            side=VatTreatmentSide.SALES,
            effect=VatTreatmentEffect.APPLY_RATE,
        )
        session.add(sales_treatment)
        await session.commit()
        invalid_side = body.model_copy(update={"vat_treatment_id": sales_treatment.id})
        with pytest.raises(ValueError):
            await confirm_item(session, pending.id, company_id, user_id, invalid_side)
        assert await session.scalar(select(func.count()).select_from(Expense)) == 0
        assert (await get_item(session, pending.id, company_id)).status.value == "PENDING"
        assert len(storage.data) == 1

        original_count = inbox_service._count_attachments
        async def fail_after_transfer(*args: object, **kwargs: object) -> int:
            del args, kwargs
            raise RuntimeError("simulated post-transfer failure")
        monkeypatch.setattr(inbox_service, "_count_attachments", fail_after_transfer)
        with pytest.raises(RuntimeError, match="simulated post-transfer"):
            await confirm_item(session, pending.id, company_id, user_id, body)
        monkeypatch.setattr(inbox_service, "_count_attachments", original_count)
        assert await session.scalar(select(func.count()).select_from(Expense)) == 0
        current = await get_item(session, pending.id, company_id)
        assert current.status.value == "PENDING"
        assert len(current.files) == 1
        attachment = (await session.execute(select(ExpenseAttachment))).scalar_one()
        assert attachment.receipt_inbox_id == pending.id and attachment.expense_id is None


@pytest.mark.asyncio
async def test_concurrent_confirmation_has_one_winner_and_reports_existing_expense(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_id, user_id, body = await _setup(db_session_maker)
    storage = MemoryStorage()
    async with db_session_maker() as session:
        pending = await new_pending(session, company_id, user_id, storage)

    async def attempt():
        async with db_session_maker() as session:
            try:
                return await confirm_item(session, pending.id, company_id, user_id, body)
            except InboxConflict as exc:
                return exc

    first, second = await asyncio.gather(attempt(), attempt())
    winners = [x for x in (first, second) if not isinstance(x, InboxConflict)]
    conflicts = [x for x in (first, second) if isinstance(x, InboxConflict)]
    assert len(winners) == len(conflicts) == 1
    assert conflicts[0].code == "ALREADY_CONFIRMED"
    assert conflicts[0].expense_id == winners[0].expense.id
    async with db_session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Expense)) == 1
        assert await session.scalar(select(func.count()).select_from(ExpenseAttachment)) == 1


@pytest.mark.asyncio
async def test_in_flight_ai_response_cannot_write_after_real_confirmation(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, user_id, body = await _setup(db_session_maker)
    storage = MemoryStorage()
    async with db_session_maker() as session:
        pending = await new_pending(session, company_id, user_id, storage)

    started, release = asyncio.Event(), asyncio.Event()

    async def slow_extract(*args: object, **kwargs: object) -> ExpenseAIPrefill:
        del args, kwargs
        started.set()
        await release.wait()
        return ExpenseAIPrefill(summary="late response")

    monkeypatch.setattr(ai_service, "extract_from_files", slow_extract)

    async def extraction() -> None:
        async with db_session_maker() as session:
            await extract_item(session, pending.id, company_id, storage)

    task = asyncio.create_task(extraction())
    await started.wait()
    async with db_session_maker() as session:
        confirmed = await confirm_item(session, pending.id, company_id, user_id, body)
    release.set()
    await task
    async with db_session_maker() as session:
        saved = await get_item(session, pending.id, company_id)
        assert confirmed.inbox.status.value == "CONFIRMED"
        assert saved.status.value == "CONFIRMED"
        assert saved.ai_suggestion is None


async def _setup(maker: async_sessionmaker[AsyncSession]):
    async with maker() as session:
        return await seed(session)
