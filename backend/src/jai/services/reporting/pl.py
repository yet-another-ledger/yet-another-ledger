"""P/L (profit & loss) reporting service (M10 step 1).

Public API
----------
``compute_profit_loss(session, company_id, date_from, date_to, granularity)``
    Aggregate revenues and expenses into a ``ProfitLossReport``.

Design decisions (see M10.md D1–D6, D-DEP, D-PCT)
---------------------------------------------------
D1  Pure-read: only SELECT queries, no writes.
D2  Accrual basis by document date (invoice_date / expense_date).
D3  Revenue: status ∈ {SENT, COMPLETED}; Cost: is_draft = false.
D4  All amounts from ``base_*`` columns (EUR, exchange_rate = 1 in v1).
D6  Sum already-persisted to-the-cent values; never re-round the aggregate.

Depreciation / business% (D-DEP, D-PCT)
----------------------------------------
For each qualifying expense the **annual depreciation slice** is:

    annual_slice = base_net_amount × (business_percentage / 100) ÷ depreciation_years

Each per-expense annual slice is quantised to the currency's minor unit
(EUR = 2 decimal places, ROUND_HALF_UP) immediately after calculation,
before accumulation into buckets.  This aligns with the M7.5 "to-the-cent"
direction (D6) and keeps ``expense_actual`` / ``profit`` at a consistent
2-decimal precision throughout the report.

Sub-bucket assignment rule (⚠️ ASSUMPTION – explicitly documented)
-------------------------------------------------------------------
The design document (M10.md) specifies "straight-line depreciation by year"
but does **not** specify how the annual slice is assigned to sub-annual
time buckets (months / quarters).

**Assumption adopted here (v1)**:
  The annual slice for year Y is attributed to the bucket that contains the
  "purchase anniversary date" = ``expense_date`` with the year component
  replaced by Y.  Special case: if ``expense_date`` is Feb 29 and year Y is
  not a leap year, the anniversary falls on Feb 28.

  Example:
    expense_date = 2024-03-15, depreciation_years = 3
    → Slices:
      - Year 2024: attributed to 2024-03-15  (Q1-2024 or March 2024)
      - Year 2025: attributed to 2025-03-15  (Q1-2025 or March 2025)
      - Year 2026: attributed to 2026-03-15  (Q1-2026 or March 2026)

  Consequence: the sum of all buckets that fall within [date_from, date_to]
  equals ``expense_actual`` in the top-level report (since only anniversary
  dates inside the window are included).

  Rationale: simple, deterministic, easy to test.  It mirrors the intuition
  that "the cost is recognised on the same day of the year as the purchase"
  and avoids arbitrary start-of-year / end-of-year attribution.

Granularity
-----------
- ``month``   → bucket key = (year, month); series label = "YYYY-MM-01"
- ``quarter`` → bucket key = (year, quarter); series label = quarter-start date
  e.g. Q1 → "YYYY-01-01", Q2 → "YYYY-04-01", Q3 → "YYYY-07-01", Q4 → "YYYY-10-01"
"""

from __future__ import annotations

import uuid
from calendar import isleap
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models._enums import (
    ExpenseKind,
    InvoiceDocumentKind,
    InvoiceStatus,
    PlCostBasis,
    VatTreatmentEffect,
)
from jai.models.document import InvoiceCorrection
from jai.models.expense import Expense
from jai.models.external_sales import ExternalSale
from jai.models.invoice import Invoice
from jai.schemas.report import ExternalSaleReportReference, ProfitLossReport, ProfitLossSeriesItem
from jai.services.expense import compute_annual_pl_cost, compute_pl_cost_basis
from jai.services.money import quantize_to_minor_unit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _quarter_start_month(month: int) -> int:
    """Return the first month of the quarter that *month* belongs to."""
    return ((month - 1) // 3) * 3 + 1


def _bucket_key(d: date, granularity: Literal["month", "quarter"]) -> tuple[int, int]:
    """Return a sortable (year, sub-period) key for a date.

    For ``month``:   returns (year, month)     → e.g. (2025, 3)
    For ``quarter``: returns (year, quarter)   → e.g. (2025, 1)
    """
    if granularity == "month":
        return (d.year, d.month)
    else:
        return (d.year, (d.month - 1) // 3 + 1)


def _bucket_label(key: tuple[int, int], granularity: Literal["month", "quarter"]) -> str:
    """Convert a bucket key back to the bucket's start-date ISO string."""
    year, sub = key
    if granularity == "month":
        return f"{year:04d}-{sub:02d}-01"
    else:
        start_month = (sub - 1) * 3 + 1
        return f"{year:04d}-{start_month:02d}-01"


def _anniversary_date(expense_date: date, year: int) -> date:
    """Return the anniversary of *expense_date* in *year*.

    If expense_date is Feb 29 and *year* is not a leap year, returns Feb 28.
    """
    month = expense_date.month
    day = expense_date.day
    if month == 2 and day == 29 and not isleap(year):
        day = 28
    return date(year, month, day)


def _annual_slice(expense: Expense) -> Decimal:
    """Compute the annual depreciation slice for one expense, rounded to cents.

    annual_slice = base_net_amount × (business_percentage / 100) ÷ depreciation_years

    The result is immediately quantised to the currency's minor unit (EUR = 2 dp,
    ROUND_HALF_UP) so that every per-expense slice entering the accumulation
    buckets is already at "to-the-cent" precision (M7.5 / D6).
    """
    if ExpenseKind(expense.kind) == ExpenseKind.MILEAGE:
        net = Decimal(str(expense.base_net_amount))
    else:
        net = compute_pl_cost_basis(
            Decimal(str(expense.base_net_amount)),
            Decimal(str(expense.base_vat_amount)),
            VatTreatmentEffect(expense.vat_treatment_effect),
            expense.deductible,
            PlCostBasis(expense.pl_cost_basis),
            expense.vat_treatment_code,
            Decimal(str(expense.base_gross_amount)),
        )
    biz_pct = Decimal(str(expense.business_percentage))
    return compute_annual_pl_cost(
        net, biz_pct, int(expense.depreciation_years), expense.include_in_pl
    )


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


def _build_empty_buckets(
    date_from: date,
    date_to: date,
    granularity: Literal["month", "quarter"],
) -> dict[tuple[int, int], dict[str, Decimal]]:
    """Pre-populate bucket dict with zero values for every period in range.

    Ensures the series is contiguous (no gaps) even for periods with no data.
    """
    buckets: dict[tuple[int, int], dict[str, Decimal]] = {}
    # Walk month by month to enumerate all periods.
    year, month = date_from.year, date_from.month
    end_year, end_month = date_to.year, date_to.month
    while (year, month) <= (end_year, end_month):
        key = _bucket_key(date(year, month, 1), granularity)
        if key not in buckets:
            buckets[key] = {"revenue_net": _ZERO, "expense_actual": _ZERO}
        # Advance month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return buckets


async def compute_profit_loss(
    session: AsyncSession,
    company_id: uuid.UUID,
    date_from: date,
    date_to: date,
    granularity: Literal["month", "quarter"],
) -> ProfitLossReport:
    """Compute the P/L report for [date_from, date_to] for *company_id*.

    Parameters
    ----------
    session:
        Async SQLAlchemy session (read-only usage).
    company_id:
        Tenant scope.
    date_from, date_to:
        Inclusive date range (D2: by document date).
    granularity:
        ``'month'`` or ``'quarter'``.

    Returns
    -------
    ProfitLossReport
        Aggregated report with series and top-level totals.
    """
    await set_rls_company(session, company_id)
    buckets = _build_empty_buckets(date_from, date_to, granularity)

    # -------------------------------------------------------------------
    # 1. Revenue: qualifying invoices (D3, D4)
    # -------------------------------------------------------------------
    revenue_statuses = [InvoiceStatus.SENT, InvoiceStatus.COMPLETED]
    stmt_inv = select(Invoice).where(
        and_(
            Invoice.company_id == company_id,
            # Advance never creates revenue; Final is the full edited project
            # event (not its residual payable amount).
            Invoice.document_kind.in_([
                InvoiceDocumentKind.STANDARD,
                InvoiceDocumentKind.FINAL,
            ]),
            Invoice.status.in_(revenue_statuses),
            Invoice.invoice_date >= date_from,
            Invoice.invoice_date <= date_to,
        )
    )
    result_inv = await session.execute(stmt_inv)
    invoices = result_inv.scalars().all()

    for inv in invoices:
        inv_date = inv.invoice_date
        key = _bucket_key(inv_date, granularity)
        # D4: use base_taxable_amount (EUR base, post-discount taxable net).
        # Quantise to minor unit to normalise DB NUMERIC(18,3) scale → 2 dp,
        # keeping revenue_net / profit output at the same precision as
        # expense_actual (M7.5 / D6).  The value is already to-the-cent from
        # M7.5 line-level rounding, so this is a pure scale normalisation
        # (no numeric change).
        amount = quantize_to_minor_unit(Decimal(str(inv.base_taxable_amount)))
        if key in buckets:
            buckets[key]["revenue_net"] = buckets[key]["revenue_net"] + amount

    # Credit revenue timing is an immutable issue-time fact.  In particular,
    # a pre-Final Advance Credit has affects_revenue=false and must not reduce
    # P/L, while its later Final naturally restates the project net.
    credit_result = await session.execute(
        select(Invoice, InvoiceCorrection)
        .join(InvoiceCorrection, InvoiceCorrection.credit_note_id == Invoice.id)
        .where(
            Invoice.company_id == company_id,
            Invoice.document_kind == InvoiceDocumentKind.CREDIT_NOTE,
            Invoice.status.in_(revenue_statuses),
            InvoiceCorrection.affects_revenue.is_(True),
            Invoice.invoice_date >= date_from,
            Invoice.invoice_date <= date_to,
        )
    )
    for credit, correction in credit_result.all():
        key = _bucket_key(credit.invoice_date, granularity)
        if key in buckets:
            buckets[key]["revenue_net"] = buckets[key]["revenue_net"] - quantize_to_minor_unit(
                Decimal(str(correction.issued_base_net_amount))
            )

    external_sales: list[ExternalSaleReportReference] = []
    external_result = await session.scalars(
        select(ExternalSale)
        .where(
            ExternalSale.status == "POSTED",
            ExternalSale.include_in_pl.is_(True),
            ExternalSale.document_date >= date_from,
            ExternalSale.document_date <= date_to,
        )
        .order_by(ExternalSale.document_date, ExternalSale.id)
    )
    for sale in external_result.all():
        if sale.document_date is None or sale.original_net_total is None:
            continue
        amount = quantize_to_minor_unit(Decimal(str(sale.original_net_total)))
        key = _bucket_key(sale.document_date, granularity)
        if key in buckets:
            buckets[key]["revenue_net"] += amount
            external_sales.append(
                ExternalSaleReportReference(
                    external_sale_id=str(sale.id),
                    source_name=sale.posted_source_name or "External source",
                    original_number=sale.original_number or "",
                    event_date=sale.document_date,
                )
            )

    # -------------------------------------------------------------------
    # 2. Expenses: confirmed expenses with depreciation/business% (D3, D-DEP, D-PCT)
    # -------------------------------------------------------------------
    # We need ALL confirmed expenses whose depreciation windows overlap with
    # [date_from, date_to].  An expense started on year(expense_date) and
    # covering years [start_year .. start_year + depreciation_years - 1].
    # Its anniversary dates can fall in any of those years.
    # We fetch all confirmed expenses and filter anniversary dates in Python
    # (avoids complex SQL year arithmetic; expense counts are small).
    stmt_exp = select(Expense).where(
        and_(
            Expense.company_id == company_id,
            Expense.is_draft.is_(False),
            Expense.include_in_pl.is_(True),
        )
    )
    result_exp = await session.execute(stmt_exp)
    expenses = result_exp.scalars().all()

    for exp in expenses:
        slice_amount = _annual_slice(exp)
        start_year = exp.expense_date.year
        dep_years = int(exp.depreciation_years)

        for yr_offset in range(dep_years):
            year = start_year + yr_offset
            ann_date = _anniversary_date(exp.expense_date, year)
            # Only attribute this slice if the anniversary falls in [from, to]
            if ann_date < date_from or ann_date > date_to:
                continue
            key = _bucket_key(ann_date, granularity)
            if key in buckets:
                buckets[key]["expense_actual"] = (
                    buckets[key]["expense_actual"] + slice_amount
                )

    # -------------------------------------------------------------------
    # 3. Build series (sorted by period) and compute top-level totals
    # -------------------------------------------------------------------
    series: list[ProfitLossSeriesItem] = []
    total_revenue = _ZERO
    total_expense = _ZERO

    for key in sorted(buckets.keys()):
        rev = buckets[key]["revenue_net"]
        exp_actual = buckets[key]["expense_actual"]
        profit = rev - exp_actual
        series.append(
            ProfitLossSeriesItem(
                period=_bucket_label(key, granularity),
                revenue_net=rev,
                expense_actual=exp_actual,
                profit=profit,
            )
        )
        total_revenue = total_revenue + rev
        total_expense = total_expense + exp_actual

    total_profit = total_revenue - total_expense

    return ProfitLossReport(
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        revenue_net=total_revenue,
        expense_actual=total_expense,
        profit=total_profit,
        series=series,
        by_category=None,
        external_sales=external_sales,
    )
