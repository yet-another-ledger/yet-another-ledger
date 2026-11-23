"""Fixed asset conversion and annual register reads."""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models.fixed_asset import FixedAsset
from jai.schemas.expense import (
    FixedAssetConfirmRequest,
    FixedAssetRead,
    FixedAssetYearDelta,
)
from jai.services.expense import _load_expense
from jai.services.fixed_asset_preview import (
    FixedAssetConflict,
    build_fixed_asset_preview,
    build_monthly_fixed_asset_schedule,
)

_PREVIEW_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


async def _read(
    session: AsyncSession, asset: FixedAsset, year: int | None = None
) -> FixedAssetRead:
    requested_year = year if year is not None else date.today().year
    projection_year = max(requested_year, asset.acquisition_date.year)
    rows = (
        build_monthly_fixed_asset_schedule(
            asset.acquisition_cost,
            asset.residual_value,
            asset.useful_life_years,
            asset.in_service_date,
            asset.business_percentage,
        )
        if asset.in_service_date is not None
        else []
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
            legacy_expense_amount=Decimal("0"),
            planned_legacy_expense_amount=Decimal("0"),
            asset_depreciation=full,
            business_use_reduction=full - planned,
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
        name=asset.name,
        acquisition_date=asset.acquisition_date,
        in_service_date=asset.in_service_date,
        acquisition_cost=asset.acquisition_cost,
        residual_value=asset.residual_value,
        useful_life_years=asset.useful_life_years,
        business_percentage=asset.business_percentage,
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
    if request.use_type != "FULL_BUSINESS" or request.business_percentage != 100:
        raise ValueError("Step 2 conversion supports fully business assets only.")
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
        ):
            return await _read(session, existing)
        raise ValueError("This purchase is already registered with different fixed asset terms.")
    from jai.models.battery import BatteryAsset

    if await session.scalar(
        select(BatteryAsset.id).where(BatteryAsset.acquisition_expense_id == expense_id)
    ) is not None:
        raise FixedAssetConflict(
            "Battery-linked purchases require the M20 Step 3 shared asset workflow."
        )
    preview = build_fixed_asset_preview(expense, request, company_id=company_id)
    if (
        not _PREVIEW_TOKEN_PATTERN.fullmatch(request.preview_token)
        or not hmac.compare_digest(request.preview_token, preview.preview_token)
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
        created_by=actor_id,
    )
    session.add(asset)
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
            select(FixedAsset).order_by(
                FixedAsset.acquisition_date.desc(), FixedAsset.name
            )
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
        await session.execute(
            select(FixedAsset).where(FixedAsset.id == asset_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("Fixed asset not found.")
    if year is not None and year < row.acquisition_date.year:
        raise LookupError("Fixed asset was not registered in the requested year.")
    return await _read(session, row, year)
