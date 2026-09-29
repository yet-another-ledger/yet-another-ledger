"""PostgreSQL runtime-role checks for M14 Step 1 migration constraints."""

from __future__ import annotations

import asyncio
import uuid
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jai.api.receipt_inbox import append_receipt_inbox_files
from jai.db import set_rls_company
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.receipt_inbox import ReceiptInbox
from jai.models.user import User
from jai.services.ai import extract_from_attachment
from jai.services.expense import delete_attachment, get_attachment_content
from jai.services.receipt_inbox import (
    InboxConflict,
    append_files,
    create_item,
    discard_item,
    get_item,
    remove_file,
)
from jai.services.storage import ReceiptValidationError, Storage

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_receipt_inbox_rls_is_company_scoped_for_runtime_role(
    db_session_maker: async_sessionmaker[AsyncSession],
    runtime_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_a, company_b = uuid.uuid4(), uuid.uuid4()
    inbox_a = uuid.uuid4()
    async with db_session_maker() as admin:
        await admin.execute(
            text(
                "INSERT INTO company (id, name, base_currency) "
                "VALUES (:a, 'M14 RLS A', 'EUR'), (:b, 'M14 RLS B', 'EUR')"
            ),
            {"a": company_a, "b": company_b},
        )
        await admin.commit()

    async with runtime_session_maker() as runtime:
        assert await runtime.scalar(text("SELECT count(*) FROM receipt_inbox")) == 0
        await set_rls_company(runtime, company_a)
        await runtime.execute(
            text("INSERT INTO receipt_inbox (id, company_id) VALUES (:id, :company_id)"),
            {"id": inbox_a, "company_id": company_a},
        )
        await runtime.commit()
        await set_rls_company(runtime, company_b)
        assert await runtime.scalar(text("SELECT count(*) FROM receipt_inbox")) == 0
        with pytest.raises(DBAPIError):
            await runtime.execute(
                text("INSERT INTO receipt_inbox (company_id) VALUES (:company_id)"),
                {"company_id": company_a},
            )
        await runtime.rollback()

        await set_rls_company(runtime, company_a)
        assert await runtime.scalar(text("SELECT count(*) FROM receipt_inbox")) == 1
        await runtime.commit()


class _MemoryStorage(Storage):
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        del content_type
        stored_key = f"{namespace}/{key}"
        self.data[stored_key] = content
        return stored_key

    def open(self, key: str) -> bytes:
        return self.data[key]

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.data


async def _seed_owner(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    company_id, creator_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 Intake', 'EUR')"),
        {"id": company_id},
    )
    await session.execute(
        text(
            'INSERT INTO "user" (id, email, hashed_password, company_id) '
            "VALUES (:id, :email, 'test-hash', :company_id)"
        ),
        {"id": creator_id, "email": f"{creator_id}@example.test", "company_id": company_id},
    )
    await session.commit()
    return company_id, creator_id


async def _counts(session: AsyncSession) -> tuple[int, int]:
    return (
        await session.scalar(select(func.count()).select_from(ReceiptInbox)) or 0,
        await session.scalar(select(func.count()).select_from(ExpenseAttachment)) or 0,
    )


class _PartialWriteStorage(_MemoryStorage):
    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0

    def save(self, namespace: str, key: str, content: bytes, content_type: str) -> str:
        self.calls += 1
        if self.calls == self.fail_on:
            self.data[f"{namespace}/{key}"] = content[:4]
            raise OSError("simulated disk full")
        return super().save(namespace, key, content, content_type)


@pytest.mark.asyncio
async def test_receipt_intake_file_lifecycle_and_discard(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    company_id, creator_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_maker() as session:
        await session.execute(
            text("INSERT INTO company (id, name, base_currency) VALUES (:id, 'M14 Intake', 'EUR')"),
            {"id": company_id},
        )
        await session.execute(
            text(
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:id, :email, 'test-hash', :company_id)"
            ),
            {"id": creator_id, "email": f"{creator_id}@example.test", "company_id": company_id},
        )
        await session.commit()
        storage = _MemoryStorage()
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        pdf = b"%PDF-1.4 payload"
        item = await create_item(
            session,
            company_id,
            creator_id,
            [(png, "image/png", "receipt.png")],
            1024,
            storage,
        )
        assert len(item.files) == 1
        assert item.ai_state == "NOT_RUN"
        assert len(storage.data) == 1

        item = await append_files(
            session,
            item.id,
            company_id,
            creator_id,
            [(pdf, "application/pdf", "scan.pdf")],
            1024,
            storage,
        )
        assert len(item.files) == 2
        assert item.ai_state == "NOT_RUN"

        remaining_file_id = item.files[0].id
        item = await remove_file(session, item.id, item.files[1].id, company_id, storage)
        assert len(item.files) == 1
        assert len(storage.data) == 1
        with pytest.raises(InboxConflict, match="LAST_FILE"):
            await remove_file(session, item.id, remaining_file_id, company_id, storage)

        with pytest.raises(LookupError):
            await get_item(session, item.id, uuid.uuid4())

        await discard_item(session, item.id, company_id, storage)
        assert not storage.data
        with pytest.raises(LookupError):
            await get_item(session, item.id, company_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", [1, 2])
async def test_partial_storage_failure_removes_current_and_prior_files(
    db_session_maker: async_sessionmaker[AsyncSession], fail_on: int
) -> None:
    async with db_session_maker() as session:
        company_id, creator_id = await _seed_owner(session)
        storage = _PartialWriteStorage(fail_on)
        png = b"\x89PNG\r\n\x1a\nreceipt"
        with pytest.raises(OSError, match="disk full"):
            await create_item(
                session,
                company_id,
                creator_id,
                [(png, "image/png", "one.png"), (png, "image/png", "two.png")],
                1024,
                storage,
            )
        assert storage.data == {}
        assert await _counts(session) == (0, 0)

        item = await create_item(
            session, company_id, creator_id, [(png, "image/png", "kept.png")], 1024,
            _MemoryStorage(),
        )
        append_storage = _PartialWriteStorage(1)
        with pytest.raises(OSError, match="disk full"):
            await append_files(
                session, item.id, company_id, creator_id,
                [(png, "image/png", "failed.png")], 1024, append_storage,
            )
        assert append_storage.data == {}
        assert len((await get_item(session, item.id, company_id)).files) == 1
        assert await _counts(session) == (1, 1)


@pytest.mark.asyncio
async def test_commit_failure_and_invalid_second_file_leave_no_batch_metadata(
    db_session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_maker() as session:
        company_id, creator_id = await _seed_owner(session)
        storage = _MemoryStorage()
        png = b"\x89PNG\r\n\x1a\nreceipt"
        with pytest.raises(ReceiptValidationError):
            await create_item(
                session, company_id, creator_id,
                [(png, "image/png", "valid.png"), (b"bad", "image/png", "bad.png")],
                1024, storage,
            )
        assert storage.data == {}
        assert await _counts(session) == (0, 0)

        real_commit = session.commit
        monkeypatch.setattr(session, "commit", AsyncMock(side_effect=OSError("commit failed")))
        with pytest.raises(OSError, match="commit failed"):
            await create_item(
                session, company_id, creator_id,
                [(png, "image/png", "valid.png")], 1024, storage,
            )
        monkeypatch.setattr(session, "commit", real_commit)
        assert storage.data == {}
        assert await _counts(session) == (0, 0)

        item = await create_item(
            session, company_id, creator_id,
            [(png, "image/png", "kept.png")], 1024, storage,
        )
        with pytest.raises(ReceiptValidationError):
            await append_files(
                session, item.id, company_id, creator_id,
                [(png, "image/png", "good.png"), (b"bad", "image/png", "bad.png")],
                1024, storage,
            )
        assert len((await get_item(session, item.id, company_id)).files) == 1
        assert await _counts(session) == (1, 1)
        assert len(storage.data) == 1


@pytest.mark.asyncio
async def test_concurrent_appends_respect_capacity(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_maker() as setup:
        company_id, creator_id = await _seed_owner(setup)
        storage = _MemoryStorage()
        png = b"\x89PNG\r\n\x1a\nreceipt"
        item = await create_item(
            setup, company_id, creator_id,
            [(png, "image/png", str(i)) for i in range(4)], 1024, storage,
        )

    async def append_one() -> str:
        async with db_session_maker() as session:
            try:
                await append_files(
                    session, item.id, company_id, creator_id,
                    [(png, "image/png", "extra")], 1024, storage,
                )
            except InboxConflict as exc:
                return exc.code
            return "OK"

    outcomes = await asyncio.gather(append_one(), append_one())
    assert sorted(outcomes) == ["FILE_LIMIT_REACHED", "OK"]
    async with db_session_maker() as verify:
        assert len((await get_item(verify, item.id, company_id)).files) == 5
        assert len(storage.data) == 5
        assert await _counts(verify) == (1, 5)


@pytest.mark.asyncio
async def test_append_api_rejects_excess_capacity_before_read(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_session_maker() as session:
        company_id, creator_id = await _seed_owner(session)
        png = b"\x89PNG\r\n\x1a\nreceipt"
        item = await create_item(
            session, company_id, creator_id,
            [(png, "image/png", str(i)) for i in range(4)], 1024, _MemoryStorage(),
        )
        files = [UploadFile(file=BytesIO(png), filename=f"{i}.png") for i in range(2)]
        reads = [AsyncMock(side_effect=AssertionError("part was read")) for _ in files]
        for file, read in zip(files, reads, strict=True):
            monkeypatch.setattr(file, "read", read)
        user = User(
            id=creator_id, email=f"{creator_id}@example.test", hashed_password="test-hash",
            company_id=company_id, role="owner",
        )
        with pytest.raises(HTTPException) as error:
            await append_receipt_inbox_files(
                item.id, files=files, language="en", user=user, session=session,
            )
        assert error.value.status_code == 409
        assert error.value.detail == "FILE_LIMIT_REACHED"
        assert all(read.await_count == 0 for read in reads)


@pytest.mark.asyncio
async def test_cross_company_and_pending_attachment_guards(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_maker() as session:
        company_id, creator_id = await _seed_owner(session)
        other_company_id, _ = await _seed_owner(session)
        storage = _MemoryStorage()
        png = b"\x89PNG\r\n\x1a\nreceipt"
        item = await create_item(
            session, company_id, creator_id,
            [(png, "image/png", "receipt.png")], 1024, storage,
        )
        attachment_id = item.files[0].id
        with pytest.raises(LookupError):
            await append_files(
                session, item.id, other_company_id, creator_id,
                [(png, "image/png", "intruder.png")], 1024, storage,
            )
        with pytest.raises(LookupError):
            await remove_file(session, item.id, attachment_id, other_company_id, storage)
        with pytest.raises(LookupError):
            await discard_item(session, item.id, other_company_id, storage)
        with pytest.raises(LookupError):
            await get_attachment_content(session, attachment_id, other_company_id, storage)
        with pytest.raises(LookupError):
            await delete_attachment(session, attachment_id, company_id, storage)
        with pytest.raises(LookupError):
            await extract_from_attachment(session, company_id, attachment_id)
        assert await get_attachment_content(session, attachment_id, company_id, storage) == (
            png, "image/png",
        )
        assert len(storage.data) == 1
        assert await _counts(session) == (1, 1)


@pytest.mark.asyncio
async def test_append_and_discard_race_leaves_no_files(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_maker() as setup:
        company_id, creator_id = await _seed_owner(setup)
        storage = _MemoryStorage()
        png = b"\x89PNG\r\n\x1a\nreceipt"
        item = await create_item(
            setup, company_id, creator_id, [(png, "image/png", "first")], 1024, storage,
        )

    async def append() -> None:
        async with db_session_maker() as session:
            try:
                await append_files(
                    session, item.id, company_id, creator_id,
                    [(png, "image/png", "second")], 1024, storage,
                )
            except LookupError:
                pass  # Discard won the item lock.

    async def discard() -> None:
        async with db_session_maker() as session:
            await discard_item(session, item.id, company_id, storage)

    await asyncio.gather(append(), discard())
    async with db_session_maker() as verify:
        assert await _counts(verify) == (0, 0)
        assert storage.data == {}
