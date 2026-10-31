"""Battery registration and annual private-use VAT snapshots."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import ExpenseKind
from jai.models.battery import (
    BatteryAnnualConfirmation,
    BatteryAnnualEvidence,
    BatteryAnnualUse,
    BatteryAsset,
)
from jai.schemas.battery import (
    BatteryAnnualCalculation,
    BatteryAnnualConfirmRequest,
    BatteryAnnualUseInput,
    BatteryAnnualUseRead,
    BatteryAssetCreateRequest,
    BatteryAssetListRead,
    BatteryAssetPreview,
    BatteryAssetPreviewRequest,
    BatteryAssetRead,
    BatteryConflictCode,
    BatteryEvidenceRead,
)
from jai.services.expense import _load_expense
from jai.services.money import quantize_to_minor_unit
from jai.services.storage import MIME_TO_EXT, Storage, validate_receipt

logger = logging.getLogger(__name__)


class BatteryConflict(ValueError):
    def __init__(self, code: str, current_version: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_version = current_version


def compute_battery_adjustment(
    net_basis: Decimal, vat_rate_percent: Decimal, private_use_percentage: Decimal
) -> tuple[Decimal, Decimal]:
    """Compute the Q4 base and VAT, rounding only after both use unrounded base."""
    unrounded_base = net_basis / Decimal("5") * private_use_percentage / Decimal("100")
    unrounded_vat = unrounded_base * vat_rate_percent / Decimal("100")
    return quantize_to_minor_unit(unrounded_base), quantize_to_minor_unit(unrounded_vat)


def _years(start: date) -> list[int]:
    return list(range(start.year, start.year + 5))


def _periods_through_today(start: date) -> list[str]:
    today = date.today()
    result: list[str] = []
    cursor = (start.year, (start.month - 1) // 3 + 1)
    end = (today.year, (today.month - 1) // 3 + 1)
    while cursor <= end:
        result.append(f"{cursor[0]}-Q{cursor[1]}")
        year, quarter = cursor
        cursor = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return result


async def preview_asset(
    session: AsyncSession, company_id: uuid.UUID, body: BatteryAssetPreviewRequest
) -> BatteryAssetPreview:
    await set_rls_company(session, company_id)
    expense = await _load_expense(session, body.acquisition_expense_id, company_id)
    warning: list[str] = []
    conflict: BatteryConflictCode | None = None
    if not body.owner_confirms_ordinary_vat_full_deduction:
        conflict = BatteryConflictCode.OWNER_CONFIRMATION_REQUIRED
    elif (
        ExpenseKind(expense.kind) != ExpenseKind.PURCHASE
        or expense.is_draft
        or expense.vat_treatment_code != "NL_DOMESTIC_PURCH"
        or expense.vat_treatment_effect != "APPLY_RATE"
        or not expense.deductible
        or Decimal(str(expense.base_vat_amount)) <= 0
        or Decimal(str(expense.vat_rate_percent)) <= 0
    ):
        conflict = BatteryConflictCode.EXPENSE_INELIGIBLE
    exists = await session.scalar(
        select(BatteryAsset.id).where(
            BatteryAsset.acquisition_expense_id == expense.id,
        )
    )
    if exists is not None:
        conflict = BatteryConflictCode.ACQUISITION_ALREADY_REGISTERED
    if body.in_service_date < expense.expense_date:
        conflict = BatteryConflictCode.EXPENSE_INELIGIBLE
        warning.append("The in-service date cannot precede the acquisition date.")
    periods = _periods_through_today(expense.expense_date)
    if expense.expense_date.year < date.today().year:
        warning.append("Historical report periods will be affected by this registration.")
    return BatteryAssetPreview(
        acquisition_expense_id=expense.id,
        eligible=conflict is None,
        conflict_code=conflict,
        in_service_date=body.in_service_date,
        applicable_years=_years(body.in_service_date),
        affected_historical_periods=periods if expense.expense_date < date.today() else [],
        net_basis=Decimal(str(expense.base_net_amount)),
        purchase_vat=Decimal(str(expense.base_vat_amount)),
        purchase_vat_rate_percent=Decimal(str(expense.vat_rate_percent)),
        warnings=warning,
    )


def _asset_read(row: BatteryAsset) -> BatteryAssetRead:
    return BatteryAssetRead(
        id=row.id,
        acquisition_expense_id=row.acquisition_expense_id,
        acquisition_expense_date=row.acquisition_expense_date,
        in_service_date=row.in_service_date,
        applicable_years=_years(row.in_service_date),
        net_basis=Decimal(str(row.net_basis)),
        purchase_vat=Decimal(str(row.purchase_vat)),
        purchase_vat_rate_percent=Decimal(str(row.purchase_vat_rate_percent)),
        currency=row.currency,
        rule_version=row.rule_version,
        version=row.version,
        ordinary_vat_full_deduction_confirmed=row.ordinary_vat_full_deduction_confirmed,
        owner_confirmed_at=row.owner_confirmed_at,
        registered_by=row.registered_by,
        historical_acknowledged_periods=row.historical_acknowledged_periods,
        historical_acknowledged_at=row.historical_acknowledged_at,
        created_at=row.created_at,
    )


async def register_asset(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: BatteryAssetCreateRequest,
) -> BatteryAssetRead:
    await set_rls_company(session, company_id)
    expense = await _load_expense(
        session, body.acquisition_expense_id, company_id, for_update=True
    )
    # Eligibility and association must be checked after acquiring the same row
    # lock used by generic Expense edit/delete, never from an earlier preview.
    preview = await preview_asset(session, company_id, body)
    if not preview.eligible:
        raise BatteryConflict(preview.conflict_code or "BATTERY_EXPENSE_INELIGIBLE")
    if preview.affected_historical_periods and not body.acknowledge_affected_periods:
        raise BatteryConflict("BATTERY_HISTORICAL_ACK_REQUIRED")
    expense.include_in_pl = False
    registered_at = datetime.now(UTC)
    acknowledged_periods = (
        preview.affected_historical_periods if body.acknowledge_affected_periods else []
    )
    row = BatteryAsset(
        company_id=company_id,
        acquisition_expense_id=expense.id,
        acquisition_expense_date=expense.expense_date,
        in_service_date=body.in_service_date,
        net_basis=expense.base_net_amount,
        purchase_vat=expense.base_vat_amount,
        purchase_vat_rate_percent=expense.vat_rate_percent,
        currency=expense.currency,
        rule_version=1,
        version=1,
        ordinary_vat_full_deduction_confirmed=True,
        owner_confirmed_at=registered_at,
        registered_by=actor_id,
        historical_acknowledged_periods=acknowledged_periods,
        historical_acknowledged_at=registered_at if acknowledged_periods else None,
    )
    session.add(row)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise BatteryConflict("BATTERY_ACQUISITION_ALREADY_REGISTERED") from exc
    await set_rls_company(session, company_id)
    await session.refresh(row)
    return _asset_read(row)


async def list_assets(
    session: AsyncSession, company_id: uuid.UUID, limit: int, offset: int
) -> BatteryAssetListRead:
    await set_rls_company(session, company_id)
    total = int(await session.scalar(select(func.count()).select_from(BatteryAsset)) or 0)
    rows = (
        await session.scalars(
            select(BatteryAsset)
            .order_by(BatteryAsset.created_at.desc(), BatteryAsset.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return BatteryAssetListRead(
        items=[_asset_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


async def get_asset(
    session: AsyncSession, company_id: uuid.UUID, asset_id: uuid.UUID
) -> BatteryAssetRead:
    await set_rls_company(session, company_id)
    row = await session.scalar(select(BatteryAsset).where(BatteryAsset.id == asset_id))
    if row is None:
        raise LookupError("Battery asset not found.")
    return _asset_read(row)


async def get_annual_use(
    session: AsyncSession, company_id: uuid.UUID, asset_id: uuid.UUID, year: int
) -> BatteryAnnualUseRead | None:
    await set_rls_company(session, company_id)
    asset = await session.scalar(select(BatteryAsset).where(BatteryAsset.id == asset_id))
    if asset is None:
        raise LookupError("Battery asset not found.")
    if year not in _years(asset.in_service_date):
        raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
    row = await session.scalar(
        select(BatteryAnnualUse)
        .options(selectinload(BatteryAnnualUse.evidence))
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
    )
    if row is None:
        return None
    return BatteryAnnualUseRead(
        id=row.id,
        asset_id=row.asset_id,
        year=row.year,
        version=row.version,
        private_use_percentage=row.private_use_percentage,
        basis_note=row.basis_note,
        confirmed=row.confirmed,
        confirmed_at=row.confirmed_at,
        calculation_base=row.calculation_base,
        calculation_vat=row.calculation_vat,
        asset_version_used=row.asset_version_used,
        evidence=[
            BatteryEvidenceRead(
                id=item.id,
                filename=item.filename,
                media_type=item.media_type,
                byte_size=item.byte_size,
                sha256=item.sha256,
                created_at=item.created_at,
            )
            for item in row.evidence
        ],
    )


async def put_annual_use(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualUseInput,
) -> BatteryAnnualUseRead:
    await set_rls_company(session, company_id)
    asset = await session.scalar(
        select(BatteryAsset).where(BatteryAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise LookupError("Battery asset not found.")
    if year not in _years(asset.in_service_date):
        raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
    row = await session.scalar(
        select(BatteryAnnualUse)
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
        .with_for_update()
    )
    if row is None:
        if body.expected_version is not None:
            raise BatteryConflict("BATTERY_VERSION_CONFLICT")
        row = BatteryAnnualUse(company_id=company_id, asset_id=asset_id, year=year, version=1)
        session.add(row)
    else:
        if body.expected_version != row.version:
            raise BatteryConflict("BATTERY_VERSION_CONFLICT", row.version)
        if row.confirmed:
            raise BatteryConflict("BATTERY_ANNUAL_CONFIRMED", row.version)
        row.version += 1
    row.private_use_percentage = body.private_use_percentage
    row.basis_note = body.basis_note
    row.confirmed = False
    row.confirmed_at = None
    row.calculation_base = None
    row.calculation_vat = None
    row.asset_version_used = None
    await session.flush()
    await session.commit()
    return await get_annual_use(session, company_id, asset_id, year)  # type: ignore[return-value]


async def calculate_annual_use(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    expected_version: int,
) -> BatteryAnnualCalculation:
    annual = await get_annual_use(session, company_id, asset_id, year)
    if annual is None:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    if annual.version != expected_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", annual.version)
    if annual.private_use_percentage is None or not annual.basis_note:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    asset = await session.scalar(select(BatteryAsset).where(BatteryAsset.id == asset_id))
    assert asset is not None
    base, vat = compute_battery_adjustment(
        Decimal(str(asset.net_basis)),
        Decimal(str(asset.purchase_vat_rate_percent)),
        annual.private_use_percentage,
    )
    return BatteryAnnualCalculation(
        asset_id=asset_id,
        year=year,
        expected_version=annual.version,
        private_use_percentage=annual.private_use_percentage,
        calculation_base=base,
        calculation_vat=vat,
        report_quarter=4,
    )


async def confirm_annual_use(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    actor_id: uuid.UUID,
    body: BatteryAnnualConfirmRequest,
) -> BatteryAnnualUseRead:
    await set_rls_company(session, company_id)
    asset = await session.scalar(
        select(BatteryAsset).where(BatteryAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise LookupError("Battery asset not found.")
    calculation = await calculate_annual_use(
        session, company_id, asset_id, year, body.expected_version
    )
    row = await session.scalar(
        select(BatteryAnnualUse)
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
        .with_for_update()
    )
    assert row is not None and row.private_use_percentage is not None
    if row.confirmed:
        raise BatteryConflict("BATTERY_ANNUAL_ALREADY_CONFIRMED", row.version)
    row.confirmed = True
    row.confirmed_at = datetime.now().astimezone()
    row.calculation_base = calculation.calculation_base
    row.calculation_vat = calculation.calculation_vat
    row.asset_version_used = asset.version
    session.add(
        BatteryAnnualConfirmation(
            company_id=company_id,
            annual_use_id=row.id,
            version=row.version,
            actor_id=actor_id,
            private_use_percentage=row.private_use_percentage,
            calculation_base=calculation.calculation_base,
            calculation_vat=calculation.calculation_vat,
            asset_version_used=asset.version,
        )
    )
    await session.commit()
    return await get_annual_use(session, company_id, asset_id, year)  # type: ignore[return-value]


async def add_annual_evidence(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    content: bytes,
    mime_type: str,
    filename: str | None,
    storage: Storage,
    max_bytes: int,
) -> BatteryEvidenceRead:
    await set_rls_company(session, company_id)
    row = await session.scalar(
        select(BatteryAnnualUse)
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
        .with_for_update()
    )
    if row is None:
        raise LookupError("Battery annual input not found.")
    if row.confirmed:
        raise BatteryConflict("BATTERY_ANNUAL_CONFIRMED", row.version)
    clean_mime = mime_type.split(";")[0].strip().lower()
    validate_receipt(content, clean_mime, max_bytes)
    evidence_id = uuid.uuid4()
    extension = MIME_TO_EXT[clean_mime]
    relative_key = f"{company_id}/{asset_id}/{year}/{evidence_id}.{extension}"
    storage_key = storage.save("battery-evidence", relative_key, content, clean_mime)
    evidence = BatteryAnnualEvidence(
        id=evidence_id,
        company_id=company_id,
        annual_use_id=row.id,
        storage_key=storage_key,
        filename=filename,
        media_type=clean_mime,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    session.add(evidence)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        try:
            storage.delete(storage_key)
        except Exception:
            logger.exception("Failed to clean up uncommitted battery evidence %s", evidence_id)
        raise
    # Metadata now owns the bytes. A subsequent read/context failure cannot be
    # compensated by deleting a file whose ownership has already committed.
    await set_rls_company(session, company_id)
    await session.refresh(evidence)
    return BatteryEvidenceRead(
        id=evidence.id,
        filename=evidence.filename,
        media_type=evidence.media_type,
        byte_size=evidence.byte_size,
        sha256=evidence.sha256,
        created_at=evidence.created_at,
    )


async def validate_annual_evidence_upload(
    session: AsyncSession, company_id: uuid.UUID, asset_id: uuid.UUID, year: int
) -> None:
    """Check tenant ownership and draft state before the API reads file bytes."""
    annual = await get_annual_use(session, company_id, asset_id, year)
    if annual is None:
        raise LookupError("Battery annual input not found.")
    if annual.confirmed:
        raise BatteryConflict("BATTERY_ANNUAL_CONFIRMED", annual.version)


async def read_annual_evidence(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    evidence_id: uuid.UUID,
    storage: Storage,
) -> tuple[bytes, BatteryEvidenceRead]:
    # Resolve the tenant-owned annual record and evidence metadata before any
    # storage access, so a cross-company id never probes the file store.
    await set_rls_company(session, company_id)
    annual = await session.scalar(
        select(BatteryAnnualUse).where(
            BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year
        )
    )
    if annual is None:
        raise LookupError("Battery annual input not found.")
    evidence = await session.scalar(
        select(BatteryAnnualEvidence).where(
            BatteryAnnualEvidence.id == evidence_id,
            BatteryAnnualEvidence.annual_use_id == annual.id,
        )
    )
    if evidence is None:
        raise LookupError("Battery evidence not found.")
    content = storage.open(evidence.storage_key)
    return content, BatteryEvidenceRead(
        id=evidence.id,
        filename=evidence.filename,
        media_type=evidence.media_type,
        byte_size=evidence.byte_size,
        sha256=evidence.sha256,
        created_at=evidence.created_at,
    )
