"""Reports API routes (M10 steps 1 & 2).

Endpoints (all GET, owner-only, requires MFA):
  GET /api/v1/reports/profit-loss  – P/L report with time series.
  GET /api/v1/reports/vat-return   – BTW VAT return summary (NL ruleset).

Design:
- Thin controller: validate parameters, call services/reporting, return schema.
- ``from`` is a Python reserved word; accepted via ``Query(..., alias="from")``
  and exposed as ``from`` in OpenAPI (D-alias pattern).
- Auth pattern mirrors api/payments.py: ``current_mfa_user`` + ``_owner_only``
  + ``_require_company_id``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.auth.deps import current_mfa_user
from jai.db import get_session, set_rls_company
from jai.models._enums import SettingLevel
from jai.models.company import Company
from jai.models.user import User
from jai.schemas.report import (
    DashboardSummary,
    ExpenseReport,
    IcpReport,
    ProfitLossReport,
    VatReturnReport,
)
from jai.schemas.setting import SETTING_KEY_VAT_RATE_TIERS, VatRateTiers
from jai.services.reporting.btw import compute_vat_return
from jai.services.reporting.dashboard import compute_dashboard
from jai.services.reporting.expenses import compute_expense_report
from jai.services.reporting.icp import compute_icp
from jai.services.reporting.pl import compute_profit_loss
from jai.services.settings import get_setting

router = APIRouter(prefix="/api/v1", tags=["reports"])


# ---------------------------------------------------------------------------
# Auth helpers (mirror payments.py pattern)
# ---------------------------------------------------------------------------


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
# P/L report
# ---------------------------------------------------------------------------


@router.get(
    "/reports/profit-loss",
    response_model=ProfitLossReport,
    response_model_by_alias=True,
)
async def get_profit_loss(
    date_from: Annotated[date, Query(alias="from", description="Inclusive start date.")],
    date_to: Annotated[date, Query(alias="to", description="Inclusive end date.")],
    granularity: Literal["month", "quarter"] = Query(
        default="month",
        description="Time-series bucket size: 'month' or 'quarter'.",
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ProfitLossReport:
    """Return a P/L (profit & loss) report for the given date range.

    Revenue is aggregated from invoices with status SENT or COMPLETED,
    using ``base_taxable_amount`` (net EUR, post-discount).

    Expenses are aggregated from confirmed (``is_draft=false``) expenses
    with straight-line depreciation prorated by ``business_percentage``.
    Each expense contributes an annual slice to the year of its purchase
    anniversary within the requested window.

    Returns a top-level summary plus a time-series breakdown by month
    or quarter.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    await set_rls_company(session, company_id)

    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'from' must not be after 'to'.",
        )

    return await compute_profit_loss(
        session,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
    )


# ---------------------------------------------------------------------------
# VAT return report (M10 step 2)
# ---------------------------------------------------------------------------


@router.get(
    "/reports/vat-return",
    response_model=VatReturnReport,
    response_model_by_alias=True,
)
async def get_vat_return(
    year: int = Query(
        description="Calendar year of the VAT return period (e.g. 2026).",
        ge=2000,
        le=2100,
    ),
    quarter: int = Query(
        description="Quarter of the VAT return period (1–4).",
        ge=1,
        le=4,
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> VatReturnReport:
    """Return the BTW (VAT) quarterly return summary for the given year and quarter.

    Aggregates invoice VAT snapshots and expense VAT amounts into the standard
    Dutch BTW return boxes (1a/1b/1c/1d/1e/2a/3a/3b/3c/4a/4b/5b) using the
    NL ruleset.  Non-NL companies fall back to the NL ruleset with a warning.

    The rate tier thresholds (hoog/laag/zero) are read from the company-level
    ``reporting.vat_rate_tiers`` setting (defaults: hoog=21, laag=9, zero=0).

    Box 1d (private-use correction) is non-zero only when quarter == 4 and is
    based on the full calendar year's expenses.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    await set_rls_company(session, company_id)

    # Fetch the company record (needed for country_code).
    stmt_co = select(Company).where(Company.id == company_id)
    result_co = await session.execute(stmt_co)
    company = result_co.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company profile not found.",
        )

    # Load VAT rate tier thresholds from the company-level setting (with defaults).
    tiers_setting = await get_setting(
        session,
        SETTING_KEY_VAT_RATE_TIERS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=VatRateTiers,
    )
    tiers = tiers_setting if tiers_setting is not None else VatRateTiers()

    return await compute_vat_return(
        session,
        company=company,
        year=year,
        quarter=quarter,
        tiers=tiers,
    )


# ---------------------------------------------------------------------------
# ICP report (M10 step 3)
# ---------------------------------------------------------------------------


@router.get(
    "/reports/icp",
    response_model=IcpReport,
)
async def get_icp_report(
    year: int = Query(
        description="Calendar year of the ICP report period (e.g. 2026).",
        ge=2000,
        le=2100,
    ),
    quarter: int = Query(
        description="Quarter of the ICP report period (1–4).",
        ge=1,
        le=4,
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> IcpReport:
    """Return the ICP (Opgaaf ICP) quarterly report for the given year and quarter.

    Lists all EU-B2B reverse-charge invoices (``requires_icp=True``) grouped
    by customer.  The ``total_net`` must equal the BTW box 3b net amount for
    the same quarter (guide §3.2: '3b ≡ Opgaaf ICP').

    Warnings are generated for customers missing a VAT ID or billing
    country code – both are required for the official Opgaaf ICP filing.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    await set_rls_company(session, company_id)

    # Fetch the company record.
    stmt_co = select(Company).where(Company.id == company_id)
    result_co = await session.execute(stmt_co)
    company = result_co.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company profile not found.",
        )

    return await compute_icp(
        session,
        company=company,
        year=year,
        quarter=quarter,
    )


# ---------------------------------------------------------------------------
# Expense report (M10 step 4)
# ---------------------------------------------------------------------------


@router.get(
    "/reports/expenses",
    response_model=ExpenseReport,
    response_model_by_alias=True,
)
async def get_expense_report(
    date_from: Annotated[date, Query(alias="from", description="Inclusive start date.")],
    date_to: Annotated[date, Query(alias="to", description="Inclusive end date.")],
    include_in_pl: bool | None = Query(default=None),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseReport:
    """Return an expense report aggregated by category for the given date range.

    Only confirmed expenses (``is_draft=false``) are included, filtered by
    ``expense_date`` within [from, to] (inclusive).

    Each ``by_category`` row reports the raw base-currency amounts (net, VAT,
    gross) and the deductible / non-deductible split of net.  Amounts are the
    original recorded amounts, *not* prorated by ``business_percentage`` or
    ``depreciation_years`` (that is P/L scope, not raw expense reporting).

    When a category has been deleted, its expenses are grouped by the preserved
    ``category_name`` snapshot; same-name snapshots are merged into one row.
    """
    _owner_only(user)
    company_id = _require_company_id(user)
    await set_rls_company(session, company_id)

    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'from' must not be after 'to'.",
        )

    return await compute_expense_report(
        session,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        include_in_pl=include_in_pl,
    )


# ---------------------------------------------------------------------------
# Dashboard summary (M10 step 5)
# ---------------------------------------------------------------------------


@router.get(
    "/reports/dashboard",
    response_model=DashboardSummary,
)
async def get_dashboard(
    year: int = Query(
        description="Calendar year for the dashboard (e.g. 2026).",
        ge=2000,
        le=2100,
    ),
    user: User = Depends(current_mfa_user),
    session: AsyncSession = Depends(get_session),
) -> DashboardSummary:
    """Return the dashboard summary for the given year.

    Aggregates:
    - KPI (YTD revenue / expense / profit, current-quarter VAT payable)
    - Monthly series (12 months Jan–Dec)
    - Top 5 expense categories by net amount

    All numbers are derived from the same underlying P/L, BTW, and expense
    report services so they are guaranteed consistent with those sub-reports.

    ``current_quarter_vat_payable`` uses:
    - The current quarter (today's quarter) when ``year`` == current year.
    - Q4 (year-end) when ``year`` is a prior year.
    """
    _owner_only(user)
    company_id = _require_company_id(user)

    # Fetch the company record (needed for country_code in VAT service).
    stmt_co = select(Company).where(Company.id == company_id)
    result_co = await session.execute(stmt_co)
    company = result_co.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company profile not found.",
        )

    # Load VAT rate tier thresholds (with defaults).
    tiers_setting = await get_setting(
        session,
        SETTING_KEY_VAT_RATE_TIERS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=VatRateTiers,
    )
    tiers = tiers_setting if tiers_setting is not None else VatRateTiers()

    return await compute_dashboard(
        session,
        company=company,
        year=year,
        tiers=tiers,
    )
