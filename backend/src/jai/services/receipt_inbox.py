"""Durable, pre-Expense receipt intake and file lifecycle."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models.dictionary import ExpenseCategory
from jai.models.expense_attachment import ExpenseAttachment
from jai.models.receipt_inbox import ReceiptInbox
from jai.models.vat import VatRate
from jai.schemas.expense import ExpenseAIPrefill, ExpenseInput
from jai.schemas.receipt_inbox import (
    ReceiptInboxAIState,
    ReceiptInboxConfirmationRead,
    ReceiptInboxErrorCode,
    ReceiptInboxFileRead,
    ReceiptInboxListResponse,
    ReceiptInboxRead,
    ReceiptInboxStatus,
)
from jai.services import ai as ai_service
from jai.services.expense import (
    _count_attachments,
    _expense_to_read,
    create_expense_record,
)
from jai.services.storage import MIME_TO_EXT, ReceiptValidationError, Storage, validate_receipt

logger = logging.getLogger(__name__)
MAX_FILES = 5


def _has_presentable_suggestion(
    suggestion: dict[str, object], *, category_matches: bool, rate_matches: bool
) -> bool:
    """Whether extraction can populate at least one review field."""
    if any(
        suggestion.get(field) is not None
        for field in ("expense_date", "net_amount", "vat_amount")
    ):
        return True
    if any(
        isinstance(suggestion.get(field), str) and bool(str(suggestion[field]).strip())
        for field in ("supplier_name", "summary")
    ):
        return True
    return category_matches or rate_matches


async def _suggestion_is_presentable(
    session: AsyncSession, company_id: uuid.UUID, suggestion: dict[str, object]
) -> bool:
    category_matches = False
    category_id = suggestion.get("suggested_category_id")
    category_name = suggestion.get("suggested_category_name")
    if isinstance(category_id, str):
        try:
            category_id = uuid.UUID(category_id)
        except ValueError:
            category_id = None
    if isinstance(category_id, uuid.UUID):
        category_matches = bool(await session.scalar(
            select(ExpenseCategory.id).where(
                ExpenseCategory.company_id == company_id,
                ExpenseCategory.id == category_id,
            )
        ))
    if not category_matches and isinstance(category_name, str) and category_name.strip():
        category_matches = bool(await session.scalar(
            select(ExpenseCategory.id).where(
                ExpenseCategory.company_id == company_id,
                func.lower(ExpenseCategory.name) == category_name.strip().lower(),
            )
        ))

    rate_matches = False
    rate_percent = suggestion.get("vat_rate_percent")
    if rate_percent is not None:
        rate_percent = Decimal(str(rate_percent))
        rate_matches = bool(await session.scalar(
            select(VatRate.id).where(
                VatRate.company_id == company_id,
                func.abs(VatRate.percent - rate_percent) < 0.01,
            )
        ))
    return _has_presentable_suggestion(
        suggestion, category_matches=category_matches, rate_matches=rate_matches
    )


class InboxConflict(ValueError):
    def __init__(self, code: str, expense_id: uuid.UUID | None = None):
        self.code = code
        self.expense_id = expense_id
        super().__init__(code)


def _read(item: ReceiptInbox, files: list[ExpenseAttachment]) -> ReceiptInboxRead:
    return ReceiptInboxRead(
        id=item.id,
        status=ReceiptInboxStatus(item.status),
        files=[
            ReceiptInboxFileRead(
                id=f.id,
                filename=f.filename,
                mime_type=f.mime_type,
                byte_size=f.byte_size,
                created_at=f.created_at,
            )
            for f in files
        ],
        ai_state=ReceiptInboxAIState(item.ai_state),
        ai_suggestion=(
            ExpenseAIPrefill.model_validate(item.ai_suggestion)
            if item.ai_suggestion is not None
            else None
        ),
        ai_error_code=ReceiptInboxErrorCode(item.ai_error_code) if item.ai_error_code else None,
        expense_id=item.expense_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
        confirmed_at=item.confirmed_at,
    )


async def _files(session: AsyncSession, item_id: uuid.UUID) -> list[ExpenseAttachment]:
    result = await session.execute(
        select(ExpenseAttachment)
        .where(ExpenseAttachment.receipt_inbox_id == item_id)
        .order_by(ExpenseAttachment.created_at, ExpenseAttachment.id)
    )
    return list(result.scalars().all())


async def _owned(
    session: AsyncSession, item_id: uuid.UUID, company_id: uuid.UUID, *, lock: bool = False
) -> ReceiptInbox:
    stmt = select(ReceiptInbox).where(
        ReceiptInbox.id == item_id, ReceiptInbox.company_id == company_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise LookupError("Receipt inbox item not found.")
    return item


def _validate(
    parts: list[tuple[bytes, str, str | None]], max_bytes: int
) -> list[tuple[bytes, str, str | None]]:
    if not parts or len(parts) > MAX_FILES:
        raise ReceiptValidationError("Upload between one and five files.")
    normalized = []
    for data, mime, name in parts:
        clean = mime.split(";")[0].strip().lower()
        validate_receipt(data, clean, max_bytes)
        normalized.append((data, clean, name))
    return normalized


async def _persist_files(
    session: AsyncSession,
    item: ReceiptInbox,
    company_id: uuid.UUID,
    creator_id: uuid.UUID,
    parts: list[tuple[bytes, str, str | None]],
    storage: Storage,
) -> list[str]:
    written: list[str] = []
    try:
        for data, mime, filename in parts:
            file_id = uuid.uuid4()
            key = f"{company_id}/{item.id}/{file_id}.{MIME_TO_EXT[mime]}"
            # save may create a partial file before raising. The key is fully
            # controlled here, so register its final storage name in advance.
            written.append(f"receipts/{key}")
            stored = storage.save("receipts", key, data, mime)
            written[-1] = stored
            session.add(
                ExpenseAttachment(
                    id=file_id,
                    company_id=company_id,
                    receipt_inbox_id=item.id,
                    expense_id=None,
                    storage_key=stored,
                    filename=filename,
                    mime_type=mime,
                    byte_size=len(data),
                    creator_id=creator_id,
                )
            )
        await session.flush()
        return written
    except Exception:
        for key in written:
            try:
                storage.delete(key)
            except Exception:
                logger.exception("Failed to compensate inbox upload storage write")
        raise


async def create_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    creator_id: uuid.UUID,
    parts: list[tuple[bytes, str, str | None]],
    max_bytes: int,
    storage: Storage,
) -> ReceiptInboxRead:
    parts = _validate(parts, max_bytes)
    await set_rls_company(session, company_id)
    item = ReceiptInbox(
        company_id=company_id,
        creator_id=creator_id,
        status="PENDING",
        ai_state="NOT_RUN",
        file_revision=1,
    )
    session.add(item)
    await session.flush()
    keys: list[str] = []
    try:
        keys = await _persist_files(session, item, company_id, creator_id, parts, storage)
        await session.commit()
    except Exception:
        await session.rollback()
        for key in keys:
            try:
                storage.delete(key)
            except Exception:
                logger.exception("Failed to compensate inbox transaction")
        raise
    return await get_item(session, item.id, company_id)


async def extract_item(
    session: AsyncSession,
    inbox_id: uuid.UUID,
    company_id: uuid.UUID,
    storage: Storage,
    language: str = "en",
) -> ReceiptInboxRead:
    """Attempt extraction for the current entire group with stale-write guards."""
    await set_rls_company(session, company_id)
    item = await _owned(session, inbox_id, company_id, lock=True)
    if item.status != "PENDING":
        raise InboxConflict("INBOX_NOT_PENDING")
    attachments = await _files(session, inbox_id)
    revision = item.file_revision
    item.extraction_generation += 1
    generation = item.extraction_generation
    item.ai_state = "RUNNING"
    item.ai_error_code = None
    item.ai_suggestion = None
    item.ai_started_at = datetime.now(UTC)
    item.ai_completed_at = None
    file_refs = [(att.mime_type, att.storage_key) for att in attachments]
    await session.commit()

    state = "READY"
    error_code: str | None = None
    suggestion: dict[str, object] | None = None
    try:
        files = [(mime, storage.open(key)) for mime, key in file_refs]
        prefill = await ai_service.extract_from_files(
            session, company_id, files, language=language, strict_response=True
        )
        suggestion = prefill.model_dump(mode="json")
        if not await _suggestion_is_presentable(session, company_id, suggestion):
            suggestion = None
            state = "FAILED"
            error_code = "AI_INVALID_RESPONSE"
    except ai_service.AIUnavailableError as exc:
        await session.rollback()
        state = "UNAVAILABLE"
        error_code = exc.code
    except ai_service.AIExtractionError as exc:
        await session.rollback()
        state = "FAILED"
        error_code = exc.code
    except Exception:
        await session.rollback()
        # Storage and provider exceptions may include keys, paths, response
        # bodies or receipt bytes. Never serialize the exception or traceback.
        logger.warning("Receipt inbox AI extraction failed: AI_PROVIDER_ERROR")
        state = "FAILED"
        error_code = "AI_PROVIDER_ERROR"

    # Conditional UPDATE protects against both a newer extraction and any
    # intervening file mutation/confirmation. It also prevents stale ORM state
    # from writing fields after the model call completes.
    await set_rls_company(session, company_id)
    await session.execute(
        update(ReceiptInbox)
        .where(
            ReceiptInbox.id == inbox_id,
            ReceiptInbox.company_id == company_id,
            ReceiptInbox.status == "PENDING",
            ReceiptInbox.file_revision == revision,
            ReceiptInbox.extraction_generation == generation,
        )
        .values(
            ai_state=state,
            ai_error_code=error_code,
            ai_suggestion=suggestion,
            ai_completed_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return await get_item(session, inbox_id, company_id)


async def list_items(
    session: AsyncSession,
    company_id: uuid.UUID,
    status: ReceiptInboxStatus | None,
    limit: int,
    offset: int,
) -> ReceiptInboxListResponse:
    await set_rls_company(session, company_id)
    stmt = select(ReceiptInbox).where(ReceiptInbox.company_id == company_id)
    count_stmt = (
        select(func.count()).select_from(ReceiptInbox).where(ReceiptInbox.company_id == company_id)
    )
    if status:
        stmt = stmt.where(ReceiptInbox.status == status.value)
        count_stmt = count_stmt.where(ReceiptInbox.status == status.value)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = list(
        (
            await session.execute(
                stmt.order_by(ReceiptInbox.created_at.desc(), ReceiptInbox.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ReceiptInboxListResponse(
        items=[_read(row, await _files(session, row.id)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_item(
    session: AsyncSession, inbox_id: uuid.UUID, company_id: uuid.UUID
) -> ReceiptInboxRead:
    await set_rls_company(session, company_id)
    item = await _owned(session, inbox_id, company_id)
    return _read(item, await _files(session, item.id))


async def confirm_item(
    session: AsyncSession,
    inbox_id: uuid.UUID,
    company_id: uuid.UUID,
    creator_id: uuid.UUID,
    body: ExpenseInput,
) -> ReceiptInboxConfirmationRead:
    """Confirm one pending group atomically with its Expense and attachments."""
    await set_rls_company(session, company_id)
    try:
        item = await _owned(session, inbox_id, company_id, lock=True)
        if item.status != "PENDING":
            raise InboxConflict("ALREADY_CONFIRMED", item.expense_id)

        attachments = await _files(session, inbox_id)
        expense = await create_expense_record(session, company_id, body, creator_id)
        for attachment in attachments:
            attachment.expense_id = expense.id
            attachment.receipt_inbox_id = None

        item.status = "CONFIRMED"
        item.expense_id = expense.id
        item.confirmed_at = datetime.now(UTC)
        # One flush and one commit cover Expense, parent transfer, and history.
        await session.flush()
        # SQLAlchemy expires server/onupdate-generated columns after the flush;
        # refresh them explicitly before building the Pydantic response so an
        # async attribute read cannot trigger implicit IO (MissingGreenlet).
        await session.refresh(item)
        attachment_count = await _count_attachments(session, expense.id)
        inbox_read = _read(item, [])
        expense_read = _expense_to_read(expense, attachment_count)
        await session.commit()
        return ReceiptInboxConfirmationRead(inbox=inbox_read, expense=expense_read)
    except Exception:
        await session.rollback()
        raise


async def append_files(
    session: AsyncSession,
    inbox_id: uuid.UUID,
    company_id: uuid.UUID,
    creator_id: uuid.UUID,
    parts: list[tuple[bytes, str, str | None]],
    max_bytes: int,
    storage: Storage,
) -> ReceiptInboxRead:
    parts = _validate(parts, max_bytes)
    await set_rls_company(session, company_id)
    written: list[str] = []
    try:
        item = await _owned(session, inbox_id, company_id, lock=True)
        existing = await _files(session, item.id)
        if item.status != "PENDING":
            raise InboxConflict("INBOX_NOT_PENDING")
        if len(existing) + len(parts) > MAX_FILES:
            raise InboxConflict("FILE_LIMIT_REACHED")
        written = await _persist_files(session, item, company_id, creator_id, parts, storage)
        item.file_revision += 1
        item.ai_suggestion = None
        item.ai_error_code = None
        item.ai_state = "NOT_RUN"
        await session.commit()
    except Exception:
        await session.rollback()
        for key in written:
            try:
                storage.delete(key)
            except Exception:
                logger.exception("Failed to compensate inbox append")
        raise
    return await get_item(session, inbox_id, company_id)


async def check_append_capacity(
    session: AsyncSession, inbox_id: uuid.UUID, company_id: uuid.UUID, file_count: int
) -> None:
    """Reject an impossible append before the API reads multipart file bytes.

    The locked check in append_files remains authoritative if another request
    changes the group after this lightweight preflight.
    """
    if file_count < 1:
        raise ReceiptValidationError("Upload between one and five files.")
    await set_rls_company(session, company_id)
    item = await _owned(session, inbox_id, company_id)
    if item.status != "PENDING":
        raise InboxConflict("INBOX_NOT_PENDING")
    if len(await _files(session, item.id)) + file_count > MAX_FILES:
        raise InboxConflict("FILE_LIMIT_REACHED")


async def remove_file(
    session: AsyncSession,
    inbox_id: uuid.UUID,
    file_id: uuid.UUID,
    company_id: uuid.UUID,
    storage: Storage,
) -> ReceiptInboxRead:
    await set_rls_company(session, company_id)
    item = await _owned(session, inbox_id, company_id, lock=True)
    files = await _files(session, inbox_id)
    if item.status != "PENDING":
        raise InboxConflict("INBOX_NOT_PENDING")
    if len(files) <= 1:
        raise InboxConflict("LAST_FILE")
    att = next((f for f in files if f.id == file_id), None)
    if att is None:
        raise LookupError("Receipt inbox file not found.")
    key = att.storage_key
    await session.delete(att)
    item.file_revision += 1
    item.ai_suggestion = None
    item.ai_error_code = None
    item.ai_state = "NOT_RUN"
    await session.commit()
    try:
        storage.delete(key)
    except Exception:
        logger.exception("Failed to clean removed inbox file")
    return await get_item(session, inbox_id, company_id)


async def discard_item(
    session: AsyncSession, inbox_id: uuid.UUID, company_id: uuid.UUID, storage: Storage
) -> None:
    await set_rls_company(session, company_id)
    item = await _owned(session, inbox_id, company_id, lock=True)
    if item.status != "PENDING":
        raise InboxConflict("INBOX_NOT_PENDING")
    files = await _files(session, inbox_id)
    keys = [f.storage_key for f in files]
    await session.delete(item)
    await session.commit()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            logger.exception("Failed to clean discarded inbox file")
