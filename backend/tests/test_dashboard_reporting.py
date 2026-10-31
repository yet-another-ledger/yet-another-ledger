"""Unit tests for M10 step 5 – Dashboard summary service.

Tests ``compute_dashboard`` in ``services/reporting/dashboard.py``.

Strategy
--------
``compute_dashboard`` delegates to ``compute_profit_loss``, ``compute_vat_return``,
and ``compute_expense_report``.  We mock all three to avoid duplicating the full
DB-layer tests for those services.  The tests focus on:

  1. KPI binding: ytd_* come from the P/L top-level totals.
  2. ytd_* == Σ monthly (12-month sum invariant).
  3. Monthly series: always 12 items; gaps filled with zero; correct values.
  4. VAT payable: current_quarter_vat_payable == mock net_payable value.
  5. Q selection: current year → today's quarter; prior year → Q4.
  6. Top expense categories: sorted by net descending, capped at 5.
  7. Consistency: top categories match compute_expense_report.by_category.
  8. Empty year: all zeros, empty top categories.
  9. Amount precision: Decimal, no secondary rounding.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jai.schemas.report import (
    DashboardSummary,
    ExpenseCategoryRow,
    ExpenseReport,
    ProfitLossReport,
    ProfitLossSeriesItem,
    VatBoxBaseOnly,
    VatBoxBaseVat,
    VatBoxPrivateUse,
    VatBoxVatOnly,
    VatReturnBoxes,
    VatReturnReport,
    VatReturnTotals,
)
from jai.schemas.setting import VatRateTiers
from jai.services.reporting.dashboard import _select_quarter, compute_dashboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_DEFAULT_TIERS = VatRateTiers()  # hoog=21, laag=9, zero=0


def _make_company(country_code: str = "NL") -> MagicMock:
    co = MagicMock()
    co.id = uuid.uuid4()
    co.country_code = country_code
    return co


def _make_pl_report(
    *,
    revenue_net: str = "0.00",
    expense_actual: str = "0.00",
    profit: str = "0.00",
    series: list[ProfitLossSeriesItem] | None = None,
    year: int = 2026,
) -> ProfitLossReport:
    """Build a minimal ProfitLossReport."""
    if series is None:
        series = []
    return ProfitLossReport(
        date_from=date(year, 1, 1),
        date_to=date(year, 12, 31),
        granularity="month",
        revenue_net=Decimal(revenue_net),
        expense_actual=Decimal(expense_actual),
        mileage_expense_actual=Decimal("0.00"),
        other_expense_actual=Decimal(expense_actual),
        profit_before_mileage=Decimal(profit),
        profit_after_mileage=Decimal(profit),
        profit=Decimal(profit),
        series=series,
        by_category=None,
    )


def _make_series_item(
    year: int, month: int, rev: str, exp: str, profit: str
) -> ProfitLossSeriesItem:
    return ProfitLossSeriesItem(
        period=f"{year:04d}-{month:02d}-01",
        revenue_net=Decimal(rev),
        expense_actual=Decimal(exp),
        mileage_expense_actual=Decimal("0.00"),
        other_expense_actual=Decimal(exp),
        profit_before_mileage=Decimal(profit),
        profit_after_mileage=Decimal(profit),
        profit=Decimal(profit),
    )


def _make_vat_report(
    net_payable: str = "0.00", year: int = 2026, quarter: int = 1
) -> VatReturnReport:
    zero_base_vat = VatBoxBaseVat(base=_ZERO, vat=_ZERO)
    zero_base = VatBoxBaseOnly(base=_ZERO)
    zero_vat = VatBoxVatOnly(vat=_ZERO)
    zero_private_use = VatBoxPrivateUse(
        base=_ZERO,
        vat=_ZERO,
        known_battery_base=_ZERO,
        known_battery_vat=_ZERO,
        missing_inputs=[],
        is_complete=True,
    )
    boxes = VatReturnBoxes(
        box_1a=zero_base_vat,
        box_1b=zero_base_vat,
        box_1c=zero_base_vat,
        box_1d=zero_private_use,
        box_1e=zero_base,
        box_2a=zero_base_vat,
        box_3a=zero_base,
        box_3b=zero_base,
        box_3c=zero_base,
        box_4a=zero_base_vat,
        box_4b=zero_base_vat,
        box_5b=zero_vat,
    )
    totals = VatReturnTotals(
        output_vat_total=VatBoxVatOnly(vat=_ZERO),
        net_payable_or_refundable=VatBoxVatOnly(vat=Decimal(net_payable)),
    )
    return VatReturnReport(
        year=year,
        quarter=quarter,
        date_from=date(year, (quarter - 1) * 3 + 1, 1),
        date_to=date(year, quarter * 3, 28),
        is_last_period_of_year=(quarter == 4),
        boxes=boxes,
        totals=totals,
        warnings=[],
        disclaimer="test disclaimer",
    )


def _make_expense_row(
    *,
    category_id: str | None = None,
    category_name: str = "Category",
    net: str = "0.00",
) -> ExpenseCategoryRow:
    n = Decimal(net)
    return ExpenseCategoryRow(
        category_id=category_id,
        category_name=category_name,
        net=n,
        vat=_ZERO,
        gross=n,
        deductible_net=n,
        non_deductible_net=_ZERO,
    )


def _make_expense_report(rows: list[ExpenseCategoryRow], year: int = 2026) -> ExpenseReport:
    total_net = sum((r.net for r in rows), _ZERO)
    return ExpenseReport(
        date_from=date(year, 1, 1),
        date_to=date(year, 12, 31),
        by_category=rows,
        total_net=total_net,
        total_vat=_ZERO,
        total_gross=total_net,
        total_deductible_net=total_net,
        total_non_deductible_net=_ZERO,
    )


def _mock_session() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# Tests: _select_quarter
# ---------------------------------------------------------------------------


class TestSelectQuarter:
    """Q-selection logic (module-level function, no DB needed)."""

    def test_prior_year_returns_q4(self) -> None:
        # Any year < current year → Q4
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 15)
            q = _select_quarter(2025)
        assert q == 4

    def test_future_year_treated_as_non_current_returns_q4(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 15)
            q = _select_quarter(2027)
        assert q == 4

    def test_current_year_q1(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 1, 15)
            q = _select_quarter(2026)
        assert q == 1

    def test_current_year_q2(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 5, 1)
            q = _select_quarter(2026)
        assert q == 2

    def test_current_year_q3(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 8, 31)
            q = _select_quarter(2026)
        assert q == 3

    def test_current_year_q4(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 12, 1)
            q = _select_quarter(2026)
        assert q == 4

    def test_q_boundary_april_first_is_q2(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 4, 1)
            q = _select_quarter(2026)
        assert q == 2

    def test_q_boundary_march_31_is_q1(self) -> None:
        with patch("jai.services.reporting.dashboard.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 3, 31)
            q = _select_quarter(2026)
        assert q == 1


# ---------------------------------------------------------------------------
# Tests: compute_dashboard – empty year
# ---------------------------------------------------------------------------


class TestComputeDashboardEmpty:
    """Empty year: no invoices, no expenses → all zeros."""

    @pytest.mark.asyncio
    async def test_empty_year_kpi_all_zero(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_empty = _make_pl_report(year=year)
        vat_empty = _make_vat_report(net_payable="0.00", year=year, quarter=2)
        expense_empty = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_empty),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_empty),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_empty),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert isinstance(result, DashboardSummary)
        assert result.kpi.ytd_revenue == _ZERO
        assert result.kpi.ytd_expense == _ZERO
        assert result.kpi.ytd_profit == _ZERO
        assert result.kpi.current_quarter_vat_payable == _ZERO

    @pytest.mark.asyncio
    async def test_empty_year_monthly_has_12_items(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_empty = _make_pl_report(year=year)  # series is empty list
        vat_empty = _make_vat_report(year=year, quarter=2)
        expense_empty = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_empty),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_empty),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_empty),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert len(result.monthly) == 12
        for i, m in enumerate(result.monthly, start=1):
            assert m.month == f"{year:04d}-{i:02d}-01"
            assert m.revenue == _ZERO
            assert m.expense == _ZERO
            assert m.profit == _ZERO

    @pytest.mark.asyncio
    async def test_empty_year_top_categories_empty(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_empty = _make_pl_report(year=year)
        vat_empty = _make_vat_report(year=year, quarter=2)
        expense_empty = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_empty),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_empty),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_empty),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert result.top_expense_categories == []


# ---------------------------------------------------------------------------
# Tests: KPI binding – ytd_* from P/L top-level totals
# ---------------------------------------------------------------------------


class TestKpiBinding:
    """KPI ytd_* must mirror compute_profit_loss top-level totals."""

    @pytest.mark.asyncio
    async def test_ytd_values_come_from_pl_totals(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        # 3-month series so top-level totals are non-trivial sums
        series = [
            _make_series_item(year, 1, "1000.00", "300.00", "700.00"),
            _make_series_item(year, 2, "2000.00", "500.00", "1500.00"),
            _make_series_item(year, 3, "1500.00", "400.00", "1100.00"),
        ]
        pl_report = _make_pl_report(
            revenue_net="4500.00",
            expense_actual="1200.00",
            profit="3300.00",
            series=series,
            year=year,
        )
        vat_report = _make_vat_report(net_payable="630.00", year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert result.kpi.ytd_revenue == Decimal("4500.00")
        assert result.kpi.ytd_expense == Decimal("1200.00")
        assert result.kpi.ytd_profit == Decimal("3300.00")

    @pytest.mark.asyncio
    async def test_vat_payable_from_btw_report(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(net_payable="987.65", year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert result.kpi.current_quarter_vat_payable == Decimal("987.65")


# ---------------------------------------------------------------------------
# Tests: ytd_* == Σ monthly (consistency invariant)
# ---------------------------------------------------------------------------


class TestYtdSumInvariant:
    """ytd_revenue == sum(m.revenue for m in monthly), etc."""

    @pytest.mark.asyncio
    async def test_ytd_equals_sum_of_monthly(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        # 12 months of data
        series = []
        for month in range(1, 13):
            rev = Decimal(f"{month * 100}.00")
            exp = Decimal(f"{month * 30}.00")
            profit = rev - exp
            series.append(
                _make_series_item(year, month, str(rev), str(exp), str(profit))
            )

        total_rev = sum(Decimal(f"{m * 100}.00") for m in range(1, 13))
        total_exp = sum(Decimal(f"{m * 30}.00") for m in range(1, 13))
        total_profit = total_rev - total_exp

        pl_report = _make_pl_report(
            revenue_net=str(total_rev),
            expense_actual=str(total_exp),
            profit=str(total_profit),
            series=series,
            year=year,
        )
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        sum_rev = sum(m.revenue for m in result.monthly)
        sum_exp = sum(m.expense for m in result.monthly)
        sum_profit = sum(m.profit for m in result.monthly)

        assert sum_rev == result.kpi.ytd_revenue, (
            f"Σ monthly.revenue ({sum_rev}) != kpi.ytd_revenue ({result.kpi.ytd_revenue})"
        )
        assert sum_exp == result.kpi.ytd_expense, (
            f"Σ monthly.expense ({sum_exp}) != kpi.ytd_expense ({result.kpi.ytd_expense})"
        )
        assert sum_profit == result.kpi.ytd_profit, (
            f"Σ monthly.profit ({sum_profit}) != kpi.ytd_profit ({result.kpi.ytd_profit})"
        )


# ---------------------------------------------------------------------------
# Tests: Monthly series structure and gap-filling
# ---------------------------------------------------------------------------


class TestMonthlySeriesStructure:
    """Monthly list always has exactly 12 items; gaps become zero."""

    @pytest.mark.asyncio
    async def test_always_12_months(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        # Only 3 months have data
        series = [
            _make_series_item(year, 1, "500.00", "100.00", "400.00"),
            _make_series_item(year, 6, "800.00", "200.00", "600.00"),
            _make_series_item(year, 12, "300.00", "50.00", "250.00"),
        ]
        pl_report = _make_pl_report(
            revenue_net="1600.00",
            expense_actual="350.00",
            profit="1250.00",
            series=series,
            year=year,
        )
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert len(result.monthly) == 12
        months_present = {m.month for m in result.monthly}
        for month_num in range(1, 13):
            assert f"{year:04d}-{month_num:02d}-01" in months_present

    @pytest.mark.asyncio
    async def test_gap_months_are_zero(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        # Only January has data
        series = [_make_series_item(year, 1, "500.00", "100.00", "400.00")]
        pl_report = _make_pl_report(
            revenue_net="500.00",
            expense_actual="100.00",
            profit="400.00",
            series=series,
            year=year,
        )
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        # Jan is correct
        jan = result.monthly[0]
        assert jan.month == f"{year:04d}-01-01"
        assert jan.revenue == Decimal("500.00")
        assert jan.expense == Decimal("100.00")
        assert jan.profit == Decimal("400.00")

        # All other months are zero
        for m in result.monthly[1:]:
            assert m.revenue == _ZERO, f"{m.month} revenue should be 0"
            assert m.expense == _ZERO, f"{m.month} expense should be 0"
            assert m.profit == _ZERO, f"{m.month} profit should be 0"

    @pytest.mark.asyncio
    async def test_monthly_values_match_pl_series(self) -> None:
        """Each monthly item's values must match the corresponding P/L series bucket."""
        year = 2026
        company = _make_company()
        session = _mock_session()

        series = [
            _make_series_item(year, 3, "1200.00", "400.00", "800.00"),
            _make_series_item(year, 9, "2500.00", "700.00", "1800.00"),
        ]
        pl_report = _make_pl_report(
            revenue_net="3700.00",
            expense_actual="1100.00",
            profit="2600.00",
            series=series,
            year=year,
        )
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        monthly_by_label = {m.month: m for m in result.monthly}
        march = monthly_by_label[f"{year:04d}-03-01"]
        assert march.revenue == Decimal("1200.00")
        assert march.expense == Decimal("400.00")
        assert march.profit == Decimal("800.00")

        sep = monthly_by_label[f"{year:04d}-09-01"]
        assert sep.revenue == Decimal("2500.00")
        assert sep.expense == Decimal("700.00")
        assert sep.profit == Decimal("1800.00")


# ---------------------------------------------------------------------------
# Tests: Top expense categories
# ---------------------------------------------------------------------------


class TestTopExpenseCategories:
    """Top categories: sorted by net descending, capped at 5."""

    @pytest.mark.asyncio
    async def test_sorted_descending_by_net(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        rows = [
            _make_expense_row(category_name="Cat A", net="300.00"),
            _make_expense_row(category_name="Cat B", net="100.00"),
            _make_expense_row(category_name="Cat C", net="500.00"),
        ]
        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report(rows, year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        names = [c.category_name for c in result.top_expense_categories]
        nets = [c.net for c in result.top_expense_categories]
        assert names == ["Cat C", "Cat A", "Cat B"]
        assert nets == [Decimal("500.00"), Decimal("300.00"), Decimal("100.00")]

    @pytest.mark.asyncio
    async def test_capped_at_5(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        rows = [
            _make_expense_row(category_name=f"Cat {i}", net=f"{(10 - i) * 100}.00")
            for i in range(1, 8)  # 7 categories
        ]
        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report(rows, year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert len(result.top_expense_categories) == 5

    @pytest.mark.asyncio
    async def test_fewer_than_5_returns_all(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        rows = [
            _make_expense_row(category_name="Cat A", net="100.00"),
            _make_expense_row(category_name="Cat B", net="200.00"),
        ]
        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report(rows, year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert len(result.top_expense_categories) == 2

    @pytest.mark.asyncio
    async def test_top_category_net_matches_expense_report(self) -> None:
        """Top category net must match the expense report's by_category net."""
        year = 2026
        company = _make_company()
        session = _mock_session()

        cid = str(uuid.uuid4())
        rows = [
            _make_expense_row(category_id=cid, category_name="Software", net="450.00"),
            _make_expense_row(category_name="Travel", net="200.00"),
        ]
        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report(rows, year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        top = result.top_expense_categories[0]
        assert top.category_name == "Software"
        assert top.category_id == cid
        assert top.net == Decimal("450.00")


# ---------------------------------------------------------------------------
# Tests: VAT payable == compute_vat_return.totals.net_payable
# ---------------------------------------------------------------------------


class TestVatPayableConsistency:
    """current_quarter_vat_payable must equal the VAT service's net_payable."""

    @pytest.mark.asyncio
    async def test_vat_payable_positive(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_return_with_net_payable("1234.56", year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        # The dashboard value must equal the VAT report's net_payable
        expected = vat_report.totals.net_payable_or_refundable.vat
        assert result.kpi.current_quarter_vat_payable == expected

    @pytest.mark.asyncio
    async def test_vat_payable_negative_refundable(self) -> None:
        """Negative net_payable (refundable) must be preserved as-is."""
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_return_with_net_payable("-500.00", year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert result.kpi.current_quarter_vat_payable == Decimal("-500.00")


# ---------------------------------------------------------------------------
# Tests: Q selection in context of compute_dashboard
# ---------------------------------------------------------------------------


class TestQuarterSelectionInDashboard:
    """Verify compute_vat_return is called with the correct quarter."""

    @pytest.mark.asyncio
    async def test_current_year_uses_todays_quarter(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        mock_vat = AsyncMock(return_value=vat_report)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch("jai.services.reporting.dashboard.compute_vat_return", new=mock_vat),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            # Today is in Q2
            mock_dt.now.return_value.date.return_value = date(year, 5, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        # Should have been called with quarter=2
        call_kwargs = mock_vat.call_args
        assert call_kwargs.kwargs.get("quarter") == 2 or call_kwargs.args[3] == 2
        assert result.quarter == 2

    @pytest.mark.asyncio
    async def test_prior_year_uses_q4(self) -> None:
        year = 2025
        company = _make_company()
        session = _mock_session()

        pl_report = _make_pl_report(year=year)
        vat_report = _make_vat_report(year=year, quarter=4)
        expense_rpt = _make_expense_report([], year=year)

        mock_vat = AsyncMock(return_value=vat_report)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch("jai.services.reporting.dashboard.compute_vat_return", new=mock_vat),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            # Today is in 2026 Q2 → prior year → Q4
            mock_dt.now.return_value.date.return_value = date(2026, 5, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        call_kwargs = mock_vat.call_args
        # Expect quarter=4 passed as keyword arg
        assert call_kwargs.kwargs.get("quarter") == 4 or call_kwargs.args[3] == 4
        assert result.quarter == 4


# ---------------------------------------------------------------------------
# Tests: Amount precision
# ---------------------------------------------------------------------------


class TestAmountPrecision:
    """No secondary rounding; Decimal values are preserved as-is."""

    @pytest.mark.asyncio
    async def test_decimal_amounts_preserved(self) -> None:
        year = 2026
        company = _make_company()
        session = _mock_session()

        series = [_make_series_item(year, 3, "1234.56", "567.89", "666.67")]
        pl_report = _make_pl_report(
            revenue_net="1234.56",
            expense_actual="567.89",
            profit="666.67",
            series=series,
            year=year,
        )
        vat_report = _make_vat_report(net_payable="98.76", year=year, quarter=2)
        expense_rpt = _make_expense_report([], year=year)

        with (
            patch(
                "jai.services.reporting.dashboard.compute_profit_loss",
                new=AsyncMock(return_value=pl_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_vat_return",
                new=AsyncMock(return_value=vat_report),
            ),
            patch(
                "jai.services.reporting.dashboard.compute_expense_report",
                new=AsyncMock(return_value=expense_rpt),
            ),
            patch("jai.services.reporting.dashboard.datetime") as mock_dt,
        ):
            mock_dt.now.return_value.date.return_value = date(year, 6, 15)
            result = await compute_dashboard(
                session, company=company, year=year, tiers=_DEFAULT_TIERS
            )

        assert result.kpi.ytd_revenue == Decimal("1234.56")
        assert result.kpi.ytd_expense == Decimal("567.89")
        assert result.kpi.ytd_profit == Decimal("666.67")
        assert result.kpi.current_quarter_vat_payable == Decimal("98.76")


# ---------------------------------------------------------------------------
# Helpers for additional tests
# ---------------------------------------------------------------------------


def _make_vat_return_with_net_payable(net_payable: str, year: int, quarter: int) -> VatReturnReport:
    """Alias for _make_vat_report – clearly named for test readability."""
    return _make_vat_report(net_payable=net_payable, year=year, quarter=quarter)
