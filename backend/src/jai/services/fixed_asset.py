"""Fixed asset conversion and annual register reads."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.secret import get_auth_secret
from jai.db import set_rls_company
from jai.models.fixed_asset import (
    FixedAsset,
    FixedAssetAnnualRevision,
    FixedAssetAnnualUse,
    FixedAssetRevision,
)
from jai.schemas.expense import (
    FixedAssetConfirmRequest,
    FixedAssetRead,
    FixedAssetYearDelta,
)
from jai.schemas.fixed_asset import (
    FixedAssetAnnualConfirmInput,
    FixedAssetAnnualConfirmPreview,
    FixedAssetAnnualConfirmPreviewInput,
    FixedAssetAnnualPeriodDelta,
    FixedAssetAnnualRevisionInput,
    FixedAssetAnnualRevisionPreview,
    FixedAssetAnnualRevisionRead,
    FixedAssetAnnualUseInput,
    FixedAssetAnnualUseRead,
    FixedAssetRevisionRead,
    FixedAssetServiceDateRevisionInput,
    FixedAssetServiceDateRevisionPreview,
)
from jai.services.expense import _load_expense
from jai.services.fixed_asset_preview import (
    AnnualBusinessUse,
    FixedAssetConflict,
    apply_yearly_business_use,
    build_fixed_asset_preview,
    build_monthly_fixed_asset_schedule,
)

_PREVIEW_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class _AnnualProjectionInput:
    year: int
    estimated_business_percentage: Decimal
    actual_business_percentage: Decimal | None


async def _read(
    session: AsyncSession, asset: FixedAsset, year: int | None = None
) -> FixedAssetRead:
    from jai.models.battery import BatteryAsset

    battery_id = await session.scalar(
        select(BatteryAsset.id).where(
            BatteryAsset.acquisition_expense_id == asset.acquisition_expense_id
        )
    )
    requested_year = year if year is not None else date.today().year
    projection_year = max(requested_year, asset.acquisition_date.year)
    rows = (
        build_monthly_fixed_asset_schedule(
            asset.acquisition_cost,
            asset.residual_value,
            asset.useful_life_years,
            asset.in_service_date,
            Decimal("100"),
        )
        if asset.in_service_date is not None
        else []
    )
    annuals = (
        await session.scalars(
            select(FixedAssetAnnualUse).where(FixedAssetAnnualUse.asset_id == asset.id)
        )
    ).all()
    provisional_years = apply_yearly_business_use(
        rows, asset.business_percentage, annuals, asset.use_type
    )
    annual_rows = [row for row in rows if row.period_end.year == projection_year]
    prior_rows = [row for row in rows if row.period_end.year < projection_year]
    as_of = date.today()
    planned = sum((row.business_amount for row in annual_rows), Decimal("0"))
    full = sum((row.amount for row in annual_rows), Decimal("0"))
    realized_rows = [row for row in annual_rows if row.period_end <= as_of]
    realized = sum((row.amount for row in realized_rows), Decimal("0"))
    actual = sum((row.business_amount for row in realized_rows), Decimal("0"))
    actual_prior = sum(
        (
            row.amount
            for row in rows
            if row.period_end.year < projection_year and row.period_end <= as_of
        ),
        Decimal("0"),
    )
    planned_prior = sum((row.amount for row in prior_rows), Decimal("0"))
    opening = asset.acquisition_cost - actual_prior
    planned_opening = asset.acquisition_cost - planned_prior
    closing = opening - realized
    planned_closing = planned_opening - full
    cumulative = asset.acquisition_cost - closing
    yearly_deltas = [
        FixedAssetYearDelta(
            year=projection_year,
            provisional=projection_year in provisional_years,
            legacy_expense_amount=Decimal("0"),
            planned_legacy_expense_amount=Decimal("0"),
            asset_depreciation=full,
            business_use_reduction=realized - actual,
            fixed_asset_depreciation=actual,
            realized_asset_depreciation=realized,
            planned_fixed_asset_depreciation=planned,
            planned_difference=planned,
            planned_business_use_reduction=full - planned,
            difference=actual,
            opening_book_value=opening,
            planned_opening_book_value=planned_opening,
            cumulative_depreciation=cumulative,
            closing_book_value=closing,
            planned_closing_book_value=planned_closing,
        )
    ]
    return FixedAssetRead(
        id=asset.id,
        acquisition_expense_id=asset.acquisition_expense_id,
        battery_asset_id=battery_id,
        name=asset.name,
        acquisition_date=asset.acquisition_date,
        in_service_date=asset.in_service_date,
        acquisition_cost=asset.acquisition_cost,
        residual_value=asset.residual_value,
        useful_life_years=asset.useful_life_years,
        business_percentage=asset.business_percentage,
        use_type=cast(Literal["FULL_BUSINESS", "MIXED_USE"], asset.use_type),
        version=asset.version,
        pending_in_service=asset.in_service_date is None,
        yearly_deltas=yearly_deltas,
    )


async def convert_expense_to_fixed_asset(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
    request: FixedAssetConfirmRequest,
    actor_id: uuid.UUID,
) -> FixedAssetRead:
    """Revalidate the preview while holding the source purchase row lock."""
    await set_rls_company(session, company_id)
    expense = await _load_expense(session, expense_id, company_id, for_update=True)
    from jai.models.battery import BatteryAsset

    linked_battery = await session.scalar(
        select(BatteryAsset)
        .where(BatteryAsset.acquisition_expense_id == expense_id)
        .with_for_update()
    )
    if linked_battery is not None and request.in_service_date is None:
        request = request.model_copy(update={"in_service_date": linked_battery.in_service_date})
    if request.use_type == "FULL_BUSINESS" and request.business_percentage != 100:
        raise ValueError("Full-business use requires a 100% business percentage.")
    existing = await session.scalar(
        select(FixedAsset).where(FixedAsset.acquisition_expense_id == expense_id)
    )
    if existing:
        if (
            existing.name == request.asset_name.strip()
            and existing.in_service_date == request.in_service_date
            and existing.useful_life_years == request.useful_life_years
            and existing.residual_value == request.residual_value
            and existing.business_percentage == request.business_percentage
            and existing.use_type == request.use_type
        ):
            return await _read(session, existing)
        raise ValueError("This purchase is already registered with different fixed asset terms.")
    if linked_battery is not None and request.in_service_date != linked_battery.in_service_date:
        raise FixedAssetConflict("The fixed asset and battery must use the same in-service date.")
    preview = build_fixed_asset_preview(expense, request, company_id=company_id)
    if not _PREVIEW_TOKEN_PATTERN.fullmatch(request.preview_token) or not hmac.compare_digest(
        request.preview_token, preview.preview_token
    ):
        raise ValueError("Preview is missing, stale, or does not match these terms. Preview again.")
    if preview.conversion_blocked:
        raise ValueError(preview.conversion_block_reason or "Conversion affects a prior year.")
    asset = FixedAsset(
        company_id=company_id,
        acquisition_expense_id=expense.id,
        name=request.asset_name.strip(),
        acquisition_date=expense.expense_date,
        in_service_date=request.in_service_date,
        acquisition_cost=preview.acquisition_cost,
        residual_value=request.residual_value,
        useful_life_years=request.useful_life_years,
        business_percentage=request.business_percentage,
        use_type=request.use_type,
        created_by=actor_id,
    )
    session.add(asset)
    await session.flush()
    if request.use_type == "MIXED_USE":
        session.add(
            FixedAssetAnnualUse(
                company_id=company_id,
                asset_id=asset.id,
                year=(request.in_service_date or expense.expense_date).year,
                estimated_business_percentage=request.business_percentage,
            )
        )
    await session.flush()
    response = await _read(session, asset)
    await session.commit()
    return response


async def list_fixed_assets(
    session: AsyncSession, company_id: uuid.UUID, year: int | None = None
) -> list[FixedAssetRead]:
    await set_rls_company(session, company_id)
    rows = (
        await session.scalars(
            select(FixedAsset).order_by(FixedAsset.acquisition_date.desc(), FixedAsset.name)
        )
    ).all()
    return [
        await _read(session, asset, year)
        for asset in rows
        if year is None or asset.acquisition_date.year <= year
    ]


async def get_fixed_asset(
    session: AsyncSession, asset_id: uuid.UUID, company_id: uuid.UUID, year: int | None = None
) -> FixedAssetRead:
    await set_rls_company(session, company_id)
    row = (
        await session.execute(select(FixedAsset).where(FixedAsset.id == asset_id))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("Fixed asset not found.")
    if year is not None and year < row.acquisition_date.year:
        raise LookupError("Fixed asset was not registered in the requested year.")
    return await _read(session, row, year)


def _annual_read(row: FixedAssetAnnualUse, asset: FixedAsset, year: int) -> FixedAssetAnnualUseRead:
    from jai.services.fixed_asset_preview import build_monthly_fixed_asset_schedule
    from jai.services.money import quantize_to_minor_unit

    full = Decimal("0.00")
    if asset.in_service_date is not None:
        full = sum(
            (
                item.amount
                for item in build_monthly_fixed_asset_schedule(
                    asset.acquisition_cost,
                    asset.residual_value,
                    asset.useful_life_years,
                    asset.in_service_date,
                    Decimal("100"),
                )
                if item.period_end.year == year and item.period_end <= date.today()
            ),
            Decimal("0.00"),
        )
    pct = (
        row.actual_business_percentage
        if row.actual_business_percentage is not None
        else row.estimated_business_percentage
    )
    net = quantize_to_minor_unit(full * Decimal(str(pct)) / Decimal("100"))
    return FixedAssetAnnualUseRead(
        asset_id=asset.id,
        year=year,
        estimated_business_percentage=row.estimated_business_percentage,
        actual_business_percentage=row.actual_business_percentage,
        effective_business_percentage=pct,
        provisional=row.actual_business_percentage is None,
        version=row.version,
        confirmed_at=row.confirmed_at,
        full_depreciation=full,
        private_use_reduction=full - net,
        net_pnl_effect=net,
    )


def _ensure_mixed_asset(asset: FixedAsset) -> None:
    if asset.use_type != "MIXED_USE":
        raise FixedAssetConflict("Annual business-use inputs apply only to mixed-use assets.")


def _ensure_actual_period(year: int) -> None:
    today = date.today()
    if year > today.year:
        raise FixedAssetConflict("Actual use cannot be confirmed for a future calendar year.")
    if year == today.year and today.month < 10:
        raise FixedAssetConflict("Actual use can be confirmed in Q4 or later.")


def _annual_projection_state(
    company_id: uuid.UUID,
    asset: FixedAsset,
    annuals: list[FixedAssetAnnualUse],
    year: int,
    percentage: Decimal,
) -> tuple[list[FixedAssetAnnualPeriodDelta], list[str], str, list[FixedAssetAnnualPeriodDelta]]:
    from jai.auth.secret import get_auth_secret

    rows = [
        _AnnualProjectionInput(
            year=row.year,
            estimated_business_percentage=row.estimated_business_percentage,
            actual_business_percentage=row.actual_business_percentage,
        )
        for row in annuals
    ]
    current = next((item for item in rows if item.year == year), None)
    if current is None:
        prior = next((item for item in reversed(rows) if item.year < year), None)
        estimate = (
            prior.actual_business_percentage
            if prior is not None and prior.actual_business_percentage is not None
            else prior.estimated_business_percentage
            if prior is not None
            else asset.business_percentage
        )
        current = _AnnualProjectionInput(
            year=year,
            estimated_business_percentage=estimate,
            actual_business_percentage=None,
        )
        rows.append(current)
    baseline_rows = [
        _AnnualProjectionInput(
            year=item.year,
            estimated_business_percentage=item.estimated_business_percentage,
            actual_business_percentage=item.actual_business_percentage,
        )
        for item in rows
    ]
    current.actual_business_percentage = percentage
    as_of = date.today()

    def projected(items: Sequence[AnnualBusinessUse], *, monthly: bool) -> dict[str, Decimal]:
        if asset.in_service_date is None:
            return {}
        schedule = build_monthly_fixed_asset_schedule(
            asset.acquisition_cost,
            asset.residual_value,
            asset.useful_life_years,
            asset.in_service_date,
            Decimal("100"),
        )
        apply_yearly_business_use(schedule, asset.business_percentage, items, asset.use_type)
        totals: dict[str, Decimal] = {}
        for item in schedule:
            if item.period_end > as_of:
                continue
            period = (
                item.period_end.strftime("%Y-%m")
                if monthly
                else f"{item.period_end.year}-Q{(item.period_end.month - 1) // 3 + 1}"
            )
            totals[period] = totals.get(period, Decimal("0.00")) + item.business_amount
        return totals

    before_values = projected(baseline_rows, monthly=False)
    after_values = projected(rows, monthly=False)
    periods = sorted(
        period
        for period in set(before_values) | set(after_values)
        if before_values.get(period, Decimal("0.00")) != after_values.get(period, Decimal("0.00"))
    )
    deltas = [
        FixedAssetAnnualPeriodDelta(
            period=period,
            before_business_depreciation=before_values.get(period, Decimal("0.00")),
            after_business_depreciation=after_values.get(period, Decimal("0.00")),
            pnl_delta=after_values.get(period, Decimal("0.00"))
            - before_values.get(period, Decimal("0.00")),
        )
        for period in periods
    ]
    before_months = projected(baseline_rows, monthly=True)
    after_months = projected(rows, monthly=True)
    monthly_periods = sorted(
        period
        for period in set(before_months) | set(after_months)
        if before_months.get(period, Decimal("0.00"))
        != after_months.get(period, Decimal("0.00"))
    )
    monthly_deltas = [
        FixedAssetAnnualPeriodDelta(
            period=period,
            before_business_depreciation=before_months.get(period, Decimal("0.00")),
            after_business_depreciation=after_months.get(period, Decimal("0.00")),
            pnl_delta=after_months.get(period, Decimal("0.00"))
            - before_months.get(period, Decimal("0.00")),
        )
        for period in monthly_periods
    ]
    payload = {
        "company_id": str(company_id),
        "asset_id": str(asset.id),
        "version": asset.version,
        "cost": str(asset.acquisition_cost),
        "residual": str(asset.residual_value),
        "life": asset.useful_life_years,
        "service_date": asset.in_service_date.isoformat() if asset.in_service_date else None,
        "year": year,
        "percentage": str(percentage),
        "as_of": as_of.isoformat(),
        "annuals": [
            [
                row.year,
                row.version,
                str(row.estimated_business_percentage),
                str(row.actual_business_percentage)
                if row.actual_business_percentage is not None
                else None,
            ]
            for row in annuals
        ],
        "periods": [
            [
                item.period,
                str(item.before_business_depreciation),
                str(item.after_business_depreciation),
            ]
            for item in deltas
        ],
        "monthly_periods": [
            [
                item.period,
                str(item.before_business_depreciation),
                str(item.after_business_depreciation),
            ]
            for item in monthly_deltas
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    token = hmac.new(
        get_auth_secret().encode("utf-8"),
        b"jai:m20:fixed-asset-annual-confirm:v1:" + encoded,
        hashlib.sha256,
    ).hexdigest()
    return deltas, periods, token, monthly_deltas


async def preview_annual_confirmation(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: FixedAssetAnnualConfirmPreviewInput,
) -> FixedAssetAnnualConfirmPreview:
    await set_rls_company(session, company_id)
    _ensure_actual_period(year)
    asset = await session.scalar(select(FixedAsset).where(FixedAsset.id == asset_id))
    if asset is None:
        raise LookupError("Fixed asset not found.")
    _ensure_mixed_asset(asset)
    annuals = list(
        (
            await session.scalars(
                select(FixedAssetAnnualUse)
                .where(FixedAssetAnnualUse.asset_id == asset_id)
                .order_by(FixedAssetAnnualUse.year)
            )
        ).all()
    )
    current = next((item for item in annuals if item.year == year), None)
    if current is not None and current.actual_business_percentage is not None:
        raise FixedAssetConflict("Confirmed actual use requires an audited correction.")
    prior = next((item for item in reversed(annuals) if item.year <= year), None)
    before_pct = (
        current.estimated_business_percentage
        if current is not None
        else prior.actual_business_percentage
        if prior is not None and prior.actual_business_percentage is not None
        else prior.estimated_business_percentage
        if prior is not None
        else asset.business_percentage
    )
    deltas, periods, token, monthly_deltas = _annual_projection_state(
        company_id, asset, annuals, year, body.actual_business_percentage
    )
    return FixedAssetAnnualConfirmPreview(
        asset_id=asset.id,
        year=year,
        before_business_percentage=before_pct,
        after_business_percentage=body.actual_business_percentage,
        period_deltas=deltas,
        monthly_period_deltas=monthly_deltas,
        affected_periods=periods,
        preview_token=token,
    )


async def get_annual_use(
    session: AsyncSession, company_id: uuid.UUID, asset_id: uuid.UUID, year: int
) -> FixedAssetAnnualUseRead | None:
    await set_rls_company(session, company_id)
    asset = await session.scalar(select(FixedAsset).where(FixedAsset.id == asset_id))
    if asset is None:
        raise LookupError("Fixed asset not found.")
    _ensure_mixed_asset(asset)
    rows = list(
        (
            await session.scalars(
                select(FixedAssetAnnualUse)
                .where(
                    FixedAssetAnnualUse.asset_id == asset_id,
                    FixedAssetAnnualUse.year <= year,
                )
                .order_by(FixedAssetAnnualUse.year)
            )
        ).all()
    )
    row = next((item for item in reversed(rows) if item.year == year), None)
    if row is None:
        # GET is the same effective projection used by reports: the latest
        # annual row (estimate or actual) carries forward provisionally.
        prior = rows[-1] if rows else None
        percentage = (
            prior.actual_business_percentage
            if prior is not None and prior.actual_business_percentage is not None
            else prior.estimated_business_percentage
            if prior is not None
            else asset.business_percentage
        )
        return _annual_projection(
            asset, year, percentage, provisional=True, version=prior.version if prior else 0
        )
    return _annual_read(row, asset, year)


def _annual_projection(
    asset: FixedAsset, year: int, percentage: Decimal, *, provisional: bool, version: int
) -> FixedAssetAnnualUseRead:
    from jai.services.fixed_asset_preview import build_monthly_fixed_asset_schedule
    from jai.services.money import quantize_to_minor_unit

    full = Decimal("0.00")
    if asset.in_service_date is not None:
        full = sum(
            (
                item.amount
                for item in build_monthly_fixed_asset_schedule(
                    asset.acquisition_cost,
                    asset.residual_value,
                    asset.useful_life_years,
                    asset.in_service_date,
                    Decimal("100"),
                )
                if item.period_end.year == year and item.period_end <= date.today()
            ),
            Decimal("0.00"),
        )
    net = quantize_to_minor_unit(full * Decimal(str(percentage)) / Decimal("100"))
    return FixedAssetAnnualUseRead(
        asset_id=asset.id,
        year=year,
        estimated_business_percentage=percentage,
        actual_business_percentage=None,
        effective_business_percentage=percentage,
        provisional=provisional,
        version=version,
        confirmed_at=None,
        full_depreciation=full,
        private_use_reduction=full - net,
        net_pnl_effect=net,
    )


async def put_annual_estimate(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: FixedAssetAnnualUseInput,
) -> FixedAssetAnnualUseRead:
    await set_rls_company(session, company_id)
    asset = await session.scalar(
        select(FixedAsset).where(FixedAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise LookupError("Fixed asset not found.")
    _ensure_mixed_asset(asset)
    row = await session.scalar(
        select(FixedAssetAnnualUse)
        .where(FixedAssetAnnualUse.asset_id == asset_id, FixedAssetAnnualUse.year == year)
        .with_for_update()
    )
    if row is not None and row.actual_business_percentage is not None:
        raise FixedAssetConflict("Confirmed annual use requires an audited correction.")
    if row is None:
        row = FixedAssetAnnualUse(
            company_id=company_id,
            asset_id=asset_id,
            year=year,
            estimated_business_percentage=body.estimated_business_percentage,
        )
        session.add(row)
    else:
        row.estimated_business_percentage = body.estimated_business_percentage
        row.version += 1
        row.updated_at = datetime.now(UTC)
    await session.flush()
    result = _annual_read(row, asset, year)
    await session.commit()
    return result


async def confirm_annual_use(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: FixedAssetAnnualConfirmInput,
) -> FixedAssetAnnualUseRead:
    await set_rls_company(session, company_id)
    _ensure_actual_period(year)
    asset = await session.scalar(
        select(FixedAsset).where(FixedAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise LookupError("Fixed asset not found.")
    _ensure_mixed_asset(asset)
    row = await session.scalar(
        select(FixedAssetAnnualUse)
        .where(FixedAssetAnnualUse.asset_id == asset_id, FixedAssetAnnualUse.year == year)
        .with_for_update()
    )
    if row is not None and row.actual_business_percentage is not None:
        raise FixedAssetConflict("Confirmed actual use requires an audited correction.")
    annuals = list(
        (
            await session.scalars(
                select(FixedAssetAnnualUse)
                .where(FixedAssetAnnualUse.asset_id == asset_id)
                .order_by(FixedAssetAnnualUse.year)
                .with_for_update()
            )
        ).all()
    )
    deltas, periods, expected_token, monthly_deltas = _annual_projection_state(
        company_id, asset, annuals, year, body.actual_business_percentage
    )
    if not hmac.compare_digest(body.preview_token, expected_token):
        raise FixedAssetConflict("Confirmation preview is stale or does not match these terms.")
    if row is None:
        previous = next((item for item in reversed(annuals) if item.year < year), None)
        row = FixedAssetAnnualUse(
            company_id=company_id,
            asset_id=asset_id,
            year=year,
            estimated_business_percentage=(
                previous.actual_business_percentage
                if previous is not None and previous.actual_business_percentage is not None
                else previous.estimated_business_percentage
                if previous is not None
                else asset.business_percentage
            ),
            version=1,
        )
        session.add(row)
        # Materialize the durable annual row before creating an audit that
        # references its ID and version. Both writes remain in this transaction.
        await session.flush()
    next_version = row.version + 1
    before_snapshot = {
        "estimated_business_percentage": str(row.estimated_business_percentage),
        "actual_business_percentage": None,
        "effective_business_percentage": str(row.estimated_business_percentage),
        "version": row.version,
    }
    after_snapshot = {
        "estimated_business_percentage": str(row.estimated_business_percentage),
        "actual_business_percentage": str(body.actual_business_percentage),
        "effective_business_percentage": str(body.actual_business_percentage),
        "version": next_version,
        "affected_periods": periods,
        "period_deltas": [item.model_dump(mode="json") for item in deltas],
        "monthly_period_deltas": [item.model_dump(mode="json") for item in monthly_deltas],
    }
    session.add(
        FixedAssetAnnualRevision(
            company_id=company_id,
            annual_use_id=row.id,
            version=next_version,
            reason=body.reason,
            actor_id=actor_id,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
    )
    row.actual_business_percentage = body.actual_business_percentage
    row.confirmed_at = datetime.now(UTC)
    row.version = next_version
    await session.flush()
    result = _annual_read(row, asset, year)
    await session.commit()
    return result


def _annual_revision_token(
    company_id: uuid.UUID,
    asset: FixedAsset,
    row: FixedAssetAnnualUse,
    annuals: list[FixedAssetAnnualUse],
    percentage: Decimal,
) -> str:
    # Share the same canonical report cutoff and period-delta signature as
    # initial confirmation so a month-end advance invalidates old approvals.
    return _annual_projection_state(company_id, asset, annuals, row.year, percentage)[2]


def _service_date_token(
    company_id: uuid.UUID,
    asset: FixedAsset,
    new_date: date,
    annuals: list[FixedAssetAnnualUse],
) -> str:
    as_of = date.today()
    schedule = build_monthly_fixed_asset_schedule(
        asset.acquisition_cost,
        asset.residual_value,
        asset.useful_life_years,
        new_date,
        Decimal("100"),
    )
    apply_yearly_business_use(schedule, asset.business_percentage, annuals, asset.use_type)
    projected: dict[str, Decimal] = {}
    for item in schedule:
        if item.period_end > as_of:
            continue
        period = f"{item.period_end.year}-Q{(item.period_end.month - 1) // 3 + 1}"
        projected[period] = projected.get(period, Decimal("0.00")) + item.business_amount
    payload = json.dumps(
        [
            str(company_id),
            str(asset.id),
            asset.version,
            str(asset.acquisition_cost),
            str(asset.residual_value),
            asset.useful_life_years,
            new_date.isoformat(),
            as_of.isoformat(),
            [[period, str(amount)] for period, amount in sorted(projected.items())],
            [
                [
                    str(item.id),
                    item.year,
                    item.version,
                    str(item.estimated_business_percentage),
                    str(item.actual_business_percentage)
                    if item.actual_business_percentage is not None
                    else None,
                ]
                for item in annuals
            ],
        ],
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        get_auth_secret().encode(),
        b"jai:m20:fixed-asset-service-date:v1:" + payload,
        hashlib.sha256,
    ).hexdigest()


async def preview_service_date_revision(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: FixedAssetServiceDateRevisionInput,
) -> FixedAssetServiceDateRevisionPreview:
    await set_rls_company(session, company_id)
    asset = await session.scalar(select(FixedAsset).where(FixedAsset.id == asset_id))
    if asset is None:
        raise LookupError("Fixed asset not found.")
    if asset.version != body.expected_version:
        raise FixedAssetConflict("Asset changed; reload and preview the correction again.")
    if asset.in_service_date is not None:
        raise FixedAssetConflict("Only pending assets can be activated by this correction.")
    if body.in_service_date < asset.acquisition_date or body.in_service_date > date.today():
        raise FixedAssetConflict("Enter the actual in-service date from acquisition through today.")
    from jai.models.battery import BatteryAsset

    if (
        await session.scalar(
            select(BatteryAsset.id).where(
                BatteryAsset.acquisition_expense_id == asset.acquisition_expense_id
            )
        )
        is not None
    ):
        raise FixedAssetConflict("Use the linked battery asset revision to change the shared date.")
    schedule = build_monthly_fixed_asset_schedule(
        asset.acquisition_cost,
        asset.residual_value,
        asset.useful_life_years,
        body.in_service_date,
        Decimal("100"),
    )
    annuals = list(
        (
            await session.scalars(
                select(FixedAssetAnnualUse).where(FixedAssetAnnualUse.asset_id == asset.id)
            )
        ).all()
    )
    apply_yearly_business_use(schedule, asset.business_percentage, annuals, asset.use_type)
    completed = [row for row in schedule if row.period_end <= date.today()]
    after = sum((row.business_amount for row in completed), Decimal("0.00"))
    periods = sorted(
        {f"{row.period_end.year}-Q{(row.period_end.month - 1) // 3 + 1}" for row in completed}
    )
    return FixedAssetServiceDateRevisionPreview(
        asset_id=asset.id,
        expected_version=asset.version,
        before_in_service_date=None,
        after_in_service_date=body.in_service_date,
        affected_periods=periods,
        before_pnl_effect=Decimal("0.00"),
        after_pnl_effect=after,
        pnl_delta=after,
        preview_token=_service_date_token(company_id, asset, body.in_service_date, annuals),
    )


async def revise_service_date(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: FixedAssetServiceDateRevisionInput,
) -> FixedAssetRead:
    await set_rls_company(session, company_id)
    probe = await session.scalar(select(FixedAsset).where(FixedAsset.id == asset_id))
    if probe is None:
        raise LookupError("Fixed asset not found.")
    await _load_expense(session, probe.acquisition_expense_id, company_id, for_update=True)
    asset = await session.scalar(
        select(FixedAsset)
        .where(FixedAsset.id == asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None:
        raise LookupError("Fixed asset not found.")
    preview = await preview_service_date_revision(session, company_id, asset_id, body)
    if asset.version != body.expected_version or not hmac.compare_digest(
        body.preview_token, preview.preview_token
    ):
        raise FixedAssetConflict("Correction preview is stale or does not match these terms.")
    before = {
        "in_service_date": None,
        "acquisition_date": asset.acquisition_date.isoformat(),
        "acquisition_cost": str(asset.acquisition_cost),
        "version": asset.version,
    }
    after = {
        "in_service_date": body.in_service_date.isoformat(),
        "acquisition_date": asset.acquisition_date.isoformat(),
        "acquisition_cost": str(asset.acquisition_cost),
        "version": asset.version + 1,
        "affected_periods": preview.affected_periods,
        "before_pnl_effect": str(preview.before_pnl_effect),
        "after_pnl_effect": str(preview.after_pnl_effect),
    }
    revision = FixedAssetRevision(
        company_id=company_id,
        asset_id=asset.id,
        version=asset.version + 1,
        reason=body.reason,
        actor_id=actor_id,
        before_snapshot=before,
        after_snapshot=after,
        affected_periods=preview.affected_periods,
    )
    session.add(revision)
    await session.flush()
    await session.execute(
        select(func.set_config("jai.fixed_asset_revision_id", str(revision.id), True))
    )
    asset.in_service_date = body.in_service_date
    asset.version += 1
    result = await _read(session, asset)
    await session.commit()
    return result


async def list_asset_revisions(
    session: AsyncSession, company_id: uuid.UUID, asset_id: uuid.UUID
) -> list[FixedAssetRevisionRead]:
    await set_rls_company(session, company_id)
    if await session.scalar(select(FixedAsset.id).where(FixedAsset.id == asset_id)) is None:
        raise LookupError("Fixed asset not found.")
    rows = (
        await session.scalars(
            select(FixedAssetRevision)
            .where(FixedAssetRevision.asset_id == asset_id)
            .order_by(FixedAssetRevision.version)
        )
    ).all()
    return [
        FixedAssetRevisionRead(
            version=row.version,
            reason=row.reason,
            actor_id=row.actor_id,
            created_at=row.created_at,
            before_snapshot=row.before_snapshot,
            after_snapshot=row.after_snapshot,
            affected_periods=row.affected_periods,
        )
        for row in rows
    ]


async def preview_annual_revision(
    session: AsyncSession,
    company_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: FixedAssetAnnualRevisionInput,
) -> FixedAssetAnnualRevisionPreview:
    await set_rls_company(session, company_id)
    asset = await session.scalar(select(FixedAsset).where(FixedAsset.id == asset_id))
    if asset is None:
        raise LookupError("Fixed asset not found.")
    _ensure_mixed_asset(asset)
    _ensure_actual_period(year)
    row = await session.scalar(
        select(FixedAssetAnnualUse).where(
            FixedAssetAnnualUse.asset_id == asset_id, FixedAssetAnnualUse.year == year
        )
    )
    if row is None:
        raise LookupError("Annual use record not found.")
    if row.version != body.expected_version:
        raise FixedAssetConflict("Annual use changed; reload and preview the correction again.")
    if row.actual_business_percentage is None:
        raise FixedAssetConflict("Only confirmed actual use can be corrected.")
    before = _annual_read(row, asset, year)
    annuals = list(
        (
            await session.scalars(
                select(FixedAssetAnnualUse)
                .where(FixedAssetAnnualUse.asset_id == asset.id)
                .order_by(FixedAssetAnnualUse.year)
            )
        ).all()
    )
    period_deltas, affected_periods, _, monthly_period_deltas = _annual_projection_state(
        company_id, asset, annuals, year, body.business_percentage
    )
    from jai.services.money import quantize_to_minor_unit

    proposed = quantize_to_minor_unit(
        before.full_depreciation * body.business_percentage / Decimal("100")
    )
    return FixedAssetAnnualRevisionPreview(
        asset_id=asset.id,
        year=year,
        expected_version=row.version,
        before=before,
        after_business_percentage=body.business_percentage,
        after_net_pnl_effect=proposed,
        pnl_delta=proposed - before.net_pnl_effect,
        affected_periods=affected_periods,
        period_deltas=period_deltas,
        monthly_period_deltas=monthly_period_deltas,
        preview_token=_annual_revision_token(
            company_id, asset, row, annuals, body.business_percentage
        ),
    )


async def revise_annual_use(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    asset_id: uuid.UUID,
    year: int,
    body: FixedAssetAnnualRevisionInput,
) -> FixedAssetAnnualUseRead:
    await set_rls_company(session, company_id)
    _ensure_actual_period(year)
    asset = await session.scalar(
        select(FixedAsset).where(FixedAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise LookupError("Fixed asset not found.")
    _ensure_mixed_asset(asset)
    row = await session.scalar(
        select(FixedAssetAnnualUse)
        .where(FixedAssetAnnualUse.asset_id == asset_id, FixedAssetAnnualUse.year == year)
        .with_for_update()
    )
    if row is None:
        raise LookupError("Annual use record not found.")
    if row.version != body.expected_version:
        raise FixedAssetConflict("Annual use changed; reload and preview the correction again.")
    if row.actual_business_percentage is None:
        raise FixedAssetConflict("Only confirmed actual use can be corrected.")
    annuals = list(
        (
            await session.scalars(
                select(FixedAssetAnnualUse)
                .where(FixedAssetAnnualUse.asset_id == asset.id)
                .order_by(FixedAssetAnnualUse.year)
                .with_for_update()
            )
        ).all()
    )
    period_deltas, affected_periods, _, monthly_period_deltas = _annual_projection_state(
        company_id, asset, annuals, year, body.business_percentage
    )
    if not hmac.compare_digest(
        body.preview_token,
        _annual_revision_token(company_id, asset, row, annuals, body.business_percentage),
    ):
        raise FixedAssetConflict("Correction preview is stale or does not match these terms.")
    before = {
        "estimated_business_percentage": str(row.estimated_business_percentage),
        "actual_business_percentage": str(row.actual_business_percentage)
        if row.actual_business_percentage is not None
        else None,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
    }
    next_version = row.version + 1
    after = {
        "estimated_business_percentage": str(row.estimated_business_percentage),
        "actual_business_percentage": str(body.business_percentage),
        "version": next_version,
        "affected_periods": affected_periods,
        "period_deltas": [item.model_dump(mode="json") for item in period_deltas],
        "monthly_period_deltas": [item.model_dump(mode="json") for item in monthly_period_deltas],
    }
    session.add(
        FixedAssetAnnualRevision(
            company_id=company_id,
            annual_use_id=row.id,
            version=next_version,
            reason=body.reason,
            actor_id=actor_id,
            before_snapshot=before,
            after_snapshot=after,
        )
    )
    row.actual_business_percentage = body.business_percentage
    row.version = next_version
    row.confirmed_at = datetime.now(UTC)
    row.updated_at = row.confirmed_at
    await session.flush()
    result = _annual_read(row, asset, year)
    await session.commit()
    return result


async def list_annual_revisions(
    session: AsyncSession, company_id: uuid.UUID, asset_id: uuid.UUID, year: int
) -> list[FixedAssetAnnualRevisionRead]:
    await set_rls_company(session, company_id)
    row = await session.scalar(
        select(FixedAssetAnnualUse).where(
            FixedAssetAnnualUse.asset_id == asset_id, FixedAssetAnnualUse.year == year
        )
    )
    if row is None:
        return []
    revisions = (
        await session.scalars(
            select(FixedAssetAnnualRevision)
            .where(FixedAssetAnnualRevision.annual_use_id == row.id)
            .order_by(FixedAssetAnnualRevision.version)
        )
    ).all()
    return [
        FixedAssetAnnualRevisionRead(
            version=r.version,
            reason=r.reason,
            actor_id=r.actor_id,
            created_at=r.created_at,
            before_snapshot=r.before_snapshot,
            after_snapshot=r.after_snapshot,
        )
        for r in revisions
    ]
