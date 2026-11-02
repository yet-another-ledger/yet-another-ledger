"""Manual external sales source and pending-entry API."""

from __future__ import annotations

import uuid
from typing import NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.config import get_settings
from jai.db import get_session
from jai.models.user import User
from jai.schemas.external_sales import (
    ExternalSalesCalculateRequest,
    ExternalSalesCalculation,
    ExternalSalesConfirmRequest,
    ExternalSalesConflict,
    ExternalSalesInput,
    ExternalSalesListRead,
    ExternalSalesRead,
    ExternalSalesRevisionInput,
    ExternalSalesRevisionListRead,
    ExternalSalesRevisionPreview,
    ExternalSalesSourceInput,
    ExternalSalesSourceListRead,
    ExternalSalesSourceRead,
    ExternalSalesVoidInput,
)
from jai.services.external_sales import (
    ExternalSalesConflict as ServiceConflict,
)
from jai.services.external_sales import (
    append_files,
    calculate_sale,
    check_append_capacity,
    confirm_sale,
    create_sale,
    create_source,
    discard_sale,
    extract_sale,
    get_sale,
    list_revisions,
    list_sales,
    list_sources,
    preview_revision,
    read_file,
    remove_file,
    revise_sale,
    update_sale,
    update_source,
    void_sale,
)
from jai.services.receipt_inbox import MAX_FILES
from jai.services.storage import ReceiptValidationError, get_storage

router = APIRouter(prefix="/api/v1", tags=["external-sales"])


def _owner(user: User = Depends(current_mfa_user)) -> User:
    if user.role != "owner" or user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner access required.")
    return user


def _company_id(user: User) -> uuid.UUID:
    if user.company_id is None:
        raise HTTPException(status_code=403, detail="Owner has no company.")
    return user.company_id


def _raise_service_error(exc: Exception) -> NoReturn:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ServiceConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "detail": str(exc),
                "external_sale_id": (
                    str(exc.external_sale_id) if exc.external_sale_id is not None else None
                ),
                "current_version": exc.current_version,
            },
        ) from exc
    if isinstance(exc, ReceiptValidationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


async def _read_uploads(
    files: list[UploadFile], max_bytes: int
) -> list[tuple[bytes, str, str | None]]:
    if not 1 <= len(files) <= MAX_FILES:
        raise ReceiptValidationError("Upload between one and five files.")
    parts: list[tuple[bytes, str, str | None]] = []
    for file in files:
        parts.append(
            (
                await file.read(max_bytes + 1),
                file.content_type or "application/octet-stream",
                file.filename,
            )
        )
    return parts


@router.get("/external-sales-sources", response_model=ExternalSalesSourceListRead)
async def get_external_sales_sources(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesSourceListRead:
    return await list_sources(session, _company_id(user), limit, offset)


@router.post(
    "/external-sales-sources",
    response_model=ExternalSalesSourceRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_external_sales_source(
    body: ExternalSalesSourceInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesSourceRead:
    return await create_source(session, _company_id(user), body)


@router.put("/external-sales-sources/{source_id}", response_model=ExternalSalesSourceRead)
async def put_external_sales_source(
    source_id: uuid.UUID,
    body: ExternalSalesSourceInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesSourceRead:
    try:
        return await update_source(session, _company_id(user), source_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/external-sales", response_model=ExternalSalesListRead)
async def get_external_sales(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesListRead:
    return await list_sales(session, _company_id(user), limit, offset)


@router.post(
    "/external-sales",
    response_model=ExternalSalesRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale(
    body: ExternalSalesInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await create_sale(session, _company_id(user), user.id, body)
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.get("/external-sales/{sale_id}", response_model=ExternalSalesRead)
async def get_external_sale(
    sale_id: uuid.UUID, user: User = Depends(_owner), session: AsyncSession = Depends(get_session)
) -> ExternalSalesRead:
    try:
        return await get_sale(session, _company_id(user), sale_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/external-sales/{sale_id}",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def put_external_sale(
    sale_id: uuid.UUID,
    body: ExternalSalesInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await update_sale(session, _company_id(user), sale_id, body)
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.delete(
    "/external-sales/{sale_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ExternalSalesConflict}},
)
async def delete_external_sale(
    sale_id: uuid.UUID, user: User = Depends(_owner), session: AsyncSession = Depends(get_session)
) -> None:
    try:
        await discard_sale(session, _company_id(user), sale_id, get_storage())
    except (LookupError, ServiceConflict) as exc:
        _raise_service_error(exc)


@router.post(
    "/external-sales/{sale_id}/files",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_files(
    sale_id: uuid.UUID,
    files: list[UploadFile] = File(...),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        await check_append_capacity(session, _company_id(user), sale_id, len(files))
        parts = await _read_uploads(files, get_settings().max_receipt_bytes)
        return await append_files(
            session,
            _company_id(user),
            user.id,
            sale_id,
            parts,
            get_settings().max_receipt_bytes,
            get_storage(),
        )
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.post(
    "/external-sales/{sale_id}/extract",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_extract(
    sale_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await extract_sale(session, _company_id(user), sale_id, get_storage())
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.get("/external-sales/{sale_id}/files/{file_id}")
async def get_external_sale_file(
    sale_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        file_row, content = await read_file(
            session, _company_id(user), sale_id, file_id, get_storage()
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Original file not found") from exc
    filename = quote(file_row.filename or "original", safe="")
    return Response(
        content,
        media_type=file_row.mime_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post(
    "/external-sales/{sale_id}/calculate",
    response_model=ExternalSalesCalculation,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_calculation(
    sale_id: uuid.UUID,
    body: ExternalSalesCalculateRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesCalculation:
    try:
        return await calculate_sale(session, _company_id(user), sale_id, body, get_storage())
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.post(
    "/external-sales/{sale_id}/confirm",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_confirmation(
    sale_id: uuid.UUID,
    body: ExternalSalesConfirmRequest,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await confirm_sale(session, _company_id(user), user.id, sale_id, body, get_storage())
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.post(
    "/external-sales/{sale_id}/revisions/preview",
    response_model=ExternalSalesRevisionPreview,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_revision_preview(
    sale_id: uuid.UUID,
    body: ExternalSalesRevisionInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRevisionPreview:
    try:
        return await preview_revision(session, _company_id(user), sale_id, body)
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.post(
    "/external-sales/{sale_id}/revisions",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_revision(
    sale_id: uuid.UUID,
    body: ExternalSalesRevisionInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await revise_sale(session, _company_id(user), user.id, sale_id, body)
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.get("/external-sales/{sale_id}/revisions", response_model=ExternalSalesRevisionListRead)
async def get_external_sale_revisions(
    sale_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRevisionListRead:
    try:
        return await list_revisions(session, _company_id(user), sale_id, offset, limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/external-sales/{sale_id}/void",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def post_external_sale_void(
    sale_id: uuid.UUID,
    body: ExternalSalesVoidInput,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await void_sale(session, _company_id(user), user.id, sale_id, body)
    except (LookupError, ServiceConflict, ReceiptValidationError) as exc:
        _raise_service_error(exc)


@router.delete(
    "/external-sales/{sale_id}/files/{file_id}",
    response_model=ExternalSalesRead,
    responses={409: {"model": ExternalSalesConflict}},
)
async def delete_external_sale_file(
    sale_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User = Depends(_owner),
    session: AsyncSession = Depends(get_session),
) -> ExternalSalesRead:
    try:
        return await remove_file(session, _company_id(user), sale_id, file_id, get_storage())
    except (LookupError, ServiceConflict) as exc:
        _raise_service_error(exc)
