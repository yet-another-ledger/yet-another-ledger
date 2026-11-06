"""Validation and persistence for immutable PDF appendix originals."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import uuid
from collections.abc import Buffer
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models._enums import QuoteStatus
from jai.models.pdf_appendix import PdfAppendixFile
from jai.schemas.pdf_appendix import (
    PdfAppendixFileListResponse,
    PdfAppendixFileRead,
    PdfLibraryFileUpdate,
)


class QuotePdfCompositionError(ValueError):
    """Safe, stable failure while assembling a complete Quote PDF."""

    def __init__(self, code: str, file_id: uuid.UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.file_id = file_id


def _compose_quote_pdf(
    body: bytes,
    originals: list[tuple[uuid.UUID, bytes, str, int]],
    *,
    max_input_bytes: int,
    max_output_bytes: int,
    max_pages: int,
) -> bytes:
    """Append verified originals to the rendered body with a fresh PDF catalog."""
    readers: list[tuple[PdfReader, uuid.UUID | None]] = []
    streams: list[io.BytesIO] = []
    writer: PdfWriter | None = None
    output: _BoundedPdfBuffer | None = None
    try:
        if not originals:
            return body
        if sum(len(content) for _, content, _, _ in originals) > max_input_bytes:
            raise QuotePdfCompositionError("PDF_APPENDIX_INPUT_LIMIT")
        if not body or len(body) > max_output_bytes:
            raise QuotePdfCompositionError("QUOTE_PDF_OUTPUT_LIMIT")
        streams.append(io.BytesIO(body))
        body_reader = PdfReader(streams[-1], strict=True)
        readers.append((body_reader, None))
        total_pages = len(body_reader.pages)
        for file_id, content, expected_hash, expected_pages in originals:
            try:
                if hashlib.sha256(content).hexdigest() != expected_hash:
                    raise QuotePdfCompositionError("QUOTE_PDF_COMPOSITION_FAILED", file_id)
                stream = io.BytesIO(content)
                streams.append(stream)
                reader = PdfReader(stream, strict=True)
                readers.append((reader, file_id))
                if reader.is_encrypted or len(reader.pages) != expected_pages:
                    raise QuotePdfCompositionError("QUOTE_PDF_COMPOSITION_FAILED", file_id)
            except QuotePdfCompositionError:
                raise
            except Exception as exc:
                raise QuotePdfCompositionError("QUOTE_PDF_COMPOSITION_FAILED", file_id) from exc
            total_pages += len(reader.pages)
        if total_pages > max_pages:
            raise QuotePdfCompositionError("QUOTE_PDF_PAGE_LIMIT")
        writer = PdfWriter()
        for reader, source_file_id in readers:
            try:
                for page in reader.pages:
                    if source_file_id is not None:
                        _reject_embedded_page_objects(page, set())
                    writer.add_page(page)
            except Exception as exc:
                if isinstance(exc, QuotePdfCompositionError):
                    raise
                raise QuotePdfCompositionError(
                    "QUOTE_PDF_COMPOSITION_FAILED", source_file_id
                ) from exc
        output = _BoundedPdfBuffer(max_output_bytes)
        try:
            writer.write(output)
        except PdfAppendixValidationError as exc:
            raise QuotePdfCompositionError("QUOTE_PDF_OUTPUT_LIMIT") from exc
        result = output.getvalue()
        if not result.startswith(b"%PDF-"):
            raise QuotePdfCompositionError("QUOTE_PDF_COMPOSITION_FAILED")
        return result
    except QuotePdfCompositionError:
        raise
    except Exception as exc:
        raise QuotePdfCompositionError("QUOTE_PDF_COMPOSITION_FAILED") from exc
    finally:
        if output is not None:
            output.close()
        if writer is not None:
            writer.close()
        for reader, _ in readers:
            reader.close()
        for stream in streams:
            stream.close()


async def compose_quote_pdf(
    body: bytes,
    originals: list[tuple[uuid.UUID, bytes, str, int]],
    *,
    max_input_bytes: int,
    max_output_bytes: int,
    max_pages: int,
) -> bytes:
    """Compose in bounded worker threads, keeping pypdf off the event loop."""
    await _pdf_semaphore.acquire()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _compose_quote_pdf,
            body,
            originals,
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
            max_pages=max_pages,
        )
    )
    handed_to_worker = False
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        handed_to_worker = True

        def release_when_done(completed: asyncio.Task[bytes]) -> None:
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass
            finally:
                _pdf_semaphore.release()

        worker.add_done_callback(release_when_done)
        raise
    finally:
        if not handed_to_worker:
            _pdf_semaphore.release()


class PdfAppendixValidationError(ValueError):
    """A stable, public-safe PDF validation failure."""


@dataclass(frozen=True)
class ValidatedPdf:
    sha256: str
    size_bytes: int
    page_count: int


class _BoundedPdfBuffer(io.BytesIO):
    """BytesIO that refuses writes beyond the candidate-size policy."""

    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum

    def write(self, data: Buffer) -> int:
        if self.tell() + memoryview(data).nbytes > self.maximum:
            raise PdfAppendixValidationError
        return super().write(data)


_pdf_semaphore = asyncio.Semaphore(4)
_SIGNATURE_MARKER = re.compile(rb"/ByteRange\s*\[")


def _resolve_pdf_value(value: object) -> object:
    """Resolve an indirect PDF value, rejecting broken or cyclic reference chains."""
    seen: set[tuple[object, ...]] = set()
    while (
        hasattr(value, "get_object")
        and hasattr(value, "idnum")
        and hasattr(value, "generation")
    ):
        key: tuple[object, ...] = ("ref", value.idnum, value.generation)
        if key in seen:
            raise PdfAppendixValidationError
        seen.add(key)
        try:
            resolved = value.get_object()
        except Exception as exc:
            raise PdfAppendixValidationError from exc
        if resolved is value:
            raise PdfAppendixValidationError
        value = resolved
    return value


def _semantic_name(dictionary: dict[Any, Any], key: str) -> NameObject | None:
    """Read a name-valued safety field by its resolved PDF meaning."""
    if key not in dictionary:
        return None
    value = _resolve_pdf_value(dictionary[key])
    if not isinstance(value, NameObject):
        raise PdfAppendixValidationError
    return value


def _action_is_allowed(action: object) -> bool:
    action_object: Any = _resolve_pdf_value(action)
    if not hasattr(action_object, "get"):
        return False
    try:
        kind = _semantic_name(action_object, "/S")
    except PdfAppendixValidationError:
        return False
    if kind != "/URI" or "/Next" in action_object:
        return False
    try:
        uri = _resolve_pdf_value(action_object["/URI"])
    except (KeyError, PdfAppendixValidationError):
        return False
    if not isinstance(uri, str):
        return False
    try:
        return urlsplit(uri).scheme.lower() in {"http", "https", "mailto"}
    except ValueError:
        return False


def _reject_embedded_page_objects(
    value: object, visited: set[tuple[object, ...]], *, action_context: bool = False
) -> None:
    """Reject unsafe actions and embedded-file structures reachable from a page."""
    if hasattr(value, "idnum") and hasattr(value, "generation"):
        key: tuple[object, ...] = ("ref", value.idnum, value.generation, action_context)
        if key in visited:
            return
        visited.add(key)
    elif isinstance(value, (dict, list, tuple)) or hasattr(value, "get_object"):
        key = ("obj", id(value), action_context)
        if key in visited:
            return
        visited.add(key)
    if hasattr(value, "get_object"):
        value = value.get_object()
    if isinstance(value, dict):
        if (
            any(k in value for k in ("/AF", "/EF", "/EmbeddedFiles", "/EmbeddedFile"))
            or _semantic_name(value, "/Type") in {"/Filespec", "/EmbeddedFile"}
            or _semantic_name(value, "/Subtype") == "/FileAttachment"
            or "/AA" in value
        ):
            raise PdfAppendixValidationError
        if action_context and not _action_is_allowed(value):
            raise PdfAppendixValidationError
        for key, child in value.items():
            _reject_embedded_page_objects(
                child,
                visited,
                action_context=isinstance(key, str)
                and key in {"/A", "/NA", "/PA", "/Next", "/OpenAction"},
            )
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_embedded_page_objects(child, visited, action_context=action_context)


def _reject_signed_or_interactive_objects(
    value: object,
    visited: set[tuple[object, ...]],
    *,
    document_info_context: bool = False,
    trailer_object: object | None = None,
) -> None:
    """Reject signature declarations and non-static media in the parsed PDF object graph."""
    if hasattr(value, "idnum") and hasattr(value, "generation"):
        key: tuple[object, ...] = (
            "ref",
            value.idnum,
            value.generation,
            document_info_context,
        )
        if key in visited:
            return
        visited.add(key)
    elif isinstance(value, (dict, list, tuple)) or hasattr(value, "get_object"):
        key = ("obj", id(value), document_info_context)
        if key in visited:
            return
        visited.add(key)
    if hasattr(value, "get_object"):
        value = value.get_object()
    if isinstance(value, dict):
        # pypdf resolves escaped PDF names (for example /Byte#52ange) before
        # exposing dictionary keys. Inspect semantic declarations, including
        # signatures referenced through catalog /Perms /DocMDP.
        # /Type and /Subtype are names in structural dictionaries, but the
        # trailer's /Info dictionary also permits custom text-valued keys with
        # those names. Keep strict typing for structure; treat only the actual
        # document information dictionary as user metadata.
        pdf_type = None if document_info_context else _semantic_name(value, "/Type")
        pdf_subtype = (
            None if document_info_context else _semantic_name(value, "/Subtype")
        )
        field_type = None if document_info_context else _semantic_name(value, "/FT")
        if (
            pdf_type == "/Sig"
            or field_type == "/Sig"
            or "/ByteRange" in value
            or "/DocMDP" in value
            or "/OnInstantiate" in value
            or pdf_type == "/3D"
            or pdf_subtype in {"/3D", "/RichMedia", "/Movie", "/Sound", "/Screen"}
            or any(
                key in value
                for key in ("/3DD", "/RichMediaContent", "/RichMediaSettings", "/Rendition")
            )
        ):
            raise PdfAppendixValidationError
        for entry_key, child in value.items():
            _reject_signed_or_interactive_objects(
                child,
                visited,
                document_info_context=value is trailer_object and entry_key == "/Info",
                trailer_object=trailer_object,
            )
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_signed_or_interactive_objects(child, visited)


def _validate_and_copy(data: bytes, mime_type: str, max_bytes: int, max_pages: int) -> ValidatedPdf:
    if mime_type.lower().split(";", 1)[0].strip() != "application/pdf":
        raise PdfAppendixValidationError
    if not data or len(data) > max_bytes or not data.startswith(b"%PDF-"):
        raise PdfAppendixValidationError
    if _SIGNATURE_MARKER.search(data):
        raise PdfAppendixValidationError
    source = io.BytesIO(data)
    reader: PdfReader | None = None
    writer: PdfWriter | None = None
    rewritten: _BoundedPdfBuffer | None = None
    try:
        reader = PdfReader(source, strict=True)
        if reader.is_encrypted:
            raise PdfAppendixValidationError
        pages = reader.pages
        count = len(pages)
        if count < 1 or count > max_pages:
            raise PdfAppendixValidationError
        root: Any = reader.trailer["/Root"]
        trailer: Any = reader.trailer
        _reject_signed_or_interactive_objects(trailer, set(), trailer_object=trailer)
        if any(key in root for key in ("/OpenAction", "/AA", "/AcroForm", "/XFA")):
            raise PdfAppendixValidationError
        names_obj = root.get("/Names")
        names = names_obj.get_object() if names_obj is not None else None
        if names is not None and any(key in names for key in ("/EmbeddedFiles", "/JavaScript")):
            raise PdfAppendixValidationError
        writer = PdfWriter()
        for page in pages:
            _reject_embedded_page_objects(page, set())
            if page.get("/AA") is not None:
                raise PdfAppendixValidationError
            annotations = page.get("/Annots", [])
            for annotation_ref in annotations:
                annotation = annotation_ref.get_object()
                if annotation.get("/Subtype") == "/FileAttachment":
                    raise PdfAppendixValidationError
                action = annotation.get("/A")
                if action is not None and not _action_is_allowed(action):
                    raise PdfAppendixValidationError
                additional = annotation.get("/AA")
                if additional:
                    raise PdfAppendixValidationError
            writer.add_page(page)
        rewritten = _BoundedPdfBuffer(max_bytes)
        writer.write(rewritten)
        if rewritten.tell() == 0:
            raise PdfAppendixValidationError
    except PdfAppendixValidationError:
        raise
    except Exception as exc:
        raise PdfAppendixValidationError from exc
    finally:
        if rewritten is not None:
            rewritten.close()
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        source.close()
    return ValidatedPdf(hashlib.sha256(data).hexdigest(), len(data), count)


async def validate_pdf(
    data: bytes, mime_type: str, *, max_bytes: int, max_pages: int
) -> ValidatedPdf:
    """Run synchronous parsing and safe page-copy validation with bounded concurrency."""
    await _pdf_semaphore.acquire()
    worker = asyncio.create_task(
        asyncio.to_thread(_validate_and_copy, data, mime_type, max_bytes, max_pages)
    )
    permit_handed_to_worker = False
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Repeated cancellation can interrupt any await in the caller. Transfer
        # permit release and result consumption to the worker's completion callback.
        permit_handed_to_worker = True

        def finish_cancelled_worker(completed: asyncio.Task[ValidatedPdf]) -> None:
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass
            finally:
                _pdf_semaphore.release()

        worker.add_done_callback(finish_cancelled_worker)
        raise
    finally:
        if not permit_handed_to_worker:
            _pdf_semaphore.release()


def normalized_filename(filename: str) -> str:
    """Remove path components and control characters from an untrusted filename."""
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(ch for ch in filename if ord(ch) >= 32 and ord(ch) != 127)
    return filename[:500] or "appendix.pdf"


def new_file_values(
    *, company_id: uuid.UUID, title: str, filename: str, content: bytes, validated: ValidatedPdf
) -> dict[str, object]:
    return {
        "company_id": company_id,
        "title": title.strip() or filename,
        "original_filename": filename,
        "pdf_bytes": content,
        "sha256": validated.sha256,
        "size_bytes": validated.size_bytes,
        "page_count": validated.page_count,
        "is_archived": False,
    }


async def list_library_files(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    search: str | None,
    include_archived: bool,
    limit: int,
    offset: int,
) -> PdfAppendixFileListResponse:
    """Return metadata-only rows; never select the database blob in a listing."""
    await set_rls_company(session, company_id)
    clauses = [PdfAppendixFile.company_id == company_id, PdfAppendixFile.owner_quote_id.is_(None)]
    if not include_archived:
        clauses.append(PdfAppendixFile.is_archived.is_(False))
    if search:
        clauses.append(PdfAppendixFile.title.ilike(f"%{search}%"))
    total = (
        await session.scalar(select(func.count()).select_from(PdfAppendixFile).where(*clauses)) or 0
    )
    rows = (
        (
            await session.execute(
                select(
                    PdfAppendixFile.id,
                    PdfAppendixFile.company_id,
                    PdfAppendixFile.owner_quote_id,
                    PdfAppendixFile.title,
                    PdfAppendixFile.original_filename,
                    PdfAppendixFile.sha256,
                    PdfAppendixFile.size_bytes,
                    PdfAppendixFile.page_count,
                    PdfAppendixFile.is_archived,
                    PdfAppendixFile.created_at,
                    PdfAppendixFile.updated_at,
                )
                .where(*clauses)
                .order_by(PdfAppendixFile.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    return PdfAppendixFileListResponse(
        items=[PdfAppendixFileRead.model_validate(dict(r)) for r in rows], total=total
    )


async def save_library_file(
    session: AsyncSession, *, company_id: uuid.UUID, values: dict[str, object]
) -> PdfAppendixFile:
    """Insert a validated immutable original and its metadata in one transaction."""
    await set_rls_company(session, company_id)
    row = PdfAppendixFile(**values)
    session.add(row)
    return row


async def update_library_file(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    file_id: uuid.UUID,
    data: PdfLibraryFileUpdate,
) -> PdfAppendixFile | None:
    """Lock and update only mutable library metadata."""
    await set_rls_company(session, company_id)
    row = await session.scalar(
        select(PdfAppendixFile)
        .where(
            PdfAppendixFile.id == file_id,
            PdfAppendixFile.company_id == company_id,
            PdfAppendixFile.owner_quote_id.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        return None
    if data.title is not None:
        row.title = data.title.strip()
    if data.is_archived is not None:
        row.is_archived = data.is_archived
    return row


async def get_file_content(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    file_id: uuid.UUID,
) -> tuple[bytes, str] | None:
    """Fetch only the original bytes and filename for an authenticated download."""
    await set_rls_company(session, company_id)
    result = await session.execute(
        select(
            PdfAppendixFile.pdf_bytes,
            PdfAppendixFile.original_filename,
        ).where(PdfAppendixFile.id == file_id, PdfAppendixFile.company_id == company_id)
    )
    row = result.first()
    return (row.pdf_bytes, row.original_filename) if row else None


async def read_file_metadata(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    file_id: uuid.UUID,
) -> PdfAppendixFileRead | None:
    """Fetch the public metadata projection without loading the original bytes."""
    await set_rls_company(session, company_id)
    result = await session.execute(
        select(
            PdfAppendixFile.id,
            PdfAppendixFile.owner_quote_id,
            PdfAppendixFile.title,
            PdfAppendixFile.original_filename,
            PdfAppendixFile.sha256,
            PdfAppendixFile.size_bytes,
            PdfAppendixFile.page_count,
            PdfAppendixFile.is_archived,
            PdfAppendixFile.created_at,
            PdfAppendixFile.updated_at,
        ).where(
            PdfAppendixFile.id == file_id,
            PdfAppendixFile.company_id == company_id,
        )
    )
    row = result.mappings().one_or_none()
    return PdfAppendixFileRead.model_validate(dict(row)) if row is not None else None


class PdfAppendixSaveError(RuntimeError):
    """A public-safe marker for transactional persistence failure."""


async def list_quote_files(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    quote_id: uuid.UUID,
    limit: int,
    offset: int,
) -> PdfAppendixFileListResponse | None:
    """List all originals owned by a persisted Quote, without loading blobs."""
    from jai.services.quote import resolve_quote_parent

    if await resolve_quote_parent(session, quote_id, company_id) is None:
        return None
    filters = (PdfAppendixFile.owner_quote_id == quote_id,)
    total = (
        await session.scalar(
            select(func.count()).select_from(PdfAppendixFile).where(*filters)
        )
        or 0
    )
    rows = (
        (
            await session.execute(
                select(PdfAppendixFile)
                .where(*filters)
                .order_by(PdfAppendixFile.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return PdfAppendixFileListResponse(
        items=[PdfAppendixFileRead.model_validate(row) for row in rows], total=total
    )


async def quote_upload_state(
    session: AsyncSession, *, company_id: uuid.UUID, quote_id: uuid.UUID
) -> QuoteStatus | None:
    """Resolve a Quote before an upload request consumes multipart bytes."""
    from jai.services.quote import resolve_quote_parent

    quote = await resolve_quote_parent(session, quote_id, company_id)
    return QuoteStatus(quote.status) if quote is not None else None


async def save_quote_file(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    quote_id: uuid.UUID,
    title: str,
    filename: str,
    content: bytes,
    validated: ValidatedPdf,
) -> PdfAppendixFileRead:
    """Lock and recheck Quote lifecycle before persisting an unselected own file."""
    from jai.services.quote import resolve_quote_parent

    quote = await resolve_quote_parent(session, quote_id, company_id, lock=True)
    if quote is None:
        await session.rollback()
        raise ValueError("PDF_APPENDIX_NOT_FOUND")
    if QuoteStatus(quote.status) == QuoteStatus.ACCEPTED:
        await session.rollback()
        raise ValueError("QUOTE_APPENDICES_READ_ONLY")
    row = PdfAppendixFile(
        owner_quote_id=quote_id,
        **new_file_values(
            company_id=company_id,
            title=title,
            filename=filename,
            content=content,
            validated=validated,
        ),
    )
    try:
        session.add(row)
        await session.flush()
        response = PdfAppendixFileRead.model_validate(row)
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        logging.getLogger("jai").error("PDF appendix own-file persistence failed")
        raise PdfAppendixSaveError from None
