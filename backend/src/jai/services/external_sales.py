"""External sales source and pending draft operations."""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import VatTreatmentEffect, VatTreatmentSide
from jai.models.external_sales import (
    ExternalSale,
    ExternalSaleFile,
    ExternalSalesSource,
    ExternalSaleTax,
)
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.external_sales import (
    ExternalSalesFileRead,
    ExternalSalesInput,
    ExternalSalesListRead,
    ExternalSalesRead,
    ExternalSalesSourceInput,
    ExternalSalesSourceListRead,
    ExternalSalesSourceRead,
    ExternalSalesStatus,
    ExternalSalesTaxInput,
    ExternalSalesTaxRead,
)
from jai.services.receipt_inbox import MAX_FILES
from jai.services.storage import MIME_TO_EXT, ReceiptValidationError, Storage, validate_receipt

logger = logging.getLogger(__name__)


class ExternalSalesConflict(ValueError):
    """Stable state or duplicate conflict."""


def _source_read(row: ExternalSalesSource) -> ExternalSalesSourceRead:
    return ExternalSalesSourceRead.model_validate(row, from_attributes=True)


def _sale_read(row: ExternalSale) -> ExternalSalesRead:
    return ExternalSalesRead(
        id=row.id,
        source_id=row.source_id,
        source_name=row.source.name if row.source else None,
        original_number=row.original_number,
        document_date=row.document_date,
        covered_from=row.covered_from,
        covered_to=row.covered_to,
        counterparty=row.counterparty,
        description=row.description,
        include_in_pl=row.include_in_pl,
        status=ExternalSalesStatus(row.status),
        tax_summaries=[
            ExternalSalesTaxRead.model_validate(t, from_attributes=True) for t in row.tax_summaries
        ],
        files=[
            ExternalSalesFileRead(
                id=f.id,
                filename=f.filename,
                mime_type=f.mime_type,
                byte_size=f.byte_size,
                sha256=f.sha256,
                duplicate_warning=f.duplicate_warning,
                created_at=f.created_at,
            )
            for f in row.files
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_sources(
    session: AsyncSession, company_id: uuid.UUID, limit: int, offset: int
) -> ExternalSalesSourceListRead:
    await set_rls_company(session, company_id)
    total = int(await session.scalar(select(func.count()).select_from(ExternalSalesSource)) or 0)
    rows = (
        await session.scalars(
            select(ExternalSalesSource)
            .order_by(ExternalSalesSource.name, ExternalSalesSource.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ExternalSalesSourceListRead(
        items=[_source_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


async def create_source(
    session: AsyncSession, company_id: uuid.UUID, body: ExternalSalesSourceInput
) -> ExternalSalesSourceRead:
    await set_rls_company(session, company_id)
    values = body.model_dump()
    if values["default_include_in_pl"] is None:
        values["default_include_in_pl"] = {"PLATFORM": True, "ENERGY": False}.get(values["kind"])
    row = ExternalSalesSource(company_id=company_id, **values)
    session.add(row)
    await session.commit()
    await set_rls_company(session, company_id)
    saved = await session.scalar(
        select(ExternalSalesSource).where(ExternalSalesSource.id == row.id)
    )
    assert saved is not None
    return _source_read(saved)


async def update_source(
    session: AsyncSession,
    company_id: uuid.UUID,
    source_id: uuid.UUID,
    body: ExternalSalesSourceInput,
) -> ExternalSalesSourceRead:
    await set_rls_company(session, company_id)
    row = await session.scalar(
        select(ExternalSalesSource).where(ExternalSalesSource.id == source_id)
    )
    if row is None:
        raise LookupError("Source not found")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    if body.default_include_in_pl is None:
        row.default_include_in_pl = {"PLATFORM": True, "ENERGY": False}.get(body.kind)
    await session.commit()
    await set_rls_company(session, company_id)
    saved = await session.scalar(
        select(ExternalSalesSource).where(ExternalSalesSource.id == source_id)
    )
    assert saved is not None
    return _source_read(saved)


def _amount(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if value is not None else None


async def _replace_tax_rows(
    session: AsyncSession, sale: ExternalSale, rows: list[ExternalSalesTaxInput]
) -> None:
    sale.tax_summaries.clear()
    for row in rows:
        treatment = None
        rate = None
        if row.treatment_id is not None:
            treatment = await session.scalar(
                select(VatTreatment).where(VatTreatment.id == row.treatment_id)
            )
            if (
                treatment is None
                or treatment.company_id != sale.company_id
                or treatment.side != VatTreatmentSide.SALES
                or treatment.code != "NL_DOMESTIC"
                or treatment.effect != VatTreatmentEffect.APPLY_RATE
                or treatment.requires_icp
            ):
                raise ReceiptValidationError("Only NL domestic ordinary sales VAT is supported.")
        if row.rate_id is not None:
            rate = await session.scalar(select(VatRate).where(VatRate.id == row.rate_id))
            if rate is None or rate.company_id != sale.company_id or not rate.active:
                raise LookupError("Active VAT rate not found")
        sale.tax_summaries.append(
            ExternalSaleTax(
                company_id=sale.company_id,
                treatment_id=treatment.id if treatment else None,
                treatment_code=treatment.code if treatment else None,
                treatment_label=treatment.label if treatment else None,
                rate_id=rate.id if rate else None,
                rate_label=rate.label if rate else None,
                rate_percent=rate.percent if rate else None,
                net_amount=_amount(row.net_amount),
                vat_amount=_amount(row.vat_amount),
            )
        )


async def _source_for_entry(
    session: AsyncSession, company_id: uuid.UUID, source_id: uuid.UUID | None
) -> ExternalSalesSource | None:
    if source_id is None:
        return None
    row = await session.scalar(
        select(ExternalSalesSource).where(
            ExternalSalesSource.id == source_id, ExternalSalesSource.active.is_(True)
        )
    )
    if row is None:
        raise LookupError("Active source not found")
    if row.company_id != company_id:
        raise LookupError("Source not found")
    return row


async def create_sale(
    session: AsyncSession, company_id: uuid.UUID, creator_id: uuid.UUID, body: ExternalSalesInput
) -> ExternalSalesRead:
    await set_rls_company(session, company_id)
    source = await _source_for_entry(session, company_id, body.source_id)
    values = body.model_dump(exclude={"tax_summaries"})
    values["original_number"] = body.original_number
    if values["include_in_pl"] is None and source is not None:
        values["include_in_pl"] = source.default_include_in_pl
    row = ExternalSale(
        company_id=company_id,
        creator_id=creator_id,
        status="PENDING",
        tax_summaries=[],
        files=[],
        **values,
    )
    session.add(row)
    try:
        await session.flush()
        await _replace_tax_rows(session, row, body.tax_summaries)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ExternalSalesConflict("DUPLICATE_SOURCE_NUMBER") from exc
    return await get_sale(session, company_id, row.id)


async def _owned_sale(
    session: AsyncSession, company_id: uuid.UUID, sale_id: uuid.UUID, *, lock: bool = False
) -> ExternalSale:
    stmt = (
        select(ExternalSale)
        .options(
            selectinload(ExternalSale.tax_summaries),
            selectinload(ExternalSale.files),
            selectinload(ExternalSale.source),
        )
        .where(ExternalSale.id == sale_id)
    )
    if lock:
        # A preflight may already have loaded children in this session. Reload
        # them after taking the parent lock so concurrent appends are counted.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(stmt)
    if row is None or row.company_id != company_id:
        raise LookupError("External sale not found")
    return row


async def get_sale(
    session: AsyncSession, company_id: uuid.UUID, sale_id: uuid.UUID
) -> ExternalSalesRead:
    await set_rls_company(session, company_id)
    return _sale_read(await _owned_sale(session, company_id, sale_id))


async def list_sales(
    session: AsyncSession, company_id: uuid.UUID, limit: int, offset: int
) -> ExternalSalesListRead:
    await set_rls_company(session, company_id)
    total = int(await session.scalar(select(func.count()).select_from(ExternalSale)) or 0)
    rows = (
        await session.scalars(
            select(ExternalSale)
            .options(
                selectinload(ExternalSale.tax_summaries),
                selectinload(ExternalSale.files),
                selectinload(ExternalSale.source),
            )
            .order_by(ExternalSale.created_at.desc(), ExternalSale.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ExternalSalesListRead(
        items=[_sale_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


async def update_sale(
    session: AsyncSession, company_id: uuid.UUID, sale_id: uuid.UUID, body: ExternalSalesInput
) -> ExternalSalesRead:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id, lock=True)
    if row.status != "PENDING":
        raise ExternalSalesConflict("EXTERNAL_SALE_NOT_PENDING")
    values = body.model_dump(exclude={"tax_summaries"})
    source_changed = "source_id" in body.model_fields_set and body.source_id != row.source_id
    if source_changed:
        await _source_for_entry(session, company_id, body.source_id)
    if "include_in_pl" not in body.model_fields_set:
        values["include_in_pl"] = row.include_in_pl
    values["original_number"] = body.original_number
    try:
        for key, value in values.items():
            setattr(row, key, value)
        # Dictionary lookups may autoflush header changes, including its unique number.
        await _replace_tax_rows(session, row, body.tax_summaries)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ExternalSalesConflict("DUPLICATE_SOURCE_NUMBER") from exc
    return await get_sale(session, company_id, sale_id)


async def discard_sale(
    session: AsyncSession, company_id: uuid.UUID, sale_id: uuid.UUID, storage: Storage
) -> None:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id, lock=True)
    if row.status != "PENDING":
        raise ExternalSalesConflict("EXTERNAL_SALE_NOT_PENDING")
    keys = [file.storage_key for file in row.files]
    await session.delete(row)
    await session.commit()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            logger.exception("Could not remove discarded external sale original")


def _normalize_uploads(
    parts: list[tuple[bytes, str, str | None]], max_bytes: int
) -> list[tuple[bytes, str, str | None]]:
    if not 1 <= len(parts) <= MAX_FILES:
        raise ReceiptValidationError("Upload between one and five files.")
    output = []
    for content, mime, filename in parts:
        mime = mime.split(";")[0].strip().lower()
        validate_receipt(content, mime, max_bytes)
        output.append((content, mime, filename))
    return output


async def check_append_capacity(
    session: AsyncSession, company_id: uuid.UUID, sale_id: uuid.UUID, file_count: int
) -> None:
    """Check ownership/state/capacity before the API reads multipart file bytes.

    append_files repeats these checks under the parent lock after the read.
    """
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id)
    _check_append(row, file_count)


def _check_append(row: ExternalSale, file_count: int) -> None:
    if row.status != "PENDING":
        raise ExternalSalesConflict("EXTERNAL_SALE_NOT_PENDING")
    if not 1 <= file_count <= MAX_FILES:
        raise ReceiptValidationError("Upload between one and five files.")
    if len(row.files) + file_count > MAX_FILES:
        raise ExternalSalesConflict("FILE_LIMIT_REACHED")


async def append_files(
    session: AsyncSession,
    company_id: uuid.UUID,
    creator_id: uuid.UUID,
    sale_id: uuid.UUID,
    parts: list[tuple[bytes, str, str | None]],
    max_bytes: int,
    storage: Storage,
) -> ExternalSalesRead:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id, lock=True)
    _check_append(row, len(parts))
    normalized = _normalize_uploads(parts, max_bytes)
    written: list[str] = []
    existing_hashes = set((await session.scalars(select(ExternalSaleFile.sha256))).all())
    try:
        for content, mime, filename in normalized:
            file_id = uuid.uuid4()
            key = f"{company_id}/{sale_id}/{file_id}.{MIME_TO_EXT[mime]}"
            # Track controlled key before save because save may fail after writing bytes.
            written.append(f"external-sales/{key}")
            stored = storage.save("external-sales", key, content, mime)
            written[-1] = stored
            digest = hashlib.sha256(content).hexdigest()
            row.files.append(
                ExternalSaleFile(
                    company_id=company_id,
                    id=file_id,
                    storage_key=stored,
                    filename=filename,
                    mime_type=mime,
                    byte_size=len(content),
                    sha256=digest,
                    duplicate_warning=digest in existing_hashes,
                    creator_id=creator_id,
                )
            )
            existing_hashes.add(digest)
        await session.commit()
    except Exception:
        await session.rollback()
        for key in written:
            try:
                storage.delete(key)
            except Exception:
                logger.exception("Could not compensate external sale upload")
        raise
    return await get_sale(session, company_id, sale_id)


async def remove_file(
    session: AsyncSession,
    company_id: uuid.UUID,
    sale_id: uuid.UUID,
    file_id: uuid.UUID,
    storage: Storage,
) -> ExternalSalesRead:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id, lock=True)
    if row.status != "PENDING":
        raise ExternalSalesConflict("EXTERNAL_SALE_NOT_PENDING")
    file_row = next((f for f in row.files if f.id == file_id), None)
    if file_row is None:
        raise LookupError("File not found")
    key = file_row.storage_key
    row.files.remove(file_row)
    await session.commit()
    try:
        storage.delete(key)
    except Exception:
        logger.exception("Could not remove external sale original")
    return await get_sale(session, company_id, sale_id)


async def read_file(
    session: AsyncSession,
    company_id: uuid.UUID,
    sale_id: uuid.UUID,
    file_id: uuid.UUID,
    storage: Storage,
) -> tuple[ExternalSaleFile, bytes]:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id)
    file_row = next((f for f in row.files if f.id == file_id), None)
    if file_row is None:
        raise LookupError("File not found")
    return file_row, storage.open(file_row.storage_key)
