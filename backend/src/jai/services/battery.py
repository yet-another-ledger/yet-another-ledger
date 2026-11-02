"""Battery registration and annual private-use VAT snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import ExpenseKind
from jai.models.battery import (
    BatteryAnnualConfirmation,
    BatteryAnnualEvidence,
    BatteryAnnualRevision,
    BatteryAnnualUse,
    BatteryAsset,
    BatteryAssetRevision,
)
from jai.models.expense import Expense
from jai.models.vat import VatRate
from jai.schemas.battery import (
    BatteryAnnualCalculation,
    BatteryAnnualConfirmRequest,
    BatteryAnnualRevisionInput,
    BatteryAnnualRevisionListRead,
    BatteryAnnualRevisionPreview,
    BatteryAnnualRevisionPreviewItem,
    BatteryAnnualRevisionRead,
    BatteryAnnualUseInput,
    BatteryAnnualUseRead,
    BatteryAssetCreateRequest,
    BatteryAssetListRead,
    BatteryAssetPreview,
    BatteryAssetPreviewRequest,
    BatteryAssetRead,
    BatteryAssetRevisionInput,
    BatteryAssetRevisionListRead,
    BatteryAssetRevisionPreview,
    BatteryAssetRevisionRead,
    BatteryConflictCode,
    BatteryEvidenceRead,
    BatteryExpenseFinancialSnapshot,
)
from jai.services.expense import _load_expense, compute_expense_amounts
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


def _quarter_label(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def _asset_periods(
    old_date: date, new_date: date, old_service: date, new_service: date
) -> list[str]:
    years = set(_years(old_service)) | set(_years(new_service))
    return sorted(
        {_quarter_label(old_date), _quarter_label(new_date)} | {f"{year}-Q4" for year in years}
    )


def _expense_financial_snapshot(expense: Expense) -> BatteryExpenseFinancialSnapshot:
    return BatteryExpenseFinancialSnapshot(
        expense_date=expense.expense_date,
        currency=expense.currency,
        exchange_rate=Decimal(str(expense.exchange_rate)),
        net_amount=Decimal(str(expense.net_amount)),
        vat_amount=Decimal(str(expense.vat_amount)),
        gross_amount=Decimal(str(expense.gross_amount)),
        base_net_amount=Decimal(str(expense.base_net_amount)),
        base_vat_amount=Decimal(str(expense.base_vat_amount)),
        base_gross_amount=Decimal(str(expense.base_gross_amount)),
        vat_rate_id=expense.vat_rate_id,
        vat_rate_percent=Decimal(str(expense.vat_rate_percent)),
        vat_rate_label=expense.vat_rate_label,
    )


def _snapshot(model: BaseModel) -> dict[str, object]:
    # Pydantic's JSON mode converts UUID/date/Decimal to JSONB-safe values.
    return model.model_dump(mode="json")


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


async def _asset_revision_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    lock: bool,
) -> tuple[BatteryAsset, Expense, list[BatteryAnnualUse]]:
    """Acquire linked rows in the Expense → asset → annual-year order."""
    probe = await session.scalar(select(BatteryAsset).where(BatteryAsset.id == asset_id))
    if probe is None:
        raise LookupError("Battery asset not found.")
    expense = await _load_expense(
        session, probe.acquisition_expense_id, company_id, for_update=lock
    )
    stmt = select(BatteryAsset).where(BatteryAsset.id == asset_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    asset = await session.scalar(stmt)
    if asset is None:
        raise LookupError("Battery asset not found.")
    if asset.acquisition_expense_id != expense.id:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    annual_stmt = (
        select(BatteryAnnualUse)
        .where(BatteryAnnualUse.asset_id == asset.id)
        .order_by(BatteryAnnualUse.year)
    )
    if lock:
        annual_stmt = annual_stmt.with_for_update()
    annuals = list((await session.scalars(annual_stmt)).all())
    return asset, expense, annuals


def _rate_snapshot(rate: VatRate) -> str:
    """Identify the selected dictionary state; never trust client amounts."""
    payload = [
        str(rate.id), str(rate.percent), rate.label, rate.active, rate.updated_at.isoformat()
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


async def _prepare_asset_revision(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: BatteryAssetRevisionInput,
    *,
    lock: bool,
) -> tuple[
    BatteryAsset,
    Expense,
    list[BatteryAnnualUse],
    BatteryAssetRead,
    BatteryAssetRead,
    BatteryExpenseFinancialSnapshot,
    BatteryExpenseFinancialSnapshot,
    list[str],
    list[BatteryAnnualRevisionPreviewItem],
    str | None,
]:
    asset, expense, annuals = await _asset_revision_rows(session, company_id, asset_id, lock=lock)
    if asset.version != body.expected_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    if not body.model_fields_set.intersection(
        {"acquisition_expense_date", "in_service_date", "net_basis", "purchase_vat", "vat_rate_id"}
    ):
        raise BatteryConflict("BATTERY_REVISION_NO_CHANGES", asset.version)

    new_expense_date = body.acquisition_expense_date or expense.expense_date
    new_service_date = body.in_service_date or asset.in_service_date
    requested_net = body.net_basis if body.net_basis is not None else Decimal(str(asset.net_basis))
    requested_vat = (
        body.purchase_vat if body.purchase_vat is not None else Decimal(str(asset.purchase_vat))
    )
    rate_id = expense.vat_rate_id
    rate_percent = Decimal(str(expense.vat_rate_percent))
    rate_label = expense.vat_rate_label
    rate_snapshot: str | None = None
    if "vat_rate_id" in body.model_fields_set and body.vat_rate_id != expense.vat_rate_id:
        if body.vat_rate_id is None:
            raise BatteryConflict("BATTERY_EXPENSE_INELIGIBLE")
        rate_stmt = select(VatRate).where(
            VatRate.id == body.vat_rate_id, VatRate.company_id == company_id
        )
        if lock:
            # Serialize snapshot adoption with dictionary updates until commit.
            rate_stmt = rate_stmt.with_for_update(read=True).execution_options(
                populate_existing=True
            )
        rate = await session.scalar(rate_stmt)
        if lock and (rate is None or body.expected_rate_snapshot != _rate_snapshot(rate)):
            raise BatteryConflict("BATTERY_PREVIEW_STALE", asset.version)
        if rate is None or not rate.active:
            raise BatteryConflict("BATTERY_EXPENSE_INELIGIBLE")
        rate_snapshot = _rate_snapshot(rate)
        rate_id = rate.id
        rate_percent = Decimal(str(rate.percent))
        rate_label = rate.label

    if requested_net <= 0 or requested_vat <= 0 or rate_percent <= 0:
        raise BatteryConflict("BATTERY_EXPENSE_INELIGIBLE")
    if new_service_date < new_expense_date:
        raise BatteryConflict("BATTERY_EXPENSE_INELIGIBLE")

    exchange_rate = Decimal(str(expense.exchange_rate))
    if exchange_rate <= 0:
        raise BatteryConflict("BATTERY_EXPENSE_INELIGIBLE")
    original_net, original_vat, original_gross = compute_expense_amounts(
        requested_net / exchange_rate, requested_vat / exchange_rate
    )
    base_net = quantize_to_minor_unit(original_net * exchange_rate)
    base_vat = quantize_to_minor_unit(original_vat * exchange_rate)
    base_gross = quantize_to_minor_unit(base_net + base_vat)
    if (
        new_expense_date == expense.expense_date
        and new_service_date == asset.in_service_date
        and base_net == Decimal(str(asset.net_basis))
        and base_vat == Decimal(str(asset.purchase_vat))
        and rate_id == expense.vat_rate_id
    ):
        raise BatteryConflict("BATTERY_REVISION_NO_CHANGES", asset.version)

    before_asset = _asset_read(asset)
    before_expense = _expense_financial_snapshot(expense)
    after_asset = before_asset.model_copy(
        update={
            "acquisition_expense_date": new_expense_date,
            "in_service_date": new_service_date,
            "applicable_years": _years(new_service_date),
            "net_basis": base_net,
            "purchase_vat": base_vat,
            "purchase_vat_rate_percent": rate_percent,
            "version": asset.version + 1,
        }
    )
    after_expense = BatteryExpenseFinancialSnapshot(
        expense_date=new_expense_date,
        currency=expense.currency,
        exchange_rate=exchange_rate,
        net_amount=original_net,
        vat_amount=original_vat,
        gross_amount=original_gross,
        base_net_amount=base_net,
        base_vat_amount=base_vat,
        base_gross_amount=base_gross,
        vat_rate_id=rate_id,
        vat_rate_percent=rate_percent,
        vat_rate_label=rate_label,
    )
    affected_periods = sorted(set(_asset_periods(
        expense.expense_date, new_expense_date, asset.in_service_date, new_service_date
    )) | {f"{row.year}-Q4" for row in annuals})
    annual_by_year = {row.year: row for row in annuals}
    preview_items: list[BatteryAnnualRevisionPreviewItem] = []
    for year in sorted(
        set(_years(asset.in_service_date))
        | set(_years(new_service_date))
        | set(annual_by_year)
    ):
        row = annual_by_year.get(year)
        percentage = row.private_use_percentage if row is not None else None
        proposed_base: Decimal | None = None
        proposed_vat: Decimal | None = None
        if percentage is not None and year in _years(new_service_date):
            proposed_base, proposed_vat = compute_battery_adjustment(
                base_net, rate_percent, percentage
            )
        preview_items.append(
            BatteryAnnualRevisionPreviewItem(
                year=year,
                current_version=row.version if row is not None else None,
                current_confirmed=row.confirmed if row is not None else False,
                current_private_use_percentage=percentage,
                current_calculation_base=(
                    Decimal(str(row.calculation_base))
                    if row is not None and row.calculation_base is not None
                    else None
                ),
                current_calculation_vat=(
                    Decimal(str(row.calculation_vat))
                    if row is not None and row.calculation_vat is not None
                    else None
                ),
                proposed_calculation_base=proposed_base,
                proposed_calculation_vat=proposed_vat,
                requires_reconfirmation=row is not None and year in _years(new_service_date),
            )
        )
    return (
        asset,
        expense,
        annuals,
        before_asset,
        after_asset,
        before_expense,
        after_expense,
        affected_periods,
        preview_items,
        rate_snapshot,
    )


async def preview_asset_revision(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: BatteryAssetRevisionInput,
) -> BatteryAssetRevisionPreview:
    await set_rls_company(session, company_id)
    prepared = await _prepare_asset_revision(session, company_id, asset_id, body, lock=False)
    asset, _, _, before, after, expense_before, expense_after, periods, annuals, rate = prepared
    return BatteryAssetRevisionPreview(
        rate_snapshot=rate,
        asset_id=asset.id,
        expected_version=asset.version,
        before=before,
        after=after,
        expense_before=expense_before,
        expense_after=expense_after,
        affected_periods=periods,
        annual_previews=annuals,
        warnings=["Changes affect the listed report periods; filing status is unknown."],
    )


async def revise_asset(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: BatteryAssetRevisionInput,
) -> BatteryAssetRead:
    await set_rls_company(session, company_id)
    prepared = await _prepare_asset_revision(session, company_id, asset_id, body, lock=True)
    (
        asset,
        expense,
        annuals,
        before,
        after,
        expense_before,
        expense_after,
        periods,
        annual_previews,
        _,
    ) = prepared
    current_annual_versions = {row.year: row.version for row in annuals}
    if body.expected_annual_versions != current_annual_versions:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    if sorted(set(body.acknowledge_affected_periods)) != periods:
        raise BatteryConflict("BATTERY_REVISION_PERIOD_ACK_REQUIRED", asset.version)

    affected_annuals_before = [_annual_snapshot(row) for row in annuals]
    after_snapshot = _snapshot(after)
    after_snapshot["expense_after"] = _snapshot(expense_after)
    after_snapshot["annual_previews"] = [_snapshot(item) for item in annual_previews]
    after_snapshot["annual_before"] = affected_annuals_before
    after_snapshot["annual_after"] = [
        {
            **_annual_snapshot(row),
            "version": row.version + 1,
            "confirmed": False,
            "confirmed_at": None,
            "calculation_base": None,
            "calculation_vat": None,
            "asset_version_used": None,
        }
        for row in annuals
    ]
    revision = BatteryAssetRevision(
        company_id=company_id,
        asset_id=asset.id,
        version=asset.version + 1,
        reason=body.reason,
        actor_id=actor_id,
        affected_periods=periods,
        before_snapshot={
            **_snapshot(before),
            "expense_before": _snapshot(expense_before),
            "annual_before": affected_annuals_before,
        },
        after_snapshot=after_snapshot,
    )
    session.add(revision)
    await session.flush()
    await session.execute(
        select(func.set_config("jai.battery_asset_revision_id", str(revision.id), True))
    )
    asset.acquisition_expense_date = expense_after.expense_date
    asset.in_service_date = after.in_service_date
    asset.net_basis = expense_after.base_net_amount
    asset.purchase_vat = expense_after.base_vat_amount
    asset.purchase_vat_rate_percent = expense_after.vat_rate_percent
    asset.version += 1
    expense.expense_date = expense_after.expense_date
    expense.net_amount = expense_after.net_amount
    expense.vat_amount = expense_after.vat_amount
    expense.gross_amount = expense_after.gross_amount
    expense.base_net_amount = expense_after.base_net_amount
    expense.base_vat_amount = expense_after.base_vat_amount
    expense.base_gross_amount = expense_after.base_gross_amount
    if "vat_rate_id" in body.model_fields_set and body.vat_rate_id != expense.vat_rate_id:
        expense.vat_rate_id = expense_after.vat_rate_id
        expense.vat_rate_percent = expense_after.vat_rate_percent
        expense.vat_rate_label = expense_after.vat_rate_label
    for annual in annuals:
        annual.version += 1
        annual.confirmed = False
        annual.confirmed_at = None
        annual.calculation_base = None
        annual.calculation_vat = None
        annual.asset_version_used = None
    await session.commit()
    await set_rls_company(session, company_id)
    await session.refresh(asset)
    return _asset_read(asset)


def _annual_snapshot(row: BatteryAnnualUse) -> dict[str, object]:
    return {
        "id": str(row.id),
        "year": row.year,
        "version": row.version,
        "private_use_percentage": (
            str(row.private_use_percentage) if row.private_use_percentage is not None else None
        ),
        "basis_note": row.basis_note,
        "confirmed": row.confirmed,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "calculation_base": str(row.calculation_base) if row.calculation_base is not None else None,
        "calculation_vat": str(row.calculation_vat) if row.calculation_vat is not None else None,
        "asset_version_used": row.asset_version_used,
    }


async def register_asset(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: BatteryAssetCreateRequest,
) -> BatteryAssetRead:
    await set_rls_company(session, company_id)
    expense = await _load_expense(session, body.acquisition_expense_id, company_id, for_update=True)
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
    row = await session.scalar(
        select(BatteryAnnualUse)
        .options(selectinload(BatteryAnnualUse.evidence))
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
    )
    if row is None:
        if year not in _years(asset.in_service_date):
            raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
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
        raise BatteryConflict("BATTERY_REVISION_REQUIRED", row.version)
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
    expected_asset_version: int,
) -> BatteryAnnualCalculation:
    await set_rls_company(session, company_id)
    asset = await session.scalar(
        select(BatteryAsset).where(BatteryAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise LookupError("Battery asset not found.")
    if year not in _years(asset.in_service_date):
        raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
    if asset.version != expected_asset_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    annual = await session.scalar(
        select(BatteryAnnualUse)
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
        .with_for_update()
    )
    if annual is None:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    if annual.version != expected_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", annual.version)
    if annual.private_use_percentage is None or not annual.basis_note:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    base, vat = compute_battery_adjustment(
        Decimal(str(asset.net_basis)),
        Decimal(str(asset.purchase_vat_rate_percent)),
        annual.private_use_percentage,
    )
    return BatteryAnnualCalculation(
        asset_id=asset_id,
        year=year,
        expected_version=annual.version,
        expected_asset_version=asset.version,
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
    if asset.version != body.expected_asset_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    if year not in _years(asset.in_service_date):
        raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
    row = await session.scalar(
        select(BatteryAnnualUse)
        .where(BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year)
        .with_for_update()
    )
    if row is None:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    if row.version != body.expected_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", row.version)
    if row.private_use_percentage is None or not row.basis_note:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    if row.confirmed:
        raise BatteryConflict("BATTERY_ANNUAL_ALREADY_CONFIRMED", row.version)
    calculation_base, calculation_vat = compute_battery_adjustment(
        Decimal(str(asset.net_basis)),
        Decimal(str(asset.purchase_vat_rate_percent)),
        row.private_use_percentage,
    )
    row.confirmed = True
    row.confirmed_at = datetime.now(UTC)
    row.calculation_base = calculation_base
    row.calculation_vat = calculation_vat
    row.asset_version_used = asset.version
    session.add(
        BatteryAnnualConfirmation(
            company_id=company_id,
            annual_use_id=row.id,
            version=row.version,
            actor_id=actor_id,
            private_use_percentage=row.private_use_percentage,
            calculation_base=calculation_base,
            calculation_vat=calculation_vat,
            asset_version_used=asset.version,
        )
    )
    await session.commit()
    return await get_annual_use(session, company_id, asset_id, year)  # type: ignore[return-value]


async def _annual_revision_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    *,
    lock: bool,
) -> tuple[BatteryAsset, BatteryAnnualUse]:
    asset_stmt = select(BatteryAsset).where(BatteryAsset.id == asset_id)
    if lock:
        asset_stmt = asset_stmt.with_for_update().execution_options(populate_existing=True)
    asset = await session.scalar(asset_stmt)
    if asset is None:
        raise LookupError("Battery asset not found.")
    if year not in _years(asset.in_service_date):
        raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
    annual_stmt = select(BatteryAnnualUse).where(
        BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year
    )
    if lock:
        annual_stmt = annual_stmt.with_for_update().execution_options(populate_existing=True)
    annual = await session.scalar(annual_stmt)
    if annual is None:
        raise BatteryConflict("BATTERY_ANNUAL_INPUT_MISSING")
    return asset, annual


def _annual_proposal(
    asset: BatteryAsset, body: BatteryAnnualRevisionInput
) -> tuple[Decimal | None, Decimal | None]:
    if body.private_use_percentage is None:
        return None, None
    return compute_battery_adjustment(
        Decimal(str(asset.net_basis)),
        Decimal(str(asset.purchase_vat_rate_percent)),
        body.private_use_percentage,
    )


async def preview_annual_revision(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualRevisionInput,
) -> BatteryAnnualRevisionPreview:
    await set_rls_company(session, company_id)
    asset, annual = await _annual_revision_rows(session, company_id, asset_id, year, lock=False)
    if asset.version != body.expected_asset_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    if annual.version != body.expected_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", annual.version)
    proposed_base, proposed_vat = _annual_proposal(asset, body)
    return BatteryAnnualRevisionPreview(
        asset_id=asset.id,
        year=year,
        expected_version=annual.version,
        expected_asset_version=asset.version,
        current_private_use_percentage=annual.private_use_percentage,
        current_calculation_base=(
            Decimal(str(annual.calculation_base)) if annual.calculation_base is not None else None
        ),
        current_calculation_vat=(
            Decimal(str(annual.calculation_vat)) if annual.calculation_vat is not None else None
        ),
        proposed_private_use_percentage=body.private_use_percentage,
        proposed_calculation_base=proposed_base,
        proposed_calculation_vat=proposed_vat,
        affected_periods=[f"{year}-Q4"],
        warnings=["This correction affects the listed Q4 report; filing status is unknown."],
    )


async def revise_annual_use(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: BatteryAnnualRevisionInput,
) -> BatteryAnnualUseRead:
    await set_rls_company(session, company_id)
    asset, annual = await _annual_revision_rows(session, company_id, asset_id, year, lock=True)
    if asset.version != body.expected_asset_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", asset.version)
    if annual.version != body.expected_version:
        raise BatteryConflict("BATTERY_VERSION_CONFLICT", annual.version)
    periods = [f"{year}-Q4"]
    if body.acknowledge_affected_periods != periods:
        raise BatteryConflict("BATTERY_REVISION_PERIOD_ACK_REQUIRED", annual.version)
    before = _annual_snapshot(annual)
    proposed_base, proposed_vat = _annual_proposal(asset, body)
    annual.version += 1
    annual.private_use_percentage = body.private_use_percentage
    annual.basis_note = body.basis_note
    annual.confirmed = False
    annual.confirmed_at = None
    annual.calculation_base = None
    annual.calculation_vat = None
    annual.asset_version_used = None
    after = {
        **_annual_snapshot(annual),
        "proposed_calculation_base": str(proposed_base) if proposed_base is not None else None,
        "proposed_calculation_vat": str(proposed_vat) if proposed_vat is not None else None,
    }
    session.add(
        BatteryAnnualRevision(
            company_id=company_id,
            annual_use_id=annual.id,
            version=annual.version,
            reason=body.reason,
            actor_id=actor_id,
            before_snapshot=before,
            after_snapshot=after,
        )
    )
    await session.commit()
    return await get_annual_use(session, company_id, asset_id, year)  # type: ignore[return-value]


async def list_asset_revisions(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    offset: int,
    limit: int,
) -> BatteryAssetRevisionListRead:
    await set_rls_company(session, company_id)
    if await session.scalar(select(BatteryAsset.id).where(BatteryAsset.id == asset_id)) is None:
        raise LookupError("Battery asset not found.")
    query = select(BatteryAssetRevision).where(BatteryAssetRevision.asset_id == asset_id)
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = list(
        (
            await session.scalars(
                query.order_by(BatteryAssetRevision.version).offset(offset).limit(limit)
            )
        ).all()
    )
    return BatteryAssetRevisionListRead(
        items=[BatteryAssetRevisionRead.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


async def list_annual_revisions(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    offset: int,
    limit: int,
) -> BatteryAnnualRevisionListRead:
    await set_rls_company(session, company_id)
    await get_asset(session, company_id, asset_id)
    annual_id = await session.scalar(
        select(BatteryAnnualUse.id).where(
            BatteryAnnualUse.asset_id == asset_id, BatteryAnnualUse.year == year
        )
    )
    if annual_id is None:
        raise LookupError("Battery annual input not found.")
    query = select(BatteryAnnualRevision).where(BatteryAnnualRevision.annual_use_id == annual_id)
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = list(
        (
            await session.scalars(
                query.order_by(BatteryAnnualRevision.version).offset(offset).limit(limit)
            )
        ).all()
    )
    return BatteryAnnualRevisionListRead(
        items=[BatteryAnnualRevisionRead.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


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
    await set_rls_company(session, company_id)
    asset = await session.scalar(select(BatteryAsset).where(BatteryAsset.id == asset_id))
    if asset is None:
        raise LookupError("Battery asset not found.")
    if year not in _years(asset.in_service_date):
        raise BatteryConflict("BATTERY_YEAR_NOT_APPLICABLE")
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
