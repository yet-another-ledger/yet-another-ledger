"""Receipt inbox OpenAPI contract; workflow operations land in later M14 steps."""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.config import get_settings
from jai.db import get_session
from jai.models.user import User
from jai.schemas.expense import ExpenseInput
from jai.schemas.receipt_inbox import (
    ReceiptInboxConfirmationRead,
    ReceiptInboxConflict,
    ReceiptInboxExtractRequest,
    ReceiptInboxListResponse,
    ReceiptInboxRead,
    ReceiptInboxStatus,
)
from jai.services.receipt_inbox import (
    MAX_FILES,
    InboxConflict,
    append_files,
    check_append_capacity,
    confirm_item,
    create_item,
    discard_item,
    extract_item,
    get_item,
    list_items,
    remove_file,
)
from jai.services.storage import ReceiptValidationError, get_storage

router = APIRouter(prefix="/api/v1/receipt-inbox", tags=["receipt-inbox"])


async def _read_uploads(
    files: list[UploadFile], max_bytes: int
) -> list[tuple[bytes, str, str | None]]:
    if not 1 <= len(files) <= MAX_FILES:
        raise ReceiptValidationError("Upload between one and five files.")
    # Read one byte beyond the configured limit so oversized multipart parts are
    # rejected without buffering an unbounded upload in application memory.
    parts = []
    for file in files:
        data = await file.read(max_bytes + 1)
        parts.append((data, file.content_type or "application/octet-stream", file.filename))
    return parts


def _raise_service_error(exc: Exception) -> NoReturn:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, InboxConflict):
        raise HTTPException(status_code=409, detail=exc.code) from exc
    if isinstance(exc, ReceiptValidationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def _owner(user: User = Depends(current_mfa_user)) -> User:
    if user.role != "owner" or user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner access required.")
    return user


def _company_id(user: User) -> uuid.UUID:
    if user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner has no company.")
    return user.company_id


@router.post("", response_model=ReceiptInboxRead, status_code=status.HTTP_201_CREATED)
async def create_receipt_inbox(
    files: list[UploadFile] = File(...),
    language: str = Query("en", pattern="^(en|zh)$"),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxRead:
    """Create a receipt group with one to five files."""
    try:
        created = await create_item(
            session,
            _company_id(user),
            user.id,
            await _read_uploads(files, get_settings().max_receipt_bytes),
            get_settings().max_receipt_bytes,
            get_storage(),
        )
        return await extract_item(
            session, created.id, _company_id(user), get_storage(), language
        )
    except (LookupError, InboxConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.get("", response_model=ReceiptInboxListResponse)
async def list_receipt_inbox(
    status_filter: ReceiptInboxStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxListResponse:
    """List the company's receipt inbox, newest first."""
    return await list_items(session, _company_id(user), status_filter, limit, offset)


@router.get("/{inbox_id}", response_model=ReceiptInboxRead)
async def get_receipt_inbox(
    inbox_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxRead:
    try:
        return await get_item(session, inbox_id, _company_id(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{inbox_id}/files",
    response_model=ReceiptInboxRead,
    responses={409: {"model": ReceiptInboxConflict}},
)
async def append_receipt_inbox_files(
    inbox_id: uuid.UUID,
    files: list[UploadFile] = File(...),
    language: str = Query("en", pattern="^(en|zh)$"),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxRead:
    try:
        await check_append_capacity(session, inbox_id, _company_id(user), len(files))
        updated = await append_files(
            session,
            inbox_id,
            _company_id(user),
            user.id,
            await _read_uploads(files, get_settings().max_receipt_bytes),
            get_settings().max_receipt_bytes,
            get_storage(),
        )
        return await extract_item(
            session, updated.id, _company_id(user), get_storage(), language
        )
    except (LookupError, InboxConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.delete(
    "/{inbox_id}/files/{file_id}",
    response_model=ReceiptInboxRead,
    responses={409: {"model": ReceiptInboxConflict}},
)
async def remove_receipt_inbox_file(
    inbox_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxRead:
    try:
        updated = await remove_file(session, inbox_id, file_id, _company_id(user), get_storage())
        return await extract_item(session, updated.id, _company_id(user), get_storage())
    except (LookupError, InboxConflict) as exc:
        _raise_service_error(exc)


@router.post(
    "/{inbox_id}/extract",
    response_model=ReceiptInboxRead,
    responses={409: {"model": ReceiptInboxConflict}},
)
async def extract_receipt_inbox(
    inbox_id: uuid.UUID,
    body: ReceiptInboxExtractRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxRead:
    try:
        return await extract_item(
            session, inbox_id, _company_id(user), get_storage(), body.language
        )
    except (LookupError, InboxConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.post(
    "/{inbox_id}/confirm",
    response_model=ReceiptInboxConfirmationRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ReceiptInboxConflict}},
)
async def confirm_receipt_inbox(
    inbox_id: uuid.UUID,
    body: ExpenseInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ReceiptInboxConfirmationRead | JSONResponse:
    try:
        return await confirm_item(
            session, inbox_id, _company_id(user), user.id, body
        )
    except InboxConflict as exc:
        if exc.code == "ALREADY_CONFIRMED":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": exc.code,
                    "expense_id": str(exc.expense_id) if exc.expense_id else None,
                },
            )
        _raise_service_error(exc)
    except (LookupError, ReceiptValidationError) as exc:
        _raise_service_error(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.delete(
    "/{inbox_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ReceiptInboxConflict}},
)
async def discard_receipt_inbox(
    inbox_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> None:
    try:
        await discard_item(session, inbox_id, _company_id(user), get_storage())
    except (LookupError, InboxConflict) as exc:
        _raise_service_error(exc)
