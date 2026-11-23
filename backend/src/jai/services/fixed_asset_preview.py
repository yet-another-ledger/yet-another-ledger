"""Read-only fixed asset conversion preview and straight-line schedule math."""

from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models._enums import ExpenseKind, PlCostBasis, VatTreatmentEffect
from jai.models.expense import Expense
from jai.schemas.expense import (
    FixedAssetMonthlyDepreciation,
    FixedAssetPeriodDelta,
    FixedAssetPreviewRequest,
    FixedAssetPreviewResult,
    FixedAssetYearDelta,
)
from jai.services.expense import (
    _load_expense,
    compute_pl_cost_basis,
    compute_stored_cost_preview,
)
from jai.services.money import quantize_to_minor_unit
from jai.services.reporting.pl import _anniversary_date

_ZERO = Decimal("0.00")


def build_fixed_asset_preview(
    expense: Expense,
    request: FixedAssetPreviewRequest,
) -> FixedAssetPreviewResult:
    """Pure calculation for the proposed asset; it never mutates the purchase."""
    if ExpenseKind(expense.kind) != ExpenseKind.PURCHASE:
        raise ValueError("Only purchase expenses can be previewed as fixed assets.")
    if expense.is_draft:
        raise ValueError("Only confirmed purchase expenses can be previewed as fixed assets.")
    name = request.asset_name.strip()
    if not name:
        raise ValueError("Asset name must not be empty.")
    if request.use_type == "FULL_BUSINESS" and request.business_percentage != 100:
        raise ValueError("Full-business use requires 100% business use.")

    cost = compute_pl_cost_basis(
        Decimal(str(expense.base_net_amount)),
        Decimal(str(expense.base_vat_amount)),
        VatTreatmentEffect(expense.vat_treatment_effect),
        expense.deductible,
        PlCostBasis.AUTO,
        expense.vat_treatment_code,
        Decimal(str(expense.base_gross_amount)),
    )
    residual = request.residual_value
    if residual > cost:
        raise ValueError("Residual value cannot exceed acquisition cost.")
    depreciable = cost - residual
    unrounded_annual = depreciable / Decimal(request.useful_life_years)
    annual = quantize_to_minor_unit(unrounded_annual)
    if unrounded_annual > cost * Decimal("0.20"):
        raise ValueError("Annual depreciation cannot exceed 20% of acquisition cost.")

    schedule: list[FixedAssetMonthlyDepreciation] = []
    if request.in_service_date is not None:
        if request.in_service_date < expense.expense_date:
            raise ValueError("In-service date cannot be earlier than the purchase date.")
        if (
            request.in_service_date.year + request.useful_life_years
            > expense.expense_date.year + 100
        ):
            raise ValueError("In-service date and useful life exceed the 100-year preview window.")
        if request.in_service_date.year + request.useful_life_years > 9999:
            raise ValueError("Useful life extends beyond the supported calendar range.")
        cumulative = _ZERO
        business_cumulative = _ZERO
        start = request.in_service_date
        schedule_months = request.useful_life_years * 12 + (1 if start.day > 1 else 0)
        first_month_days = monthrange(start.year, start.month)[1]
        first_month_weight = first_month_days - start.day + 1
        total_weight = request.useful_life_years * 12 * first_month_days
        for offset in range(schedule_months):
            month_index = start.month - 1 + offset
            year = start.year + month_index // 12
            month = month_index % 12 + 1
            days_in_month = monthrange(year, month)[1]
            cumulative_weight = first_month_weight + offset * first_month_days
            target_cumulative = (
                depreciable
                if offset == schedule_months - 1
                else quantize_to_minor_unit(
                    depreciable * Decimal(cumulative_weight) / Decimal(total_weight)
                )
            )
            amount = target_cumulative - cumulative
            cumulative += amount
            target_business_cumulative = (
                quantize_to_minor_unit(depreciable * request.business_percentage / Decimal("100"))
                if offset == schedule_months - 1
                else quantize_to_minor_unit(
                    cumulative * request.business_percentage / Decimal("100")
                )
            )
            business_amount = target_business_cumulative - business_cumulative
            business_cumulative = target_business_cumulative
            schedule.append(
                FixedAssetMonthlyDepreciation(
                    period_end=date(year, month, days_in_month),
                    amount=amount,
                    business_amount=business_amount,
                    private_use_reduction=amount - business_amount,
                    opening_book_value=cost - cumulative + amount,
                    cumulative_amount=cumulative,
                    closing_book_value=cost - cumulative,
                )
            )
    legacy = compute_stored_cost_preview(expense).annual_cost
    as_of = date.today()
    # Cover every proposed schedule month and every supported legacy anniversary
    # through the 100-year preview horizon. M10 can represent longer terms, so
    # report when its remaining anniversaries fall outside this bounded view.
    legacy_term_end_year = expense.expense_date.year + int(expense.depreciation_years) - 1
    preview_horizon_year = min(expense.expense_date.year + 100, 9999)
    schedule_end_year = schedule[-1].period_end.year if schedule else expense.expense_date.year
    last_report_year = max(schedule_end_year, min(legacy_term_end_year, preview_horizon_year))
    last_report_year = min(last_report_year, 9999)
    years = range(expense.expense_date.year, last_report_year + 1)
    # The first period includes the purchase anniversary, which can precede
    # month-end (and is included by an M10 range ending before that month-end).
    preview_start = date(expense.expense_date.year, expense.expense_date.month, 1)
    preview_end = date(last_report_year, 12, 31)
    legacy_preview_truncated = bool(
        expense.include_in_pl and legacy_term_end_year > last_report_year
    )
    deltas: list[FixedAssetYearDelta] = []
    prior_years_affected: list[int] = []
    period_deltas: list[FixedAssetPeriodDelta] = []
    period_count = (
        (last_report_year - expense.expense_date.year) * 12 + (12 - expense.expense_date.month) + 1
    )
    # Report baseline follows M10's requested-range attribution (including
    # future legacy anniversaries); asset report contribution only includes
    # completed months. Period plan values contain only future events.
    period_values: dict[date, dict[str, Decimal]] = {}
    legacy_dates: dict[date, date] = {}
    asset_dates: dict[date, date] = {}
    for index in range(period_count):
        month_index = expense.expense_date.month - 1 + index
        year = expense.expense_date.year + month_index // 12
        month = month_index % 12 + 1
        end = date(year, month, monthrange(year, month)[1])
        period_values[end] = {
            "legacy_actual": _ZERO,
            "legacy_planned": _ZERO,
            "asset_actual": _ZERO,
            "asset_planned": _ZERO,
        }
    for row in schedule:
        if row.period_end in period_values:
            key = "asset_actual" if row.period_end <= as_of else "asset_planned"
            period_values[row.period_end][key] += row.business_amount
            asset_dates[row.period_end] = row.period_end
    if expense.include_in_pl:
        anniversary_year_count = min(
            int(expense.depreciation_years),
            last_report_year - expense.expense_date.year + 1,
            10000 - expense.expense_date.year,
        )
        for offset in range(anniversary_year_count):
            recognized_date = _anniversary_date(
                expense.expense_date, expense.expense_date.year + offset
            )
            period_end = date(
                recognized_date.year,
                recognized_date.month,
                monthrange(recognized_date.year, recognized_date.month)[1],
            )
            if period_end in period_values:
                period_values[period_end]["legacy_actual"] += legacy
                if recognized_date > as_of:
                    period_values[period_end]["legacy_planned"] += legacy
                legacy_dates[period_end] = recognized_date
    for period_end, values in period_values.items():
        old_period_amount = values["legacy_actual"]
        planned_old_amount = values["legacy_planned"]
        new_period_amount = values["asset_actual"]
        planned_period_amount = values["asset_planned"]
        period_deltas.append(
            FixedAssetPeriodDelta(
                period_end=period_end,
                legacy_attribution_date=legacy_dates.get(period_end),
                fixed_asset_attribution_date=asset_dates.get(period_end),
                legacy_expense_amount=old_period_amount,
                planned_legacy_expense_amount=planned_old_amount,
                fixed_asset_depreciation=new_period_amount,
                planned_fixed_asset_depreciation=planned_period_amount,
                difference=new_period_amount - old_period_amount,
                planned_difference=planned_period_amount - planned_old_amount,
                completed_period=period_end <= as_of,
            )
        )
    period_deltas.sort(key=lambda row: row.period_end)
    opening_value = cost
    planned_opening_value = cost
    for year in years:
        is_legacy_year = expense.include_in_pl and year <= legacy_term_end_year
        planned_old_amount = legacy if is_legacy_year else _ZERO
        # M10 reports legacy expense by anniversary inside the requested
        # reporting range, even when that range extends beyond today.
        old_amount = planned_old_amount
        year_rows = [row for row in schedule if row.period_end.year == year]
        asset_amount = sum((row.amount for row in year_rows), _ZERO)
        completed_year_rows = [row for row in year_rows if row.period_end <= as_of]
        realized_amount = sum((row.amount for row in completed_year_rows), _ZERO)
        new_amount = sum((row.business_amount for row in completed_year_rows), _ZERO)
        planned_business_amount = sum((row.business_amount for row in year_rows), _ZERO)
        closing_value = opening_value - realized_amount
        planned_closing_value = planned_opening_value - asset_amount
        difference = new_amount - old_amount
        if year < as_of.year and (old_amount != 0 or new_amount != 0):
            prior_years_affected.append(year)
        deltas.append(
            FixedAssetYearDelta(
                year=year,
                legacy_expense_amount=old_amount,
                planned_legacy_expense_amount=planned_old_amount,
                asset_depreciation=asset_amount,
                business_use_reduction=realized_amount - new_amount,
                fixed_asset_depreciation=new_amount,
                realized_asset_depreciation=realized_amount,
                planned_fixed_asset_depreciation=planned_business_amount,
                planned_difference=planned_business_amount - planned_old_amount,
                planned_business_use_reduction=asset_amount - planned_business_amount,
                difference=difference,
                opening_book_value=opening_value,
                planned_opening_book_value=planned_opening_value,
                cumulative_depreciation=cost - closing_value,
                closing_book_value=closing_value,
                planned_closing_book_value=planned_closing_value,
            )
        )
        opening_value = closing_value
        planned_opening_value = planned_closing_value

    return FixedAssetPreviewResult(
        expense_id=expense.id,
        expense_date=expense.expense_date,
        acquisition_cost=cost,
        residual_value=residual,
        depreciable_amount=depreciable,
        annual_baseline=annual,
        legacy_annual_cost=legacy,
        pending_in_service=request.in_service_date is None,
        prior_years_affected=prior_years_affected,
        conversion_blocked=bool(prior_years_affected),
        conversion_block_reason=(
            "Conversion would change profit and loss in earlier calendar year(s): "
            + ", ".join(str(year) for year in prior_years_affected)
            if prior_years_affected
            else None
        ),
        reporting_as_of=as_of,
        preview_period_start=preview_start,
        preview_period_end=preview_end,
        legacy_preview_truncated=legacy_preview_truncated,
        period_deltas=period_deltas,
        monthly_schedule=schedule,
        yearly_deltas=deltas,
    )


async def preview_expense_fixed_asset(
    session: AsyncSession,
    expense_id: uuid.UUID,
    company_id: uuid.UUID,
    request: FixedAssetPreviewRequest,
) -> FixedAssetPreviewResult:
    """Load a tenant-scoped expense and calculate a read-only conversion preview."""
    await set_rls_company(session, company_id)
    expense = await _load_expense(session, expense_id, company_id)
    return build_fixed_asset_preview(expense, request)
