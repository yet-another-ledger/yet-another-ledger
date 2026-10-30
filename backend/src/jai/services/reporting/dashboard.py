"""Dashboard summary service (M10 step 5).

Public API
----------
``compute_dashboard(session, company, year, tiers)``
    Aggregate KPI / monthly series / top expense categories into a
    ``DashboardSummary`` by composing the three existing reporting services:

      - ``compute_profit_loss``   → KPI + monthly series
      - ``compute_vat_return``    → current-quarter VAT payable
      - ``compute_expense_report`` → top 5 expense categories by net

Design decisions (M10.md step 5 / D1–D6)
-----------------------------------------
D1  Pure-read: delegates entirely to the three existing services.
D6  No secondary rounding: amounts come pre-rounded from the sub-services.

KPI binding
-----------
- ``ytd_revenue / ytd_expense / ytd_profit``:
    = ``compute_profit_loss(year 01-01 … 12-31, granularity='month')``
      ``.revenue_net / .expense_actual / .profit``
  Invariant: ytd_* == Σ monthly (guaranteed because both come from the same
  P/L call — the top-level totals are built by summing the monthly buckets).

- ``current_quarter_vat_payable``:
    = ``compute_vat_return(year, Q).totals.net_payable_or_refundable.vat``
  Q selection (⚠️ ASSUMPTION – explicitly documented):
    - If ``year`` == the current calendar year: Q = the quarter that today
      falls in (1, 2, 3 or 4).
    - If ``year`` != the current calendar year: Q = 4 (the last quarter of the
      selected year) — the assumption is that for past years the author wants
      the final quarterly figure.
  Rationale: the project has no future-dated data, so "current quarter" is
  only meaningful for the current year; for prior years Q4 is the natural
  year-end summary.

Monthly series
--------------
The ``monthly`` list always contains exactly 12 items (Jan through Dec) for the
selected year.  Each item's ``revenue / expense / profit`` correspond to the
matching bucket from the P/L monthly series.  Months with no data are 0.

Top expense categories
----------------------
``top_expense_categories``:
    = ``compute_expense_report(year 01-01 … 12-31)``
      ``.by_category`` sorted descending by ``net``, top 5.
  If fewer than 5 categories exist, all are returned.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.schemas.report import (
    DashboardKpi,
    DashboardMonthly,
    DashboardSummary,
    DashboardTopCategory,
)
from jai.schemas.setting import VatRateTiers
from jai.services.reporting.btw import compute_vat_return
from jai.services.reporting.expenses import compute_expense_report
from jai.services.reporting.pl import compute_profit_loss

if TYPE_CHECKING:
    from jai.models.company import Company

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_TOP_N = 5


# ---------------------------------------------------------------------------
# Quarter selection helper
# ---------------------------------------------------------------------------


def _select_quarter(year: int) -> int:
    """Return the quarter to use for the VAT payable KPI.

    ⚠️ ASSUMPTION (documented in module docstring):
    - year == current year → quarter that today falls in.
    - year != current year → 4 (year-end summary).
    """
    today = datetime.now().date()
    if year == today.year:
        return (today.month - 1) // 3 + 1
    return 4


# ---------------------------------------------------------------------------
# Main service function
# ---------------------------------------------------------------------------


async def compute_dashboard(
    session: AsyncSession,
    company: Company,
    year: int,
    tiers: VatRateTiers,
) -> DashboardSummary:
    """Compute the dashboard summary for *company* and *year*.

    Parameters
    ----------
    session:
        Read-only async SQLAlchemy session.
    company:
        Company ORM object (needs ``id`` and ``country_code`` for VAT).
    year:
        Calendar year for which to compute the dashboard.
    tiers:
        VatRateTiers for the BTW call (hoog/laag/zero thresholds).

    Returns
    -------
    DashboardSummary
        KPI, 12-month series, top-5 expense categories.
    """
    company_id: uuid.UUID = company.id
    # P/L reaches RLS-protected M12 correction rows before BTW establishes its
    # own tenant context, so the composed dashboard must scope the transaction
    # up front.
    await set_rls_company(session, company_id)
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    # ------------------------------------------------------------------
    # 1. P/L for the full year – monthly granularity
    #    Provides: KPI totals + the 12-month series in one call.
    # ------------------------------------------------------------------
    pl = await compute_profit_loss(
        session,
        company_id=company_id,
        date_from=year_start,
        date_to=year_end,
        granularity="month",
    )

    # Build a quick lookup from period label → series item
    pl_by_period: dict[str, object] = {item.period: item for item in pl.series}

    monthly: list[DashboardMonthly] = []
    for month_num in range(1, 13):
        period_label = f"{year:04d}-{month_num:02d}-01"
        item = pl_by_period.get(period_label)
        if item is not None:
            # item is a ProfitLossSeriesItem
            from jai.schemas.report import ProfitLossSeriesItem

            assert isinstance(item, ProfitLossSeriesItem)
            rev = item.revenue_net
            exp_actual = item.expense_actual
            profit = item.profit
        else:
            rev = _ZERO
            exp_actual = _ZERO
            profit = _ZERO
        monthly.append(
            DashboardMonthly(
                month=period_label,
                revenue=rev,
                expense=exp_actual,
                profit=profit,
            )
        )

    # ------------------------------------------------------------------
    # 2. VAT return – current quarter (Q selection per module assumption)
    # ------------------------------------------------------------------
    quarter = _select_quarter(year)
    vat_return = await compute_vat_return(
        session,
        company=company,
        year=year,
        quarter=quarter,
        tiers=tiers,
    )
    vat_payable = vat_return.totals.net_payable_or_refundable.vat

    # ------------------------------------------------------------------
    # 3. Expense report – full year → top 5 by net (descending)
    # ------------------------------------------------------------------
    expense_rpt = await compute_expense_report(
        session,
        company_id=company_id,
        date_from=year_start,
        date_to=year_end,
        include_in_pl=True,
    )

    sorted_categories = sorted(
        expense_rpt.by_category,
        key=lambda r: r.net,
        reverse=True,
    )
    top_categories: list[DashboardTopCategory] = [
        DashboardTopCategory(
            category_id=row.category_id,
            category_name=row.category_name,
            net=row.net,
        )
        for row in sorted_categories[:_TOP_N]
    ]

    # ------------------------------------------------------------------
    # 4. Assemble KPI
    # ------------------------------------------------------------------
    kpi = DashboardKpi(
        ytd_revenue=pl.revenue_net,
        ytd_expense=pl.expense_actual,
        ytd_profit=pl.profit,
        current_quarter_vat_payable=vat_payable,
    )

    return DashboardSummary(
        year=year,
        quarter=quarter,
        kpi=kpi,
        monthly=monthly,
        top_expense_categories=top_categories,
    )
