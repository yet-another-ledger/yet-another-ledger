"""PDF download endpoints – M9 step 1 / step 2 / step 3 / step 4.

Endpoints
---------
GET /api/v1/invoices/{id}/pdf?locale=en|zh
    Download an invoice as a PDF file.
    - locale is optional; when omitted the D2 resolution chain is used:
        customer.locale → company default → "en".
    - Returns application/pdf with Content-Disposition: attachment.
    - company_id is injected from the authenticated user (red-line 2).
    - Cross-company or missing invoice → 404.
    - owner-only (same guard as api/invoices.py).

GET /api/v1/quotes/{id}/pdf?locale=en|zh
    Download a quote as a PDF file.
    - Same locale resolution, same auth guards.
    - No due_amount / paid_status rendered (quotes have no payment dimension).
    - No cost/margin fields rendered (client-facing zero-leakage).

GET /api/v1/payments/{id}/receipt-pdf?locale=en|zh
    Download a payment receipt as a PDF file.
    - Single payment → one receipt (D3).
    - Same locale resolution, same auth guards.
    - Receipt is download-only; no email sending (D3).
    - Amounts taken from payment and invoice snapshots, never recalculated.
"""
# ruff: noqa: E501

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal, Never

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.config import get_settings
from jai.db import get_session
from jai.models._enums import DocumentArtifactReason, InvoiceStatus
from jai.models.user import User
from jai.schemas.artifact import (
    DocumentArtifactListResponse,
    DocumentArtifactNotFoundErrorResponse,
    DocumentArtifactRead,
    DocumentArtifactUnprocessableErrorResponse,
    DocumentArtifactUploadConflictErrorResponse,
    DocumentArtifactValidationConflictErrorResponse,
    DocumentArtifactValidationFailedErrorResponse,
    DocumentArtifactValidationRead,
)
from jai.schemas.pdf_appendix import (
    PdfAppendixErrorResponse,
    PdfAppendixFileListResponse,
    PdfAppendixFileRead,
    PdfAppendixLimitsRead,
    PdfLibraryFileUpdate,
)
from jai.services.artifact_validation import (
    ArtifactAIConfigurationError,
    ArtifactAIValidationError,
    validate_uploaded_invoice_artifact,
)
from jai.services.artifacts import (
    ArtifactFileValidationError,
    ensure_invoice_artifact_upload_eligible,
    validate_artifact_pdf,
)

from .invoices import _owner_only, _require_company_id

router = APIRouter(prefix="/api/v1", tags=["pdf"])
_PDF_APPENDIX_MULTIPART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["file"],
    "properties": {
        "file": {"type": "string", "format": "binary"},
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "additionalProperties": False,
}
_PDF_APPENDIX_ERRORS = {
    404: {"model": PdfAppendixErrorResponse, "description": "PDF material not found."},
    413: {
        "model": PdfAppendixErrorResponse,
        "description": "PDF material exceeds a configured limit.",
    },
    422: {"model": PdfAppendixErrorResponse, "description": "Invalid PDF material request."},
    500: {"model": PdfAppendixErrorResponse, "description": "PDF material could not be saved."},
}


class _PdfAppendixTooLargeError(ValueError):
    """Private control flow for the upload byte-limit response."""


async def _read_pdf_appendix_multipart(
    request: Request, *, max_bytes: int
) -> tuple[bytes, str, str, str | None]:
    """Read one bounded file and optional title without UploadFile spooling."""
    try:
        media_type, options = parse_options_header(request.headers.get("content-type"))
        boundary = options.get(b"boundary")
        if media_type.lower() != b"multipart/form-data" or not boundary or len(boundary) > 200:
            raise _ArtifactMultipartError
        current_name = bytearray()
        current_value = bytearray()
        headers: dict[bytes, bytes] = {}
        fields: set[bytes] = set()
        body = bytearray()
        title_body = bytearray()
        part_name = b""
        filename = ""
        mime_type = "application/octet-stream"
        complete = False

        def on_part_begin() -> None:
            nonlocal headers
            headers = {}

        def on_header_begin() -> None:
            current_name.clear()
            current_value.clear()

        def on_header_field(data: bytes, start: int, end: int) -> None:
            current_name.extend(data[start:end])
            if len(current_name) > _ARTIFACT_MULTIPART_MAX_HEADER_BYTES:
                raise _ArtifactMultipartError

        def on_header_value(data: bytes, start: int, end: int) -> None:
            current_value.extend(data[start:end])
            if len(current_value) > _ARTIFACT_MULTIPART_MAX_HEADER_BYTES:
                raise _ArtifactMultipartError

        def on_header_end() -> None:
            name = bytes(current_name).lower()
            if not name or name in headers or len(headers) >= _ARTIFACT_MULTIPART_MAX_HEADERS:
                raise _ArtifactMultipartError
            headers[name] = bytes(current_value)

        def on_headers_finished() -> None:
            nonlocal part_name, filename, mime_type
            disposition, params = parse_options_header(headers.get(b"content-disposition"))
            part_name = params.get(b"name", b"")
            if (
                disposition.lower() != b"form-data"
                or part_name not in {b"file", b"title"}
                or part_name in fields
            ):
                raise _ArtifactMultipartError
            fields.add(part_name)
            if part_name == b"file":
                if b"filename" not in params:
                    raise _ArtifactMultipartError
                filename = params[b"filename"].decode("utf-8", "replace")
                mime_type = headers.get(b"content-type", b"application/octet-stream").decode(
                    "latin-1"
                )

        def on_part_data(data: bytes, start: int, end: int) -> None:
            content = body if part_name == b"file" else title_body
            cap = max_bytes if part_name == b"file" else 2000
            size = end - start
            remaining = cap - len(content)
            if size > remaining:
                if remaining >= 0:
                    content.extend(data[start : start + remaining + 1])
                if part_name == b"file":
                    raise _PdfAppendixTooLargeError
                raise _ArtifactMultipartError
            content.extend(data[start:end])

        def on_end() -> None:
            nonlocal complete
            complete = True

        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": on_part_begin,
                "on_header_begin": on_header_begin,
                "on_header_field": on_header_field,
                "on_header_value": on_header_value,
                "on_header_end": on_header_end,
                "on_headers_finished": on_headers_finished,
                "on_part_data": on_part_data,
                "on_end": on_end,
            },
        )
        received = 0
        envelope_limit = max_bytes + 16 * 1024 + 2000
        async for chunk in request.stream():
            allowed = envelope_limit - received
            if allowed <= 0:
                raise _ArtifactMultipartError
            parser_chunk = chunk[:allowed]
            parser.write(parser_chunk)
            received += len(parser_chunk)
            if complete:
                break
            if len(parser_chunk) != len(chunk):
                raise _ArtifactMultipartError
        parser.finalize()
        if not complete or b"file" not in fields or len(fields) > 2:
            raise _ArtifactMultipartError
        title = title_body.decode("utf-8").strip() if b"title" in fields else None
        if b"title" in fields and not title:
            raise _ArtifactMultipartError
        return bytes(body), mime_type, filename, title
    except Exception as exc:
        if isinstance(exc, _PdfAppendixTooLargeError):
            raise
        if isinstance(exc, _ArtifactMultipartError):
            raise
        raise _ArtifactMultipartError from exc


def _appendix_error(code: str, message: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@router.get("/pdf-library/limits", response_model=PdfAppendixLimitsRead)
async def pdf_appendix_limits(user: User = Depends(current_mfa_user)) -> PdfAppendixLimitsRead:
    _owner_only(user)
    cfg = get_settings()
    return PdfAppendixLimitsRead(
        max_appendix_file_bytes=cfg.max_appendix_file_bytes,
        max_quote_appendices=cfg.max_quote_appendices,
        max_quote_appendix_input_bytes=cfg.max_quote_appendix_input_bytes,
        max_quote_pdf_bytes=cfg.max_quote_pdf_bytes,
        max_quote_pdf_pages=cfg.max_quote_pdf_pages,
    )


@router.get(
    "/pdf-library",
    response_model=PdfAppendixFileListResponse,
    responses={422: _PDF_APPENDIX_ERRORS[422]},
)
async def list_pdf_library(
    search: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> PdfAppendixFileListResponse:
    _owner_only(user)
    company_id = _require_company_id(user)
    from jai.services.pdf_appendix import list_library_files

    return await list_library_files(
        session,
        company_id=company_id,
        search=search,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/pdf-library",
    status_code=201,
    response_model=PdfAppendixFileRead,
    responses={
        413: _PDF_APPENDIX_ERRORS[413],
        422: _PDF_APPENDIX_ERRORS[422],
        500: _PDF_APPENDIX_ERRORS[500],
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"multipart/form-data": {"schema": _PDF_APPENDIX_MULTIPART_SCHEMA}},
        }
    },
)
async def upload_pdf_library_file(
    request: Request,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> PdfAppendixFileRead:
    _owner_only(user)
    company_id = _require_company_id(user)
    from jai.db import set_rls_company
    from jai.services.pdf_appendix import (
        PdfAppendixValidationError,
        new_file_values,
        normalized_filename,
        save_library_file,
        validate_pdf,
    )

    await set_rls_company(session, company_id)
    try:
        content, mime_type, original_filename, title = await _read_pdf_appendix_multipart(
            request, max_bytes=get_settings().max_appendix_file_bytes
        )
        filename = normalized_filename(original_filename)
        if title is not None and (len(title) > 500 or not title.strip()):
            raise _ArtifactMultipartError
        validated = await validate_pdf(
            content,
            mime_type,
            max_bytes=get_settings().max_appendix_file_bytes,
            max_pages=get_settings().max_quote_pdf_pages,
        )
    except _PdfAppendixTooLargeError:
        raise _appendix_error(
            "PDF_APPENDIX_FILE_TOO_LARGE", "The PDF exceeds the configured file size limit.", 413
        ) from None
    except (_ArtifactMultipartError, PdfAppendixValidationError):
        raise _appendix_error(
            "PDF_APPENDIX_INVALID", "Upload a supported, static PDF within the configured limits."
        ) from None
    row = await save_library_file(
        session,
        company_id=company_id,
        values=new_file_values(
            company_id=company_id,
            title=title or filename,
            filename=filename,
            content=content,
            validated=validated,
        ),
    )
    try:
        await session.flush()
        response = PdfAppendixFileRead.model_validate(row)
        await session.commit()
    except Exception as exc:
        logging.getLogger("jai").error(
            "PDF appendix database insert failed (%s)", type(exc).__name__
        )
        await session.rollback()
        raise _appendix_error(
            "PDF_APPENDIX_SAVE_FAILED", "The PDF could not be saved.", 500
        ) from None
    return response


@router.patch(
    "/pdf-library/{file_id}",
    response_model=PdfAppendixFileRead,
    responses={404: _PDF_APPENDIX_ERRORS[404], 422: _PDF_APPENDIX_ERRORS[422]},
)
async def update_pdf_library_file(
    file_id: uuid.UUID,
    payload: PdfLibraryFileUpdate,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> PdfAppendixFileRead:
    _owner_only(user)
    company_id = _require_company_id(user)
    from jai.services.pdf_appendix import read_file_metadata, update_library_file

    row = await update_library_file(session, company_id=company_id, file_id=file_id, data=payload)
    if row is None:
        raise _appendix_error("PDF_APPENDIX_NOT_FOUND", "PDF material was not found.", 404)
    await session.flush()
    response = await read_file_metadata(session, company_id=company_id, file_id=file_id)
    assert response is not None
    await session.commit()
    return response


@router.get(
    "/pdf-files/{file_id}/content",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "Original PDF file."},
        404: _PDF_APPENDIX_ERRORS[404],
        422: _PDF_APPENDIX_ERRORS[422],
    },
)
async def get_pdf_appendix_content(
    file_id: uuid.UUID,
    disposition: Literal["inline", "attachment"] = Query(default="attachment"),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    _owner_only(user)
    company_id = _require_company_id(user)
    from jai.services.pdf import build_content_disposition
    from jai.services.pdf_appendix import get_file_content

    result = await get_file_content(session, company_id=company_id, file_id=file_id)
    if result is None:
        raise _appendix_error("PDF_APPENDIX_NOT_FOUND", "PDF material was not found.", 404)
    content, filename = result
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(filename).replace(
                "attachment;", f"{disposition};", 1
            ),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


def _artifact_422_response(description: str) -> dict[str, Any]:
    """Describe both raw-file and stable parameter-validation failures."""
    return {
        "model": DocumentArtifactUnprocessableErrorResponse,
        "description": description,
    }


_ARTIFACT_UPLOAD_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": DocumentArtifactNotFoundErrorResponse,
        "description": "Invoice is missing, foreign, or not issued.",
    },
    status.HTTP_409_CONFLICT: {
        "model": DocumentArtifactUploadConflictErrorResponse,
        "description": "An artifact already exists.",
    },
    status.HTTP_422_UNPROCESSABLE_ENTITY: _artifact_422_response(
        "The supplied file is not an accepted PDF, or request parameters are invalid."
    ),
}
_ARTIFACT_VALIDATION_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": DocumentArtifactNotFoundErrorResponse,
        "description": "Invoice is missing, foreign, or not issued.",
    },
    status.HTTP_409_CONFLICT: {
        "model": DocumentArtifactValidationConflictErrorResponse,
        "description": "An artifact already exists or advisory AI is unavailable.",
    },
    status.HTTP_422_UNPROCESSABLE_ENTITY: _artifact_422_response(
        "The supplied file is not an accepted PDF, or request parameters are invalid."
    ),
    status.HTTP_502_BAD_GATEWAY: {
        "model": DocumentArtifactValidationFailedErrorResponse,
        "description": "The advisory AI validation failed.",
    },
}

_ARTIFACT_MULTIPART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["file"],
    "properties": {
        "file": {
            "type": "string",
            "format": "binary",
            "description": "One historical PDF to retain or compare exactly.",
        }
    },
    "additionalProperties": False,
}

# The file payload has its own exact limit.  Keep framing separately bounded
# so an attacker cannot make the route scan an unlimited preamble, headers, or
# epilogue while staying below a (possibly absent or dishonest) Content-Length.
_ARTIFACT_MULTIPART_FRAMING_BYTES = 16 * 1024
_ARTIFACT_MULTIPART_MAX_HEADERS = 8
_ARTIFACT_MULTIPART_MAX_HEADER_BYTES = 4224


class _ArtifactMultipartError(ValueError):
    """Private control-flow error that never exposes parser implementation details."""


def _invalid_artifact_file_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "INVALID_ARTIFACT_FILE",
            "message": "The file must be a valid PDF within the configured size limit.",
        },
    )


async def _read_single_artifact_multipart(
    request: Request, *, max_bytes: int
) -> tuple[bytes, str, str]:
    """Read one PDF part directly from ASGI without Starlette's disk spool.

    ``UploadFile`` and ``request.form()`` are intentionally not used here:
    both invoke Starlette's general multipart parser before route code runs and
    can spool oversized file parts to disk.  This parser retains at most the
    configured file limit plus its one-byte overflow sentinel, then aborts the
    stream.  Its narrow contract rejects every part except one named ``file``.
    """
    try:
        content_type = request.headers.get("content-type")
        media_type, options = parse_options_header(content_type)
        boundary = options.get(b"boundary")
        if media_type.lower() != b"multipart/form-data" or not boundary:
            raise _ArtifactMultipartError

        current_header_name = bytearray()
        current_header_value = bytearray()
        headers: dict[bytes, bytes] = {}
        header_count = 0
        content = bytearray()
        mime_type = "application/octet-stream"
        original_filename = ""
        part_count = 0
        completed = False

        def on_part_begin() -> None:
            nonlocal headers, part_count
            part_count += 1
            if part_count > 1:
                raise _ArtifactMultipartError
            headers = {}

        def on_header_begin() -> None:
            current_header_name.clear()
            current_header_value.clear()

        def on_header_field(data: bytes, start: int, end: int) -> None:
            current_header_name.extend(data[start:end])
            if len(current_header_name) > _ARTIFACT_MULTIPART_MAX_HEADER_BYTES:
                raise _ArtifactMultipartError

        def on_header_value(data: bytes, start: int, end: int) -> None:
            current_header_value.extend(data[start:end])
            if len(current_header_value) > _ARTIFACT_MULTIPART_MAX_HEADER_BYTES:
                raise _ArtifactMultipartError

        def on_header_end() -> None:
            nonlocal header_count
            header_count += 1
            name = bytes(current_header_name).lower()
            if (
                header_count > _ARTIFACT_MULTIPART_MAX_HEADERS
                or not name
                or name in headers
            ):
                raise _ArtifactMultipartError
            headers[name] = bytes(current_header_value)

        def on_headers_finished() -> None:
            nonlocal mime_type, original_filename
            disposition, disposition_options = parse_options_header(
                headers.get(b"content-disposition")
            )
            if (
                disposition.lower() != b"form-data"
                or disposition_options.get(b"name") != b"file"
                or b"filename" not in disposition_options
            ):
                raise _ArtifactMultipartError
            mime_type = headers.get(b"content-type", b"application/octet-stream").decode(
                "latin-1"
            )
            original_filename = disposition_options[b"filename"].decode("utf-8", "replace")

        def on_part_data(data: bytes, start: int, end: int) -> None:
            data_size = end - start
            remaining_allowed = max_bytes - len(content)
            if data_size > remaining_allowed:
                # Keep only the sentinel byte that proves the bounded file limit
                # was crossed; never copy or spool the rest of an ASGI chunk.
                sentinel_size = max(remaining_allowed + 1, 0)
                content.extend(data[start : start + sentinel_size])
                raise _ArtifactMultipartError
            content.extend(data[start:end])

        def on_part_end() -> None:
            if part_count != 1:
                raise _ArtifactMultipartError

        def on_end() -> None:
            nonlocal completed
            completed = True

        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": on_part_begin,
                "on_header_begin": on_header_begin,
                "on_header_field": on_header_field,
                "on_header_value": on_header_value,
                "on_header_end": on_header_end,
                "on_headers_finished": on_headers_finished,
                "on_part_data": on_part_data,
                "on_part_end": on_part_end,
                "on_end": on_end,
            },
        )
        total_received = 0
        max_envelope_bytes = max_bytes + _ARTIFACT_MULTIPART_FRAMING_BYTES
        async for chunk in request.stream():
            remaining_envelope = max_envelope_bytes - total_received
            if remaining_envelope <= 0:
                raise _ArtifactMultipartError
            # ASGI chunk boundaries are transport details, not multipart
            # syntax.  Parsing just the still-budgeted prefix lets a closing
            # delimiter win over arbitrary same-chunk epilogue bytes while
            # retaining a hard bounded parser input limit.
            parser_input = chunk[:remaining_envelope]
            parser.write(parser_input)
            total_received += len(parser_input)
            if completed:
                break
            if len(parser_input) != len(chunk):
                raise _ArtifactMultipartError
        parser.finalize()
    except Exception as exc:
        if isinstance(exc, _ArtifactMultipartError):
            raise
        raise _ArtifactMultipartError from exc

    if not completed or part_count != 1:
        raise _ArtifactMultipartError
    return bytes(content), mime_type, original_filename


async def _read_and_validate_artifact_upload(request: Request) -> tuple[bytes, str]:
    """Apply the shared streaming boundary before the Step 1 endpoint stubs."""
    try:
        content, mime_type, original_filename = await _read_single_artifact_multipart(
            request, max_bytes=get_settings().max_artifact_bytes
        )
        validate_artifact_pdf(
            content, mime_type, get_settings().max_artifact_bytes
        )
    except (_ArtifactMultipartError, ArtifactFileValidationError):
        raise _invalid_artifact_file_error() from None
    return content, original_filename


def _artifact_upload_not_implemented() -> Never:
    """Keep the Step 1 contract explicit until Step 2 adds persistence."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "ARTIFACT_UPLOAD_NOT_IMPLEMENTED",
            "message": "Document artifact upload is not available yet.",
        },
    )


@router.get(
    "/invoices/{invoice_id}/pdf",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Invoice PDF download.",
        }
    },
)
async def download_invoice_pdf(
    invoice_id: uuid.UUID,
    locale: Literal["en", "zh"] | None = Query(
        default=None,
        description=(
            "Document language. When omitted the D2 resolution chain is used: "
            "customer.locale → company default → 'en'."
        ),
    ),
    preview: bool = Query(
        default=False,
        description="Render for the in-app preview only; do not retain an artifact.",
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render and download an invoice as PDF.

    ``locale`` controls the language of static labels (Invoice, Date, Due Date,
    etc.).  User-entered content (names, descriptions, notes) is rendered as-is.

    When ``locale`` is omitted the smart locale-resolution chain (D2) is used:
    customer.locale → company-level default → "en".
    """
    _owner_only(user)
    company_id = _require_company_id(user)

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from jai.models.invoice import Invoice
    from jai.services.artifacts import retain_invoice_artifact
    from jai.services.pdf import build_content_disposition, render_invoice_pdf_artifact

    pdf_bytes, filename, resolved_locale, render_fingerprint = await render_invoice_pdf_artifact(
        session=session,
        invoice_id=invoice_id,
        company_id=company_id,
        locale=locale,
    )
    invoice = await session.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id, Invoice.company_id == company_id)
        .options(selectinload(Invoice.party_snapshot))
    )
    # A draft is a preview-only document.  Issued formal documents retain the
    # exact bytes that this response returns.
    if (
        not preview
        and invoice is not None
        and invoice.status in {InvoiceStatus.SENT, InvoiceStatus.COMPLETED}
    ):
        artifact, _ = await retain_invoice_artifact(
            session, invoice_id=invoice_id, company_id=company_id, pdf_bytes=pdf_bytes,
            render_fingerprint=render_fingerprint, locale=resolved_locale,
            filename=filename, reason=DocumentArtifactReason.DOWNLOAD,
        )
        await session.commit()
        pdf_bytes = artifact.pdf_bytes
        filename = artifact.filename

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/invoices/{invoice_id}/artifacts", response_model=DocumentArtifactListResponse)
async def list_invoice_artifacts_endpoint(
    invoice_id: uuid.UUID, user: User = Depends(current_mfa_user), session: AsyncSession = Depends(get_session)
) -> DocumentArtifactListResponse:
    _owner_only(user)
    from jai.services.artifacts import list_invoice_artifacts

    rows = await list_invoice_artifacts(session, invoice_id=invoice_id, company_id=_require_company_id(user))
    return DocumentArtifactListResponse(items=[DocumentArtifactRead.model_validate(row) for row in rows])


@router.get("/invoices/{invoice_id}/artifacts/{artifact_id}", response_class=Response)
async def download_invoice_artifact_endpoint(
    invoice_id: uuid.UUID, artifact_id: uuid.UUID, user: User = Depends(current_mfa_user), session: AsyncSession = Depends(get_session)
) -> Response:
    _owner_only(user)
    from jai.services.artifacts import get_invoice_artifact
    from jai.services.pdf import build_content_disposition

    artifact = await get_invoice_artifact(session, invoice_id=invoice_id, artifact_id=artifact_id, company_id=_require_company_id(user))
    return Response(
        content=artifact.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(artifact.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/invoices/{invoice_id}/artifacts",
    response_model=DocumentArtifactRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ARTIFACT_UPLOAD_ERRORS,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"multipart/form-data": {"schema": _ARTIFACT_MULTIPART_SCHEMA}},
        }
    },
)
async def upload_invoice_artifact_endpoint(
    invoice_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentArtifactRead:
    """Validate then retain one exact historical formal-document PDF."""
    _owner_only(user)
    await ensure_invoice_artifact_upload_eligible(
        session, invoice_id=invoice_id, company_id=_require_company_id(user)
    )
    content, original_filename = await _read_and_validate_artifact_upload(request)
    from jai.services.artifacts import create_uploaded_invoice_artifact

    artifact = await create_uploaded_invoice_artifact(
        session,
        invoice_id=invoice_id,
        company_id=_require_company_id(user),
        pdf_bytes=content,
        original_filename=original_filename,
    )
    await session.commit()
    return DocumentArtifactRead.model_validate(artifact)


@router.post(
    "/invoices/{invoice_id}/artifacts/validate-upload",
    response_model=DocumentArtifactValidationRead,
    responses=_ARTIFACT_VALIDATION_ERRORS,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"multipart/form-data": {"schema": _ARTIFACT_MULTIPART_SCHEMA}},
        }
    },
)
async def validate_invoice_artifact_upload_endpoint(
    invoice_id: uuid.UUID,
    request: Request,
    language: Literal["en", "zh"] = Query(description="Language for advisory summary and notes."),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentArtifactValidationRead:
    """Explicit, advisory-only comparison; neither AI nor PDF bytes are retained."""
    _owner_only(user)
    company_id = _require_company_id(user)
    await ensure_invoice_artifact_upload_eligible(
        session, invoice_id=invoice_id, company_id=company_id
    )
    content, _ = await _read_and_validate_artifact_upload(request)
    try:
        return await validate_uploaded_invoice_artifact(
            session,
            invoice_id=invoice_id,
            company_id=company_id,
            pdf_bytes=content,
            language=language,
        )
    except ArtifactAIConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "AI_NOT_CONFIGURED", "message": "AI validation is not configured."},
        ) from None
    except ArtifactAIValidationError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "AI_VALIDATION_FAILED", "message": "AI validation failed."},
        ) from None


@router.get(
    "/quotes/{quote_id}/pdf",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Quote PDF download.",
        }
    },
)
async def download_quote_pdf(
    quote_id: uuid.UUID,
    locale: Literal["en", "zh"] | None = Query(
        default=None,
        description=(
            "Document language. When omitted the D2 resolution chain is used: "
            "customer.locale → company default → 'en'."
        ),
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render and download a quote as PDF.

    ``locale`` controls the language of static labels (Quote, Date, Valid Until,
    etc.).  User-entered content (names, descriptions, notes) is rendered as-is.

    When ``locale`` is omitted the smart locale-resolution chain (D2) is used:
    customer.locale → company-level default → "en".

    No cost/margin/estimate data is included in the PDF (client-facing
    zero-leakage guard).  No due_amount / paid_status is rendered (quotes have
    no payment dimension).
    """
    _owner_only(user)
    company_id = _require_company_id(user)

    from jai.services.pdf import build_content_disposition, render_quote_pdf

    pdf_bytes, filename = await render_quote_pdf(
        session=session,
        quote_id=quote_id,
        company_id=company_id,
        locale=locale,
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(filename),
        },
    )


@router.get(
    "/payments/{payment_id}/receipt-pdf",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Payment receipt PDF download.",
        }
    },
)
async def download_payment_receipt_pdf(
    payment_id: uuid.UUID,
    locale: Literal["en", "zh"] | None = Query(
        default=None,
        description=(
            "Document language. When omitted the D2 resolution chain is used: "
            "customer.locale → company default → 'en'."
        ),
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render and download a payment receipt as PDF.

    Produces a single-payment receipt (D3).  The receipt shows:
    - Company header + customer billing address.
    - Related invoice number.
    - Payment date, amount, and payment method (from snapshot).
    - Invoice total, amount paid (total), and amount due (from invoice snapshots).

    Amounts come exclusively from DB snapshots – never recalculated (red-line 1).
    Receipt is download-only; no email sending (D3).

    ``locale`` controls the language of static labels.  When omitted the D2
    resolution chain is used: customer.locale → company-level default → 'en'.
    """
    _owner_only(user)
    company_id = _require_company_id(user)

    from jai.services.pdf import build_content_disposition, render_payment_receipt_pdf

    pdf_bytes, filename = await render_payment_receipt_pdf(
        session=session,
        payment_id=payment_id,
        company_id=company_id,
        locale=locale,
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(filename),
        },
    )
