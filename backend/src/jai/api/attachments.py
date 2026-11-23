"""Attachment API routes – receipt upload, list, inline download, delete (M8 step 2).

Endpoints:
  POST   /api/v1/expenses/{expense_id}/attachments
      – Upload a receipt (multipart/form-data, field ``file``) → 201 ExpenseAttachmentRead

  GET    /api/v1/expenses/{expense_id}/attachments
      – List all attachments for an expense → 200 ExpenseAttachmentListResponse

  GET    /api/v1/attachments/{attachment_id}/content
      – Serve attachment file inline → 200 (Content-Type=MIME, Content-Disposition=inline)

  DELETE /api/v1/attachments/{attachment_id}
      – Delete attachment DB row + disk file → 204

Auth: owner-only (mirrors api/expenses.py).
company_id is always injected from the authenticated user; never taken from the
request body (red-line 2).
storage_key / disk paths are never returned in any response (red-line 7 / D2).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.config import get_settings
from jai.db import get_session
from jai.models.user import User
from jai.schemas.expense import ExpenseAttachmentListResponse, ExpenseAttachmentRead
from jai.services.expense import (
    ExpenseConflict,
    add_attachment,
    delete_attachment,
    get_attachment_content,
    list_attachments,
)
from jai.services.storage import ReceiptValidationError, get_storage

router = APIRouter(prefix="/api/v1", tags=["attachments"])


def _owner_only(user: User) -> None:
    if user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required."
        )


def _require_company_id(user: User) -> uuid.UUID:
    if user.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company associated.",
        )
    return user.company_id


# ---------------------------------------------------------------------------
# Upload attachment
# ---------------------------------------------------------------------------


@router.post(
    "/expenses/{expense_id}/attachments",
    response_model=ExpenseAttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment_endpoint(
    expense_id: uuid.UUID,
    file: UploadFile,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseAttachmentRead:
    """Upload a receipt file and attach it to the expense.

    Accepts ``multipart/form-data`` with a single ``file`` field.

    Validation (red-line 7):
    - MIME type must be image/png, image/jpeg, image/webp, or application/pdf.
    - File size must not exceed ``config.max_receipt_bytes`` (default 10 MB).
    - Magic bytes must match the declared Content-Type.

    The original client filename is stored for display only; the storage path
    is derived solely from UUIDs + whitelisted extension.
    ``storage_key`` and disk paths are never returned in the response.
    """
    _owner_only(user)
    company_id = _require_company_id(user)

    content = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    filename = file.filename

    settings = get_settings()
    storage = get_storage()

    try:
        return await add_attachment(
            session=session,
            expense_id=expense_id,
            company_id=company_id,
            content=content,
            mime_type=mime_type,
            filename=filename,
            creator_id=user.id,
            storage=storage,
            max_receipt_bytes=settings.max_receipt_bytes,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExpenseConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "detail": str(exc)},
        ) from exc
    except ReceiptValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# List attachments
# ---------------------------------------------------------------------------


@router.get(
    "/expenses/{expense_id}/attachments",
    response_model=ExpenseAttachmentListResponse,
)
async def list_attachments_endpoint(
    expense_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseAttachmentListResponse:
    """Return all attachments for an expense."""
    _owner_only(user)
    company_id = _require_company_id(user)
    try:
        return await list_attachments(session, expense_id, company_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Serve attachment content (inline)
# ---------------------------------------------------------------------------


@router.get("/attachments/{attachment_id}/content")
async def get_attachment_content_endpoint(
    attachment_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the raw file bytes for an attachment inline.

    Returns:
    - ``Content-Type``: the stored MIME type (e.g. ``image/png``,
      ``application/pdf``).
    - ``Content-Disposition: inline`` – instructs the browser to render the
      file in-page rather than trigger a download.
    - No ``storage_key`` or disk path in the response body.

    Cross-company access → 404.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    storage = get_storage()
    try:
        content, mime_type = await get_attachment_content(
            session, attachment_id, company_id, storage
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment file not found in storage.",
        ) from exc

    return Response(
        content=content,
        media_type=mime_type,
        headers={"Content-Disposition": "inline"},
    )


# ---------------------------------------------------------------------------
# Delete attachment
# ---------------------------------------------------------------------------


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment_endpoint(
    attachment_id: uuid.UUID,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete an attachment: DB row + disk file.

    Disk deletion failures are logged but do NOT fail this request.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    storage = get_storage()
    try:
        await delete_attachment(session, attachment_id, company_id, storage)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExpenseConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "detail": str(exc)},
        ) from exc
