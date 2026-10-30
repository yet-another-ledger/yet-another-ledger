"""Expense report service (M10 step 4).

Public API
----------
``compute_expense_report(session, company_id, date_from, date_to)``
    Aggregate confirmed expenses by category into an ``ExpenseReport``.

Design decisions (see M10.md D1–D6)
-------------------------------------
D1  Pure-read: only SELECT queries, no writes.
D2  Expenses are filtered by ``expense_date`` within [date_from, date_to] inclusive.
D3  Only confirmed expenses (``is_draft = false``) are included.
D4  All amounts from ``base_*`` columns (EUR, exchange_rate = 1 in v1).
D6  Per-row amounts are already persisted to-the-cent (M7.5).  Each row is
    quantised to minor unit before accumulation; aggregates are not re-rounded.

Grouping key
------------
- ``category_id`` non-null  → group by ``category_id`` (live FK).
- ``category_id`` is NULL (category deleted) → group by ``category_name`` snapshot.
  Same-name snapshots merge into one row (design requirement: "category deletion
  preserves name snapshot for grouping").
- Both NULL → a stable "Uncategorised" placeholder.

Deductible split
----------------
- ``deductible_net``: sum of ``base_net_amount`` for rows where ``deductible = true``.
- ``non_deductible_net``: sum of ``base_net_amount`` for rows where ``deductible = false``.
- ``deductible_net + non_deductible_net`` == ``net`` for each category row.

Amounts are original recorded expense amounts (not prorated by business_percentage or
depreciation_years) – the expense report is a raw breakdown distinct from P/L.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.models.expense import Expense
from jai.schemas.report import ExpenseCategoryRow, ExpenseReport
from jai.services.money import quantize_to_minor_unit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_UNCATEGORISED = "Uncategorised"
_UNCATEGORISED_ZH = "未分类"  # Not used in service; i18n handled in UI


# ---------------------------------------------------------------------------
# Internal accumulator
# ---------------------------------------------------------------------------


@dataclass
class _CategoryBucket:
    """Mutable accumulator for one category group."""

    category_id: uuid.UUID | None
    category_name: str
    net: Decimal = field(default_factory=lambda: Decimal("0"))
    vat: Decimal = field(default_factory=lambda: Decimal("0"))
    gross: Decimal = field(default_factory=lambda: Decimal("0"))
    deductible_net: Decimal = field(default_factory=lambda: Decimal("0"))
    non_deductible_net: Decimal = field(default_factory=lambda: Decimal("0"))

    def add_row(self, expense: Expense) -> None:
        """Accumulate one expense row into this bucket.

        Each base_* amount is already persisted to-the-cent (M7.5 guarantee).
        We quantise again here as a belt-and-suspenders measure in case any
        row still carries 3-decimal precision in the DB (NUMERIC(18,3)).
        """
        net = quantize_to_minor_unit(Decimal(str(expense.base_net_amount)))
        vat = quantize_to_minor_unit(Decimal(str(expense.base_vat_amount)))
        gross = quantize_to_minor_unit(Decimal(str(expense.base_gross_amount)))

        self.net += net
        self.vat += vat
        self.gross += gross
        if expense.deductible:
            self.deductible_net += net
        else:
            self.non_deductible_net += net

    def to_schema(self) -> ExpenseCategoryRow:
        return ExpenseCategoryRow(
            category_id=str(self.category_id) if self.category_id is not None else None,
            category_name=self.category_name,
            net=self.net,
            vat=self.vat,
            gross=self.gross,
            deductible_net=self.deductible_net,
            non_deductible_net=self.non_deductible_net,
        )


# ---------------------------------------------------------------------------
# Grouping key helper
# ---------------------------------------------------------------------------


def _group_key(expense: Expense) -> tuple[uuid.UUID | None, str]:
    """Return the grouping key for one expense.

    Rules:
    - category_id non-null (live category) → key is (category_id, "").
      The name snapshot is intentionally excluded from the key so that a
      category whose name changed mid-period still merges into one row.
    - category_id is NULL (category deleted), category_name snapshot present
      → key is (None, category_name).
    - both NULL → (None, _UNCATEGORISED)
    """
    if expense.category_id is not None:
        # Live category: group only by id, never by name snapshot.
        return (expense.category_id, "")
    # Category deleted: group by name snapshot.
    name = expense.category_name if expense.category_name else _UNCATEGORISED
    return (None, name)


# ---------------------------------------------------------------------------
# Public service function
# ---------------------------------------------------------------------------


async def compute_expense_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    date_from: date,
    date_to: date,
    include_in_pl: bool | None = None,
) -> ExpenseReport:
    """Compute an expense report aggregated by category.

    Parameters
    ----------
    session:
        Active async SQLAlchemy session.
    company_id:
        The company's UUID (red-line 2: tenant scoping).
    date_from:
        Inclusive start date (expense_date >= date_from).
    date_to:
        Inclusive end date (expense_date <= date_to).

    Returns
    -------
    ExpenseReport
        Schema instance ready for JSON serialisation.
    """
    stmt = (
        select(Expense)
        .where(
            and_(
                Expense.company_id == company_id,
                Expense.is_draft.is_(False),
                Expense.expense_date >= date_from,
                Expense.expense_date <= date_to,
            )
        )
        .order_by(Expense.expense_date, Expense.id)
    )
    if include_in_pl is not None:
        stmt = stmt.where(Expense.include_in_pl.is_(include_in_pl))
    result = await session.execute(stmt)
    expenses = result.scalars().all()

    # -- Aggregate into buckets keyed by (category_id, name_for_null_case) ----
    # For live categories (category_id non-null) the key sentinel string is ""
    # so only category_id determines the group.  For deleted categories (id
    # None) the name snapshot is the grouping key.
    # We use an ordered dict to maintain insertion order (Python 3.7+).
    buckets: dict[tuple[uuid.UUID | None, str], _CategoryBucket] = {}

    for expense in expenses:
        key = _group_key(expense)
        if key not in buckets:
            category_id_for_bucket, name_for_key = key
            # For live categories the key carries "" as the name sentinel;
            # use the expense's actual name snapshot as the initial display name.
            # Subsequent rows in the same group may have a newer snapshot (the
            # expenses are ordered by expense_date, id) and will update the name
            # below, so the bucket ends up with the latest available snapshot.
            initial_name = (
                expense.category_name or ""
                if category_id_for_bucket is not None
                else name_for_key
            )
            buckets[key] = _CategoryBucket(
                category_id=category_id_for_bucket,
                category_name=initial_name,
            )
        else:
            # For live categories, keep updating the display name to the latest
            # name snapshot seen in this group (rows arrive in date/id order).
            bucket = buckets[key]
            if bucket.category_id is not None and expense.category_name:
                bucket.category_name = expense.category_name
        buckets[key].add_row(expense)

    # -- Build per-category rows and grand totals ----------------------------
    by_category = [b.to_schema() for b in buckets.values()]

    total_net = sum((r.net for r in by_category), _ZERO)
    total_vat = sum((r.vat for r in by_category), _ZERO)
    total_gross = sum((r.gross for r in by_category), _ZERO)
    total_deductible_net = sum((r.deductible_net for r in by_category), _ZERO)
    total_non_deductible_net = sum((r.non_deductible_net for r in by_category), _ZERO)

    return ExpenseReport(
        date_from=date_from,
        date_to=date_to,
        by_category=by_category,
        total_net=total_net,
        total_vat=total_vat,
        total_gross=total_gross,
        total_deductible_net=total_deductible_net,
        total_non_deductible_net=total_non_deductible_net,
    )
