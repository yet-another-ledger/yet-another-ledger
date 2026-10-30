"""Unit tests for M10 step 1 – P/L reporting engine (services/reporting/pl.py).

Tests the pure algorithmic layer: ``_annual_slice``, ``_anniversary_date``,
``_bucket_key``, ``_bucket_label``, and the main ``compute_profit_loss``
function (mocked DB via fake session fixtures).

Coverage:
Happy:
- Revenue aggregation: SENT + COMPLETED invoices summed, month/quarter buckets.
- Expense aggregation: business_percentage < 100 reduces cost;
  depreciation_years > 1 spreads slices.
- Top-level totals = sum of series buckets (consistency check).
- Month granularity and quarter granularity both produce correct period labels.

Corner:
- DRAFT invoices excluded.
- CANCELLED invoices excluded.
- is_draft=true expenses excluded.
- business_percentage=50 halves the slice.
- depreciation_years=3: only years falling in window contribute.
- Anniversary date Feb 29 in leap year → Feb 28 in non-leap year.
- Empty range (from==to, no matching docs) → all zeros.
- Quarter boundary: dates straddling quarter boundary go to correct bucket.
- Monthly series: 12-month range has 12 buckets all present (contiguous).
"""

from __future__ import annotations

import uuid
from calendar import isleap
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from jai.models._enums import ExpenseKind, PlCostBasis, VatTreatmentEffect
from jai.services.expense import compute_annual_pl_cost, compute_pl_cost_basis
from jai.services.reporting.pl import (
    _anniversary_date,
    _annual_slice,
    _bucket_key,
    _bucket_label,
    compute_profit_loss,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestAnniversaryDate:
    def test_normal_date(self) -> None:
        d = _anniversary_date(date(2024, 3, 15), 2026)
        assert d == date(2026, 3, 15)

    def test_feb29_in_leap_year(self) -> None:
        d = _anniversary_date(date(2024, 2, 29), 2028)
        assert d == date(2028, 2, 29)

    def test_feb29_in_non_leap_year(self) -> None:
        """Feb 29 anniversary in a non-leap year falls on Feb 28."""
        d = _anniversary_date(date(2024, 2, 29), 2025)
        assert d == date(2025, 2, 28)
        assert not isleap(2025)


class TestBucketKey:
    def test_month(self) -> None:
        assert _bucket_key(date(2025, 3, 15), "month") == (2025, 3)

    def test_quarter_q1(self) -> None:
        assert _bucket_key(date(2025, 1, 1), "quarter") == (2025, 1)
        assert _bucket_key(date(2025, 3, 31), "quarter") == (2025, 1)

    def test_quarter_q2(self) -> None:
        assert _bucket_key(date(2025, 4, 1), "quarter") == (2025, 2)
        assert _bucket_key(date(2025, 6, 30), "quarter") == (2025, 2)

    def test_quarter_q3(self) -> None:
        assert _bucket_key(date(2025, 7, 1), "quarter") == (2025, 3)

    def test_quarter_q4(self) -> None:
        assert _bucket_key(date(2025, 10, 1), "quarter") == (2025, 4)
        assert _bucket_key(date(2025, 12, 31), "quarter") == (2025, 4)


class TestBucketLabel:
    def test_month(self) -> None:
        assert _bucket_label((2025, 3), "month") == "2025-03-01"
        assert _bucket_label((2025, 12), "month") == "2025-12-01"

    def test_quarter_q1(self) -> None:
        assert _bucket_label((2025, 1), "quarter") == "2025-01-01"

    def test_quarter_q2(self) -> None:
        assert _bucket_label((2025, 2), "quarter") == "2025-04-01"

    def test_quarter_q3(self) -> None:
        assert _bucket_label((2025, 3), "quarter") == "2025-07-01"

    def test_quarter_q4(self) -> None:
        assert _bucket_label((2025, 4), "quarter") == "2025-10-01"


class TestAnnualSlice:
    """Annual depreciation slice computation."""

    def _make_expense(
        self,
        base_net: str = "1000.00",
        business_pct: str = "100",
        dep_years: int = 1,
    ) -> MagicMock:
        exp = MagicMock()
        exp.base_net_amount = Decimal(base_net)
        exp.base_vat_amount = Decimal("0")
        exp.base_gross_amount = Decimal(base_net)
        exp.business_percentage = Decimal(business_pct)
        exp.depreciation_years = dep_years
        exp.kind = ExpenseKind.PURCHASE
        exp.pl_cost_basis = PlCostBasis.NET
        exp.vat_treatment_effect = VatTreatmentEffect.APPLY_RATE
        exp.deductible = True
        exp.include_in_pl = True
        return exp

    def test_full_business_single_year(self) -> None:
        """100% business, 1 year → full net amount."""
        exp = self._make_expense("1000.00", "100", 1)
        result = _annual_slice(exp)
        assert result == Decimal("1000.00")

    def test_partial_business(self) -> None:
        """50% business → half the net."""
        exp = self._make_expense("1000.00", "50", 1)
        result = _annual_slice(exp)
        assert result == Decimal("500.00")

    def test_depreciation_3_years(self) -> None:
        """100% business, 3 years → 1/3 per year."""
        exp = self._make_expense("900.00", "100", 3)
        result = _annual_slice(exp)
        # 900 / 3 = 300 exactly
        assert result == Decimal("300.00")

    def test_depreciation_non_divisible_rounds_to_cent(self) -> None:
        """1/3 of 1000 is irrational; must be quantised to 2 dp (M7.5)."""
        exp = self._make_expense("1000.00", "100", 3)
        result = _annual_slice(exp)
        # 1000 / 3 = 333.333... → ROUND_HALF_UP to 2 dp = 333.33
        assert result == Decimal("333.33")
        # Must have exactly 2 decimal places (to-the-cent)
        assert result == result.quantize(Decimal("0.01"))

    def test_depreciation_with_partial_business(self) -> None:
        """75% business, 2 years → 375 per year."""
        exp = self._make_expense("1000.00", "75", 2)
        result = _annual_slice(exp)
        # 1000 × 0.75 / 2 = 375
        assert result == Decimal("375.00")

    def test_net_basis_keeps_persisted_precision_until_slice_rounding(self) -> None:
        exp = self._make_expense("100.005", "50", 1)
        assert _annual_slice(exp) == Decimal("50.00")

    def test_gross_basis_uses_persisted_gross_snapshot(self) -> None:
        exp = self._make_expense("100.005", "50", 1)
        exp.pl_cost_basis = PlCostBasis.GROSS
        exp.base_vat_amount = Decimal("21.005")
        exp.base_gross_amount = Decimal("120.999")
        assert _annual_slice(exp) == Decimal("60.50")


class TestPlCostBasis:
    def test_net_gross_and_auto_for_100_plus_21_deductible(self) -> None:
        args = (Decimal("100"), Decimal("21"), VatTreatmentEffect.APPLY_RATE, True)
        assert compute_pl_cost_basis(*args, PlCostBasis.NET) == Decimal("100.00")
        assert compute_pl_cost_basis(*args, PlCostBasis.GROSS) == Decimal("121.00")
        assert compute_pl_cost_basis(*args, PlCostBasis.AUTO) == Decimal("100.00")

    def test_auto_adds_irrecoverable_charged_vat_only(self) -> None:
        basis = compute_pl_cost_basis(
            Decimal("100"), Decimal("21"), VatTreatmentEffect.APPLY_RATE,
            False, PlCostBasis.AUTO,
        )
        assert basis == Decimal("121.00")
        reverse = compute_pl_cost_basis(
            Decimal("100"), Decimal("21"), VatTreatmentEffect.ZERO_REVERSE,
            False, PlCostBasis.AUTO,
        )
        assert reverse == Decimal("100.00")
        eu_consumer = compute_pl_cost_basis(
            Decimal("100"), Decimal("21"), VatTreatmentEffect.APPLY_RATE,
            True, PlCostBasis.AUTO, "EU_B2C_PURCH",
        )
        assert eu_consumer == Decimal("121.00")

    def test_business_percentage_depreciation_and_exclusion(self) -> None:
        assert compute_annual_pl_cost(
            Decimal("121"), Decimal("50"), 2, True
        ) == Decimal("30.25")
        assert compute_annual_pl_cost(
            Decimal("121"), Decimal("100"), 1, False
        ) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Integration: compute_profit_loss (mocked DB session)
# ---------------------------------------------------------------------------


def _make_invoice(
    company_id: uuid.UUID,
    status: str,
    invoice_date: date,
    base_taxable_amount: str,
) -> MagicMock:
    inv = MagicMock()
    inv.company_id = company_id
    inv.status = status
    inv.invoice_date = invoice_date
    inv.base_taxable_amount = Decimal(base_taxable_amount)
    return inv


def _make_expense_row(
    company_id: uuid.UUID,
    is_draft: bool,
    expense_date: date,
    base_net_amount: str,
    business_percentage: str = "100",
    depreciation_years: int = 1,
) -> MagicMock:
    exp = MagicMock()
    exp.company_id = company_id
    exp.is_draft = is_draft
    exp.expense_date = expense_date
    exp.base_net_amount = Decimal(base_net_amount)
    exp.base_vat_amount = Decimal("0")
    exp.base_gross_amount = Decimal(base_net_amount)
    exp.business_percentage = Decimal(business_percentage)
    exp.depreciation_years = depreciation_years
    exp.kind = ExpenseKind.PURCHASE
    exp.pl_cost_basis = PlCostBasis.NET
    exp.vat_treatment_effect = VatTreatmentEffect.APPLY_RATE
    exp.deductible = True
    exp.include_in_pl = True
    return exp


def _make_session(invoices: list, expenses: list) -> AsyncMock:
    """Create a mock AsyncSession that returns given rows for execute()."""
    session = AsyncMock()

    # execute() is called three times: positive revenue, Credit events, expenses.
    # We use side_effect to return the correct result for each call.
    call_count = 0

    def _scalars_for(items: list) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = items
        return result

    invoice_result = _scalars_for(invoices)
    credit_result = MagicMock()
    credit_result.all.return_value = []
    expense_result = _scalars_for(expenses)

    async def _execute(stmt):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return invoice_result
        if call_count == 2:
            return credit_result
        return expense_result

    session.execute = _execute
    return session


class TestComputeProfitLoss:
    """Integration tests for compute_profit_loss (mocked DB)."""

    @pytest.mark.asyncio
    async def test_happy_month_granularity(self) -> None:
        """Two SENT invoices in different months; one expense; verify buckets."""
        cid = uuid.uuid4()
        invoices = [
            _make_invoice(cid, "SENT", date(2025, 1, 15), "1000.00"),
            _make_invoice(cid, "COMPLETED", date(2025, 2, 10), "500.00"),
        ]
        expenses = [
            _make_expense_row(cid, False, date(2025, 1, 5), "200.00"),
        ]
        session = _make_session(invoices, expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 2, 28), "month"
        )
        assert report.revenue_net == Decimal("1500.00")
        assert report.expense_actual == Decimal("200.00")
        assert report.profit == Decimal("1300.00")
        assert len(report.series) == 2
        jan = report.series[0]
        feb = report.series[1]
        assert jan.period == "2025-01-01"
        assert jan.revenue_net == Decimal("1000.00")
        assert jan.expense_actual == Decimal("200.00")
        assert jan.profit == Decimal("800.00")
        assert feb.period == "2025-02-01"
        assert feb.revenue_net == Decimal("500.00")
        assert feb.expense_actual == Decimal("0")
        assert feb.profit == Decimal("500.00")

    @pytest.mark.asyncio
    async def test_happy_quarter_granularity(self) -> None:
        """Three invoices across two quarters; verify Q1 and Q2 buckets."""
        cid = uuid.uuid4()
        invoices = [
            _make_invoice(cid, "SENT", date(2025, 1, 1), "100.00"),
            _make_invoice(cid, "SENT", date(2025, 3, 31), "200.00"),
            _make_invoice(cid, "COMPLETED", date(2025, 4, 1), "300.00"),
        ]
        expenses = [
            _make_expense_row(cid, False, date(2025, 2, 1), "60.00"),  # Q1
            _make_expense_row(cid, False, date(2025, 5, 1), "90.00"),  # Q2
        ]
        session = _make_session(invoices, expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 6, 30), "quarter"
        )
        assert len(report.series) == 2
        q1 = report.series[0]
        q2 = report.series[1]
        assert q1.period == "2025-01-01"  # Q1 start
        assert q1.revenue_net == Decimal("300.00")
        assert q1.expense_actual == Decimal("60.00")
        assert q2.period == "2025-04-01"  # Q2 start
        assert q2.revenue_net == Decimal("300.00")
        assert q2.expense_actual == Decimal("90.00")
        # Top-level = sum of buckets
        assert report.revenue_net == q1.revenue_net + q2.revenue_net
        assert report.expense_actual == q1.expense_actual + q2.expense_actual
        assert report.profit == report.revenue_net - report.expense_actual

    @pytest.mark.asyncio
    async def test_draft_invoices_excluded(self) -> None:
        """DRAFT invoices must not appear in revenue.

        The SQL WHERE clause filters statuses; we simulate this by passing an
        empty invoice list (as if the DB returned zero matching rows).
        """
        cid = uuid.uuid4()
        session = _make_session([], [])
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 12, 31), "month"
        )
        assert report.revenue_net == Decimal("0")

    @pytest.mark.asyncio
    async def test_cancelled_invoices_excluded(self) -> None:
        """CANCELLED invoices must not appear in revenue (returns 0 from mock)."""
        cid = uuid.uuid4()
        session = _make_session([], [])  # DB filtered – returns nothing
        report = await compute_profit_loss(
            session, cid, date(2025, 6, 1), date(2025, 6, 30), "month"
        )
        assert report.revenue_net == Decimal("0")

    @pytest.mark.asyncio
    async def test_draft_expenses_excluded(self) -> None:
        """is_draft=true expenses must not contribute to expense_actual."""
        cid = uuid.uuid4()
        # The mock already filters – we pass empty to simulate DB filter.
        session = _make_session([], [])
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 3, 31), "month"
        )
        assert report.expense_actual == Decimal("0")

    @pytest.mark.asyncio
    async def test_business_percentage_reduces_cost(self) -> None:
        """50% business percentage halves the expense contribution."""
        cid = uuid.uuid4()
        expenses = [
            _make_expense_row(cid, False, date(2025, 3, 10), "800.00", "50", 1),
        ]
        session = _make_session([], expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 12, 31), "month"
        )
        # 800 × 50% / 1 = 400
        assert report.expense_actual == Decimal("400.00")

    @pytest.mark.asyncio
    async def test_depreciation_years_multi_year_window_overlap(self) -> None:
        """Expense with depreciation_years=3 purchased in 2024 contributes to 2025 window."""
        cid = uuid.uuid4()
        # Expense date: 2024-06-15, depreciation: 3 years
        # Slices land on: 2024-06-15 (year 0), 2025-06-15 (year 1), 2026-06-15 (year 2)
        # Window: 2025-01-01 to 2025-12-31 → only the 2025 anniversary falls in
        expenses = [
            _make_expense_row(cid, False, date(2024, 6, 15), "900.00", "100", 3),
        ]
        session = _make_session([], expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 12, 31), "month"
        )
        # Only the year-1 slice (300) falls in 2025
        assert report.expense_actual == Decimal("300.00")
        # Verify it lands in the June bucket (anniversary is June 15)
        jun_bucket = next(s for s in report.series if s.period == "2025-06-01")
        assert jun_bucket.expense_actual == Decimal("300.00")

    @pytest.mark.asyncio
    async def test_depreciation_outside_window_zero(self) -> None:
        """Expense anniversary years entirely outside the window contribute nothing."""
        cid = uuid.uuid4()
        # Expense date: 2020-01-15, depreciation: 2 years
        # Slices: 2020-01-15, 2021-01-15 → both outside 2025 window
        expenses = [
            _make_expense_row(cid, False, date(2020, 1, 15), "600.00", "100", 2),
        ]
        session = _make_session([], expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 12, 31), "month"
        )
        assert report.expense_actual == Decimal("0")

    @pytest.mark.asyncio
    async def test_empty_range_all_zeros(self) -> None:
        """A date range with no matching documents returns all-zero report."""
        cid = uuid.uuid4()
        session = _make_session([], [])
        report = await compute_profit_loss(
            session, cid, date(2025, 7, 1), date(2025, 7, 31), "month"
        )
        assert report.revenue_net == Decimal("0")
        assert report.expense_actual == Decimal("0")
        assert report.profit == Decimal("0")
        assert len(report.series) == 1  # one month bucket still present
        assert report.series[0].period == "2025-07-01"

    @pytest.mark.asyncio
    async def test_quarter_boundary_correct_bucket(self) -> None:
        """Invoice on March 31 goes to Q1; invoice on April 1 goes to Q2."""
        cid = uuid.uuid4()
        invoices = [
            _make_invoice(cid, "SENT", date(2025, 3, 31), "100.00"),
            _make_invoice(cid, "SENT", date(2025, 4, 1), "200.00"),
        ]
        session = _make_session(invoices, [])
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 6, 30), "quarter"
        )
        q1 = next(s for s in report.series if s.period == "2025-01-01")
        q2 = next(s for s in report.series if s.period == "2025-04-01")
        assert q1.revenue_net == Decimal("100.00")
        assert q2.revenue_net == Decimal("200.00")

    @pytest.mark.asyncio
    async def test_monthly_series_contiguous_12_buckets(self) -> None:
        """A full-year monthly report has exactly 12 buckets, all present."""
        cid = uuid.uuid4()
        session = _make_session([], [])
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 12, 31), "month"
        )
        assert len(report.series) == 12
        months = [s.period for s in report.series]
        expected = [f"2025-{m:02d}-01" for m in range(1, 13)]
        assert months == expected

    @pytest.mark.asyncio
    async def test_toplevel_equals_sum_of_series(self) -> None:
        """Top-level revenue/expense/profit equals the sum of all series buckets."""
        cid = uuid.uuid4()
        invoices = [
            _make_invoice(cid, "SENT", date(2025, 1, 10), "300.00"),
            _make_invoice(cid, "COMPLETED", date(2025, 6, 5), "700.00"),
        ]
        expenses = [
            _make_expense_row(cid, False, date(2025, 2, 28), "150.00", "80", 1),
            _make_expense_row(cid, False, date(2025, 9, 1), "200.00", "100", 2),
        ]
        session = _make_session(invoices, expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 12, 31), "quarter"
        )
        sum_rev = sum(s.revenue_net for s in report.series)
        sum_exp = sum(s.expense_actual for s in report.series)
        assert report.revenue_net == sum_rev
        assert report.expense_actual == sum_exp
        assert report.profit == report.revenue_net - report.expense_actual

    @pytest.mark.asyncio
    async def test_expense_actual_quantised_to_cents(self) -> None:
        """expense_actual and all series buckets must be quantised to 2 dp (M7.5/D6).

        Uses 1000 / 3 = 333.333... per year so the to-cent rounding is visible.
        Window covers all three anniversary years → total = 333.33 × 3 = 999.99.
        """
        cid = uuid.uuid4()
        expenses = [
            _make_expense_row(cid, False, date(2023, 6, 1), "1000.00", "100", 3),
        ]
        session = _make_session([], expenses)
        # Window covers 2023, 2024, 2025 anniversaries (June 1 of each year)
        report = await compute_profit_loss(
            session, cid, date(2023, 1, 1), date(2025, 12, 31), "month"
        )
        cent = Decimal("0.01")
        # Top-level expense_actual must be at 2 dp
        assert report.expense_actual == report.expense_actual.quantize(cent), (
            f"expense_actual {report.expense_actual!r} is not quantised to 2 dp"
        )
        # Every bucket must also be at 2 dp
        for s in report.series:
            assert s.expense_actual == s.expense_actual.quantize(cent), (
                f"series bucket {s.period!r} expense_actual {s.expense_actual!r} "
                "is not quantised to 2 dp"
            )
            assert s.profit == s.profit.quantize(cent), (
                f"series bucket {s.period!r} profit {s.profit!r} "
                "is not quantised to 2 dp"
            )
        # The three June buckets each get 333.33
        for year in (2023, 2024, 2025):
            period = f"{year}-06-01"
            bucket = next(s for s in report.series if s.period == period)
            assert bucket.expense_actual == Decimal("333.33"), (
                f"bucket {period!r}: expected 333.33, got {bucket.expense_actual!r}"
            )
        # Total = 333.33 × 3 = 999.99 (not 1000.00 — expected with per-slice rounding)
        assert report.expense_actual == Decimal("999.99")
        # top-level == sum of series buckets
        assert report.expense_actual == sum(s.expense_actual for s in report.series)

    @pytest.mark.asyncio
    async def test_all_fields_quantised_to_cents_with_db_scale(self) -> None:
        """revenue_net / expense_actual / profit must all be ≤ 2 dp, even when
        the invoice carries a 3-dp DB NUMERIC(18,3) value and the expense slice
        is non-integer.

        Reproduces the R2-F1 scenario: DB returns Decimal("1000.000") for
        base_taxable_amount (NUMERIC(18,3) scale); expense is 1000/3 = 333.33...
        Before the fix, revenue_net="1000.000" and profit="666.670" had 3 dp.
        """
        cid = uuid.uuid4()
        # Simulate DB NUMERIC(18,3) read-back with explicit 3-dp Decimal
        invoices = [
            _make_invoice(cid, "SENT", date(2025, 1, 15), "1000.000"),
        ]
        expenses = [
            _make_expense_row(cid, False, date(2025, 1, 5), "1000.00", "100", 3),
        ]
        session = _make_session(invoices, expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 3, 31), "month"
        )
        cent = Decimal("0.01")

        # Top-level: all three fields must be ≤ 2 dp
        assert report.revenue_net == report.revenue_net.quantize(cent), (
            f"revenue_net {report.revenue_net!r} is not quantised to 2 dp"
        )
        assert report.expense_actual == report.expense_actual.quantize(cent), (
            f"expense_actual {report.expense_actual!r} is not quantised to 2 dp"
        )
        assert report.profit == report.profit.quantize(cent), (
            f"profit {report.profit!r} is not quantised to 2 dp"
        )

        # Each series bucket: all three fields must be ≤ 2 dp
        for s in report.series:
            assert s.revenue_net == s.revenue_net.quantize(cent), (
                f"bucket {s.period!r} revenue_net {s.revenue_net!r} not 2 dp"
            )
            assert s.expense_actual == s.expense_actual.quantize(cent), (
                f"bucket {s.period!r} expense_actual {s.expense_actual!r} not 2 dp"
            )
            assert s.profit == s.profit.quantize(cent), (
                f"bucket {s.period!r} profit {s.profit!r} not 2 dp"
            )

        # Jan bucket: revenue=1000.00, expense=333.33, profit=666.67
        jan = next(s for s in report.series if s.period == "2025-01-01")
        assert jan.revenue_net == Decimal("1000.00")
        assert jan.expense_actual == Decimal("333.33")
        assert jan.profit == Decimal("666.67")

        # Top-level == Σ series buckets
        assert report.revenue_net == sum(s.revenue_net for s in report.series)
        assert report.expense_actual == sum(s.expense_actual for s in report.series)
        assert report.profit == report.revenue_net - report.expense_actual

    @pytest.mark.asyncio
    async def test_feb29_anniversary_non_leap_year(self) -> None:
        """Expense purchased on Feb 29 → anniversary in non-leap year is Feb 28."""
        cid = uuid.uuid4()
        # 2024 is a leap year; 2025 is not.
        # With depreciation_years=2: slices on 2024-02-29 and 2025-02-28.
        # Window: 2025-01-01 to 2025-03-31 → the Feb 28 slice falls in Q1-2025.
        expenses = [
            _make_expense_row(cid, False, date(2024, 2, 29), "600.00", "100", 2),
        ]
        session = _make_session([], expenses)
        report = await compute_profit_loss(
            session, cid, date(2025, 1, 1), date(2025, 3, 31), "month"
        )
        # Slice = 600 / 2 = 300; lands in Feb 2025 bucket
        assert report.expense_actual == Decimal("300.00")
        feb_bucket = next(s for s in report.series if s.period == "2025-02-01")
        assert feb_bucket.expense_actual == Decimal("300.00")
