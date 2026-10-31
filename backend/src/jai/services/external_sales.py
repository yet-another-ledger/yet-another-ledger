"""External sales source and pending draft operations."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import SettingLevel, VatTreatmentEffect, VatTreatmentSide
from jai.models.external_sales import (
    ExternalSale,
    ExternalSaleFile,
    ExternalSalePosting,
    ExternalSalesSource,
    ExternalSaleTax,
)
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.external_sales import (
    ExternalSalesCalculateRequest,
    ExternalSalesCalculation,
    ExternalSalesConfirmRequest,
    ExternalSalesFileRead,
    ExternalSalesInput,
    ExternalSalesListRead,
    ExternalSalesPostingRead,
    ExternalSalesRead,
    ExternalSalesSourceInput,
    ExternalSalesSourceListRead,
    ExternalSalesSourceRead,
    ExternalSalesStatus,
    ExternalSalesTaxInput,
    ExternalSalesTaxRead,
)
from jai.schemas.setting import SETTING_KEY_VAT_RATE_TIERS, VatRateTiers
from jai.services.receipt_inbox import MAX_FILES
from jai.services.reporting.btw import _classify_rate
from jai.services.settings import get_setting
from jai.services.storage import MIME_TO_EXT, ReceiptValidationError, Storage, validate_receipt

logger = logging.getLogger(__name__)


class ExternalSalesConflict(ValueError):
    """Stable state or duplicate conflict."""

    def __init__(
        self,
        code: str,
        *,
        external_sale_id: uuid.UUID | None = None,
        current_version: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.external_sale_id = external_sale_id
        self.current_version = current_version


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
        version=row.version,
        posted_at=row.posted_at,
        posted_by=row.posted_by,
        posted_source_name=row.posted_source_name,
        original_net_total=row.original_net_total,
        original_vat_total=row.original_vat_total,
        mismatch_ack_reason=row.mismatch_ack_reason,
        postings=[
            ExternalSalesPostingRead(
                id=p.id,
                version=p.version,
                actor_id=p.actor_id,
                created_at=p.created_at,
                snapshot=p.snapshot,
            )
            for p in row.postings
        ],
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
            selectinload(ExternalSale.postings),
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
                selectinload(ExternalSale.postings),
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
        raise ExternalSalesConflict(
            "EXTERNAL_SALE_NOT_PENDING", external_sale_id=row.id, current_version=row.version
        )
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
        row.version += 1
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
        raise ExternalSalesConflict(
            "EXTERNAL_SALE_NOT_PENDING", external_sale_id=row.id, current_version=row.version
        )
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
        raise ExternalSalesConflict(
            "EXTERNAL_SALE_NOT_PENDING", external_sale_id=row.id, current_version=row.version
        )
    if not 1 <= file_count <= MAX_FILES:
        raise ReceiptValidationError("Upload between one and five files.")
    if len(row.files) + file_count > MAX_FILES:
        raise ExternalSalesConflict(
            "FILE_LIMIT_REACHED", external_sale_id=row.id, current_version=row.version
        )


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
        row.version += 1
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
        raise ExternalSalesConflict(
            "EXTERNAL_SALE_NOT_PENDING", external_sale_id=row.id, current_version=row.version
        )
    file_row = next((f for f in row.files if f.id == file_id), None)
    if file_row is None:
        raise LookupError("File not found")
    key = file_row.storage_key
    row.files.remove(file_row)
    row.version += 1
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


def _check_posting_state(row: ExternalSale, expected_version: int) -> None:
    if row.status != "PENDING":
        raise ExternalSalesConflict(
            "EXTERNAL_SALE_ALREADY_POSTED", external_sale_id=row.id, current_version=row.version
        )
    if row.version != expected_version:
        raise ExternalSalesConflict(
            "EXTERNAL_SALE_VERSION_CONFLICT",
            external_sale_id=row.id,
            current_version=row.version,
        )


def _calculate_locked(
    row: ExternalSale, expected_version: int, tiers: VatRateTiers | None = None
) -> ExternalSalesCalculation:
    _check_posting_state(row, expected_version)
    tiers = tiers if tiers is not None else VatRateTiers()
    if (
        row.source_id is None
        or row.source is None
        or not row.original_number
        or not row.original_number.strip()
    ):
        raise ReceiptValidationError("Source and original number are required before posting.")
    if row.document_date is None or row.include_in_pl is None:
        raise ReceiptValidationError(
            "Document date and reviewed P/L inclusion are required before posting."
        )
    if (row.covered_from is None) != (row.covered_to is None):
        raise ReceiptValidationError("Provide both covered-period dates or neither.")
    if row.covered_from and row.covered_to and row.covered_from > row.covered_to:
        raise ReceiptValidationError("Covered-period start must not be after its end.")
    if not row.files:
        raise ReceiptValidationError("At least one original file is required before posting.")
    if not row.tax_summaries:
        raise ReceiptValidationError("At least one VAT summary is required before posting.")
    net_total = Decimal("0.00")
    vat_total = Decimal("0.00")
    derived_total = Decimal("0.00")
    mismatch_rows: list[int] = []
    warnings: list[str] = []
    for index, tax in enumerate(row.tax_summaries):
        if (
            tax.treatment_code != "NL_DOMESTIC"
            or tax.rate_percent is None
            or tax.net_amount is None
            or tax.vat_amount is None
        ):
            raise ReceiptValidationError(
                "Every VAT summary needs an eligible NL treatment, rate, net and VAT amount."
            )
        net = _amount(tax.net_amount)
        vat = _amount(tax.vat_amount)
        assert net is not None and vat is not None
        if net < 0 or vat < 0:
            raise ReceiptValidationError("Ordinary external sale amounts cannot be negative.")
        if _classify_rate(tax.rate_percent, tiers) == "zero" and vat != 0:
            raise ReceiptValidationError(
                f"Tax summary {index + 1}: the configured zero VAT rate tier cannot report "
                "nonzero original VAT; "
                "review the original document and correct the VAT rate or tax treatment."
            )
        rate_vat = _amount(net * Decimal(str(tax.rate_percent)) / Decimal("100"))
        assert rate_vat is not None
        if rate_vat != vat:
            mismatch_rows.append(index)
            warnings.append(
                f"Tax summary {index + 1}: original VAT {vat} differs from rate-derived VAT "
                f"{rate_vat}; verify the source document and acknowledge this row with a reason."
            )
        net_total += net
        vat_total += vat
        derived_total += rate_vat
    mismatch = bool(mismatch_rows)
    return ExternalSalesCalculation(
        sale_id=row.id,
        expected_version=row.version,
        original_net_total=net_total,
        original_vat_total=vat_total,
        derived_vat_total=derived_total,
        mismatch=mismatch,
        mismatch_row_indices=mismatch_rows,
        warnings=warnings,
    )


async def _calculate_for_posting(
    session: AsyncSession, company_id: uuid.UUID, row: ExternalSale, expected_version: int
) -> ExternalSalesCalculation:
    # Conflicts are independent of both settings and original-storage availability.
    _check_posting_state(row, expected_version)
    tiers = await get_setting(
        session,
        SETTING_KEY_VAT_RATE_TIERS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=VatRateTiers,
    )
    return _calculate_locked(row, expected_version, tiers)


async def calculate_sale(
    session: AsyncSession,
    company_id: uuid.UUID,
    sale_id: uuid.UUID,
    body: ExternalSalesCalculateRequest,
    storage: Storage,
) -> ExternalSalesCalculation:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id)
    calc = await _calculate_for_posting(session, company_id, row, body.expected_version)
    _validate_original_bytes(row, storage)
    return calc


async def confirm_sale(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    sale_id: uuid.UUID,
    body: ExternalSalesConfirmRequest,
    storage: Storage,
) -> ExternalSalesRead:
    await set_rls_company(session, company_id)
    row = await _owned_sale(session, company_id, sale_id, lock=True)
    calc = await _calculate_for_posting(session, company_id, row, body.expected_version)
    _validate_original_bytes(row, storage)
    reason = (body.mismatch_ack_reason or "").strip()
    if calc.mismatch and (not body.acknowledge_vat_mismatch or not reason):
        raise ReceiptValidationError(
            "Acknowledge the VAT mismatch and provide a reason before posting."
        )
    now = datetime.now(UTC)
    row.status = "POSTED"
    row.posted_at = now
    row.posted_by = actor_id
    row.posted_source_name = row.source.name if row.source else None
    row.original_net_total = calc.original_net_total
    row.original_vat_total = calc.original_vat_total
    row.mismatch_ack_reason = reason if calc.mismatch else None
    row.version += 1
    snapshot = {
        "id": str(row.id),
        "source_id": str(row.source_id),
        "source_name": row.source.name if row.source else None,
        "original_number": row.original_number,
        "document_date": row.document_date.isoformat() if row.document_date else None,
        "covered_from": row.covered_from.isoformat() if row.covered_from else None,
        "covered_to": row.covered_to.isoformat() if row.covered_to else None,
        "counterparty": row.counterparty,
        "description": row.description,
        "include_in_pl": row.include_in_pl,
        "original_net_total": str(calc.original_net_total),
        "original_vat_total": str(calc.original_vat_total),
        "derived_vat_total": str(calc.derived_vat_total),
        "mismatch_ack_reason": row.mismatch_ack_reason,
        "tax_summaries": [
            {
                "treatment_code": t.treatment_code,
                "treatment_label": t.treatment_label,
                "rate_label": t.rate_label,
                "rate_percent": str(t.rate_percent),
                "net_amount": str(_amount(t.net_amount) or 0),
                "vat_amount": str(_amount(t.vat_amount) or 0),
            }
            for t in row.tax_summaries
        ],
        "originals": [
            {
                "id": str(f.id),
                "filename": f.filename,
                "sha256": f.sha256,
                "mime_type": f.mime_type,
                "byte_size": f.byte_size,
            }
            for f in row.files
        ],
    }
    row.postings.append(
        ExternalSalePosting(
            company_id=company_id,
            version=row.version,
            actor_id=actor_id,
            snapshot=snapshot,
        )
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return await get_sale(session, company_id, sale_id)


def _validate_original_bytes(row: ExternalSale, storage: Storage) -> None:
    """Require each retained original's controlled object to still match metadata."""
    for file_row in row.files:
        try:
            content = storage.open(file_row.storage_key)
        except (FileNotFoundError, OSError) as exc:
            raise ReceiptValidationError(
                "An original file is unavailable; upload it again."
            ) from exc
        if (
            len(content) != file_row.byte_size
            or hashlib.sha256(content).hexdigest() != file_row.sha256
        ):
            raise ReceiptValidationError("An original file failed its integrity check.")
