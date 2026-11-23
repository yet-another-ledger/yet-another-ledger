from calendar import monthrange
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

import jai.services.fixed_asset_preview as preview_service
from jai.models.expense import Expense
from jai.schemas.expense import FixedAssetConfirmRequest, FixedAssetPreviewRequest
from jai.services.fixed_asset_preview import (
    apply_yearly_business_use,
    build_fixed_asset_preview,
    build_monthly_fixed_asset_schedule,
)


def _expense(**overrides: object) -> Expense:
    values: dict[str, object] = {
        "id": uuid4(),
        "kind": "PURCHASE",
        "expense_date": date(2024, 2, 29),
        "base_net_amount": Decimal("1000.00"),
        "base_vat_amount": Decimal("210.00"),
        "base_gross_amount": Decimal("1210.00"),
        "vat_treatment_effect": "APPLY_RATE",
        "vat_treatment_code": "NL_DOMESTIC_PURCH",
        "deductible": True,
        "pl_cost_basis": "AUTO",
        "business_percentage": Decimal("100"),
        "depreciation_years": 5,
        "include_in_pl": True,
        "is_draft": False,
    }
    values.update(overrides)
    return Expense(**values)


def _request(**overrides: object) -> FixedAssetPreviewRequest:
    values: dict[str, object] = {
        "asset_name": "Tester",
        "in_service_date": date(2024, 2, 29),
        "useful_life_years": 5,
        "residual_value": Decimal("0"),
        "business_percentage": Decimal("100"),
        "use_type": "FULL_BUSINESS",
    }
    values.update(overrides)
    return FixedAssetPreviewRequest(**values)


def test_schedule_prorates_leap_day_and_settles_rounding_in_final_month() -> None:
    result = build_fixed_asset_preview(_expense(), _request())

    assert result.acquisition_cost == Decimal("1000.00")
    assert result.annual_baseline == Decimal("200.00")
    assert result.monthly_schedule[0].period_end == date(2024, 2, 29)
    assert result.monthly_schedule[0].amount == Decimal("0.57")
    assert len(result.monthly_schedule) == 61
    assert sum((row.amount for row in result.monthly_schedule), Decimal("0")) == Decimal("1000.00")
    assert result.monthly_schedule[-1].closing_book_value == Decimal("0.00")
    assert result.monthly_schedule[0].opening_book_value == Decimal("1000.00")
    assert result.monthly_schedule[-1].cumulative_amount == Decimal("1000.00")
    for row in result.monthly_schedule:
        assert row.opening_book_value - row.amount == row.closing_book_value
        assert row.amount - row.business_amount == row.private_use_reduction


def test_preview_token_normalizes_decimal_scales_and_confirmation_rejects_garbage() -> None:
    expense = _expense(company_id=uuid4())
    first = build_fixed_asset_preview(
        expense,
        _request(residual_value=Decimal("0"), business_percentage=Decimal("100")),
    )
    equivalent = build_fixed_asset_preview(
        expense,
        _request(residual_value=Decimal("0.00"), business_percentage=Decimal("100.0")),
    )
    assert first.preview_token == equivalent.preview_token
    with pytest.raises(ValidationError):
        FixedAssetConfirmRequest(**_request().model_dump(), preview_token="é" * 64)


def test_cumulative_half_cent_uses_one_precise_division_before_rounding() -> None:
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2024, 2, 29),
            base_net_amount=Decimal("450.95"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("450.95"),
        ),
        _request(in_service_date=date(2024, 2, 29), useful_life_years=5),
    )
    march = next(row for row in result.monthly_schedule if row.period_end == date(2024, 3, 31))
    assert march.cumulative_amount == Decimal("7.78")
    assert march.amount == Decimal("7.52")


def test_residual_value_reduces_depreciation_and_yearly_deltas() -> None:
    result = build_fixed_asset_preview(
        _expense(),
        _request(residual_value=Decimal("100.00"), in_service_date=date(2024, 2, 29)),
    )

    assert result.depreciable_amount == Decimal("900.00")
    assert result.annual_baseline == Decimal("180.00")
    assert result.monthly_schedule[-1].closing_book_value == Decimal("100.00")
    assert result.yearly_deltas[0].legacy_expense_amount == Decimal("200.00")
    assert result.yearly_deltas[0].fixed_asset_depreciation == sum(
        (row.business_amount for row in result.monthly_schedule if row.period_end.year == 2024),
        Decimal("0"),
    )
    assert result.yearly_deltas[0].difference == (
        result.yearly_deltas[0].fixed_asset_depreciation - Decimal("200.00")
    )
    assert result.yearly_deltas[0].opening_book_value == Decimal("1000.00")
    assert result.yearly_deltas[0].closing_book_value < Decimal("1000.00")


def test_new_asset_basis_is_rounded_to_minor_unit_without_mutating_purchase() -> None:
    expense = _expense(
        base_net_amount=Decimal("100.005"),
        base_vat_amount=Decimal("0.000"),
        base_gross_amount=Decimal("100.005"),
    )
    result = build_fixed_asset_preview(
        expense,
        _request(residual_value=Decimal("0.00")),
    )
    assert result.acquisition_cost == Decimal("100.01")
    assert expense.base_net_amount == Decimal("100.005")
    assert expense.base_gross_amount == Decimal("100.005")
    assert all(row.amount >= 0 for row in result.monthly_schedule)
    assert all(row.closing_book_value >= 0 for row in result.monthly_schedule)
    assert sum((row.business_amount for row in result.monthly_schedule), Decimal("0")) == Decimal(
        "100.01"
    )
    assert result.monthly_schedule[-1].cumulative_amount == Decimal("100.01")
    assert result.monthly_schedule[-1].closing_book_value == Decimal("0.00")


@pytest.mark.parametrize(("raw_cost", "locked_cost"), [("100.005", "100.01"), ("1.009", "1.01")])
def test_new_asset_basis_rounds_historical_three_place_values(
    raw_cost: str, locked_cost: str
) -> None:
    result = build_fixed_asset_preview(
        _expense(
            base_net_amount=Decimal(raw_cost),
            base_vat_amount=Decimal("0.000"),
            base_gross_amount=Decimal(raw_cost),
        ),
        _request(in_service_date=date(2024, 2, 29), useful_life_years=5),
    )
    assert result.acquisition_cost == Decimal(locked_cost)
    assert all(row.amount >= 0 for row in result.monthly_schedule)
    assert all(row.closing_book_value >= 0 for row in result.monthly_schedule)
    assert result.monthly_schedule[-1].cumulative_amount == Decimal(locked_cost)
    assert result.monthly_schedule[-1].closing_book_value == Decimal("0.00")


def test_pending_in_service_returns_no_schedule() -> None:
    result = build_fixed_asset_preview(_expense(), _request(in_service_date=None))
    assert result.pending_in_service is True
    assert result.monthly_schedule == []


def test_preview_does_not_mutate_saved_expense() -> None:
    expense = _expense()
    original = {
        key: getattr(expense, key)
        for key in (
            "expense_date",
            "base_net_amount",
            "base_vat_amount",
            "base_gross_amount",
            "business_percentage",
            "depreciation_years",
        )
    }

    build_fixed_asset_preview(expense, _request())

    assert original == {key: getattr(expense, key) for key in original}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"residual_value": Decimal("1000.01")}, "Residual value"),
        ({"useful_life_years": 1}, "20%"),
        ({"business_percentage": Decimal("80")}, "Full-business"),
        ({"asset_name": "   "}, "Asset name"),
    ],
)
def test_invalid_terms_are_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_fixed_asset_preview(_expense(), _request(**overrides))


def test_irrecoverable_vat_is_in_acquisition_cost() -> None:
    result = build_fixed_asset_preview(
        _expense(deductible=False), _request(residual_value=Decimal("0"))
    )
    assert result.acquisition_cost == Decimal("1210.00")


def test_mileage_expense_is_not_convertible() -> None:
    with pytest.raises(ValueError, match="purchase expenses"):
        build_fixed_asset_preview(_expense(kind="MILEAGE"), _request())


def test_m10_anniversary_periods_block_prior_year_even_when_annual_total_matches() -> None:
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2024, 1, 1),
            base_net_amount=Decimal("1200.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1200.00"),
            depreciation_years=5,
        ),
        _request(in_service_date=date(2024, 1, 1), useful_life_years=5),
    )

    year = next(row for row in result.yearly_deltas if row.year == 2024)
    jan = next(row for row in result.period_deltas if row.period_end == date(2024, 1, 31))
    assert year.legacy_expense_amount == Decimal("240.00")
    assert year.fixed_asset_depreciation == Decimal("240.00")
    assert jan.legacy_expense_amount == Decimal("240.00")
    assert jan.fixed_asset_depreciation == Decimal("20.00")
    assert jan.difference == Decimal("-220.00")
    assert result.conversion_blocked is True
    assert result.conversion_block_reason is not None
    assert 2024 in result.prior_years_affected


def test_completed_month_as_of_date_excludes_current_future_month_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 11, 23)

    monkeypatch.setattr(preview_service, "date", FrozenDate)
    result = build_fixed_asset_preview(
        _expense(expense_date=date(2024, 1, 1)),
        _request(in_service_date=date(2024, 1, 1)),
    )
    current_month = result.reporting_as_of.replace(day=1)
    current_month_end = date(
        current_month.year,
        current_month.month,
        monthrange(current_month.year, current_month.month)[1],
    )
    period = next(row for row in result.period_deltas if row.period_end == current_month_end)
    assert period.completed_period is (current_month_end <= result.reporting_as_of)
    if current_month_end > result.reporting_as_of:
        assert period.fixed_asset_depreciation == Decimal("0.00")
        assert period.planned_fixed_asset_depreciation > Decimal("0.00")


def test_mixed_use_rounds_cumulatively_and_keeps_year_rates_independent() -> None:
    class Use:
        def __init__(self, year: int, estimate: str, actual: str | None = None) -> None:
            self.year = year
            self.estimated_business_percentage = Decimal(estimate)
            self.actual_business_percentage = Decimal(actual) if actual is not None else None

    schedule = build_monthly_fixed_asset_schedule(
        Decimal("0.50"), Decimal("0"), 1, date(2024, 1, 1), Decimal("100")
    )
    apply_yearly_business_use(schedule, Decimal("50"), [], "MIXED_USE")
    rows = build_monthly_fixed_asset_schedule(
        Decimal("1200"), Decimal("0"), 5, date(2024, 1, 1), Decimal("100")
    )
    years = [Use(2024, "50", "40"), Use(2025, "80")]
    provisional = apply_yearly_business_use(rows, Decimal("50"), years, "MIXED_USE")
    assert sum((r.business_amount for r in schedule), Decimal("0")) == Decimal("0.25")
    assert sum(
        (r.business_amount for r in rows if r.period_end.year == 2024), Decimal("0")
    ) == Decimal("96.00")
    assert sum(
        (r.business_amount for r in rows if r.period_end.year == 2025), Decimal("0")
    ) == Decimal("192.00")
    assert 2024 not in provisional
    assert 2025 in provisional
    assert 2026 in provisional  # the latest annual row (estimate) carries forward
    assert sum(
        (r.business_amount for r in rows if r.period_end.year == 2026), Decimal("0")
    ) == Decimal("192.00")
    assert sum((r.amount for r in rows), Decimal("0")) == Decimal("1200.00")


def test_pre_purchase_service_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="earlier than the purchase date"):
        build_fixed_asset_preview(
            _expense(expense_date=date(2024, 2, 1)),
            _request(in_service_date=date(2024, 1, 31)),
        )


def test_service_date_cannot_expand_preview_beyond_100_years() -> None:
    with pytest.raises(ValueError, match="100-year preview window"):
        build_fixed_asset_preview(
            _expense(expense_date=date(2024, 1, 1)),
            _request(in_service_date=date(2125, 1, 1)),
        )


def test_monthly_rounding_uses_unrounded_basis_and_distributes_small_amount() -> None:
    rounded = build_fixed_asset_preview(
        _expense(
            base_net_amount=Decimal("600.29"),
            base_vat_amount=Decimal("0"),
            base_gross_amount=Decimal("600.29"),
            expense_date=date(2024, 1, 1),
        ),
        _request(in_service_date=date(2024, 1, 1)),
    )
    assert rounded.monthly_schedule[0].amount == Decimal("10.00")
    assert rounded.monthly_schedule[-1].amount == Decimal("10.00")
    assert max(row.amount for row in rounded.monthly_schedule) == Decimal("10.01")
    assert sum((row.amount for row in rounded.monthly_schedule), Decimal("0")) == Decimal("600.29")

    tiny = build_fixed_asset_preview(
        _expense(
            base_net_amount=Decimal("500"),
            base_vat_amount=Decimal("0"),
            base_gross_amount=Decimal("500"),
            expense_date=date(2024, 1, 1),
        ),
        _request(in_service_date=date(2024, 1, 1), residual_value=Decimal("499.50")),
    )
    nonzero_offsets = [i for i, row in enumerate(tiny.monthly_schedule) if row.amount]
    assert len(nonzero_offsets) == 50
    assert nonzero_offsets[-1] >= 58
    assert sum((row.amount for row in tiny.monthly_schedule), Decimal("0")) == Decimal("0.50")


def test_residual_value_rejects_sub_cent_precision() -> None:
    with pytest.raises(ValueError):
        _request(residual_value=Decimal("499.995"))


def test_long_legacy_life_has_bounded_year_and_period_output() -> None:
    result = build_fixed_asset_preview(
        _expense(depreciation_years=100000),
        _request(in_service_date=None),
    )
    assert len(result.yearly_deltas) <= 101
    assert len(result.period_deltas) <= 1212
    assert result.legacy_preview_truncated is True
    assert result.preview_period_start == date(2024, 2, 1)
    assert result.preview_period_end == date(2124, 12, 31)


def test_preview_covers_every_long_schedule_month_and_m10_101st_anniversary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 1, 15)

    monkeypatch.setattr(preview_service, "date", FrozenDate)
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 1, 15),
            base_net_amount=Decimal("1200.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1200.00"),
            depreciation_years=101,
        ),
        _request(in_service_date=date(2026, 1, 15), useful_life_years=100),
    )

    last_schedule = result.monthly_schedule[-1]
    assert last_schedule.period_end == date(2126, 1, 31)
    assert last_schedule.amount == Decimal("0.45")
    last_asset_period = next(
        row for row in result.period_deltas if row.period_end == date(2126, 1, 31)
    )
    assert last_asset_period.planned_fixed_asset_depreciation == Decimal("0.45")
    assert last_asset_period.planned_legacy_expense_amount == Decimal("11.88")
    assert last_asset_period.legacy_attribution_date == date(2126, 1, 15)
    period_ends = {row.period_end for row in result.period_deltas}
    assert all(row.period_end in period_ends for row in result.monthly_schedule)
    assert sum(
        (
            row.fixed_asset_depreciation + row.planned_fixed_asset_depreciation
            for row in result.period_deltas
        ),
        Decimal("0"),
    ) == Decimal("1200.00")
    assert sum((row.asset_depreciation for row in result.yearly_deltas), Decimal("0")) == Decimal(
        "1200.00"
    )
    assert len(result.period_deltas) == 1212
    assert result.preview_period_end == date(2126, 12, 31)
    assert result.legacy_preview_truncated is False
    for yearly in result.yearly_deltas:
        periods = [row for row in result.period_deltas if row.period_end.year == yearly.year]
        assert (
            sum(
                (
                    row.fixed_asset_depreciation + row.planned_fixed_asset_depreciation
                    for row in periods
                ),
                Decimal("0"),
            )
            == yearly.asset_depreciation
        )
        assert sum((row.legacy_expense_amount for row in periods), Decimal("0")) == (
            yearly.legacy_expense_amount
        )
        if yearly.year > result.reporting_as_of.year:
            assert (
                sum((row.planned_legacy_expense_amount for row in periods), Decimal("0"))
                == yearly.planned_legacy_expense_amount
            )


def test_legacy_term_beyond_supported_calendar_is_explicitly_truncated() -> None:
    result = build_fixed_asset_preview(
        _expense(expense_date=date(9999, 1, 1), depreciation_years=100000),
        _request(in_service_date=None),
    )
    assert result.legacy_preview_truncated is True
    assert result.preview_period_start == date(9999, 1, 1)
    assert result.preview_period_end == date(9999, 12, 31)


def test_anniversary_and_month_end_attribution_preserve_actual_and_planned_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 11, 23)

    monkeypatch.setattr(preview_service, "date", FrozenDate)
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 1, 16),
            base_net_amount=Decimal("1200.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1200.00"),
        ),
        _request(in_service_date=date(2026, 1, 16)),
    )

    january = next(row for row in result.period_deltas if row.period_end == date(2026, 1, 31))
    assert january.legacy_attribution_date == date(2026, 1, 16)
    assert january.legacy_expense_amount == Decimal("240.00")
    assert january.planned_legacy_expense_amount == Decimal("0.00")
    assert january.fixed_asset_attribution_date == date(2026, 1, 31)
    assert january.fixed_asset_depreciation == Decimal("10.32")
    assert january.difference == Decimal("-229.68")
    # An M10 range ending before Jan 31 includes the old anniversary but not
    # the new month's depreciation event.
    assert january.legacy_attribution_date <= date(2026, 1, 30)
    assert january.fixed_asset_attribution_date > date(2026, 1, 30)
    assert january.legacy_expense_amount == Decimal("240.00")
    assert january.fixed_asset_depreciation == Decimal("10.32")

    future = next(row for row in result.period_deltas if row.period_end == date(2027, 1, 31))
    assert future.legacy_attribution_date == date(2027, 1, 16)
    assert future.legacy_expense_amount == Decimal("240.00")
    assert future.planned_legacy_expense_amount == Decimal("240.00")
    assert future.fixed_asset_attribution_date == date(2027, 1, 31)
    assert future.planned_fixed_asset_depreciation == Decimal("20.00")
    assert future.planned_difference == Decimal("-220.00")


def test_december_report_delta_includes_future_m10_anniversary_but_not_future_asset_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 11, 23)

    monkeypatch.setattr(preview_service, "date", FrozenDate)
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 12, 15),
            base_net_amount=Decimal("1200.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1200.00"),
        ),
        _request(in_service_date=date(2026, 12, 15)),
    )

    december = next(row for row in result.period_deltas if row.period_end == date(2026, 12, 31))
    assert december.legacy_expense_amount == Decimal("240.00")
    assert december.fixed_asset_depreciation == Decimal("0.00")
    assert december.difference == Decimal("-240.00")
    assert december.planned_fixed_asset_depreciation > Decimal("0.00")
    assert december.planned_difference == (
        december.planned_fixed_asset_depreciation - december.planned_legacy_expense_amount
    )


def test_first_partial_month_uses_normal_rate_and_final_period_settles_exact_remainder() -> None:
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 1, 16),
            base_net_amount=Decimal("1200.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1200.00"),
        ),
        _request(in_service_date=date(2026, 1, 16)),
    )
    assert [row.amount for row in result.monthly_schedule[:2]] == [
        Decimal("10.32"),
        Decimal("20.00"),
    ]
    assert result.monthly_schedule[-1].amount == Decimal("9.68")
    assert sum((row.amount for row in result.monthly_schedule), Decimal("0")) == Decimal("1200.00")


def test_business_share_is_allocated_from_cumulative_depreciation() -> None:
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 1, 1),
            base_net_amount=Decimal("0.50"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("0.50"),
        ),
        _request(
            in_service_date=date(2026, 1, 1),
            residual_value=Decimal("0"),
            business_percentage=Decimal("50"),
            use_type="MIXED_USE",
        ),
    )
    assert sum((row.amount for row in result.monthly_schedule), Decimal("0")) == Decimal("0.50")
    assert sum((row.business_amount for row in result.monthly_schedule), Decimal("0")) == Decimal(
        "0.25"
    )


def test_partial_rate_conversion_preview_matches_year_local_report_rounding() -> None:
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 10, 13),
            base_net_amount=Decimal("1000.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1000.00"),
        ),
        _request(
            in_service_date=date(2026, 10, 13),
            residual_value=Decimal("0"),
            useful_life_years=5,
            business_percentage=Decimal("30"),
            use_type="MIXED_USE",
        ),
    )
    february = next(row for row in result.monthly_schedule if row.period_end == date(2027, 2, 28))
    assert february.business_amount == Decimal("5.00")


def test_annual_planned_and_realized_values_reconcile_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 11, 23)

    monkeypatch.setattr(preview_service, "date", FrozenDate)
    result = build_fixed_asset_preview(
        _expense(
            expense_date=date(2026, 1, 16),
            base_net_amount=Decimal("1200.00"),
            base_vat_amount=Decimal("0.00"),
            base_gross_amount=Decimal("1200.00"),
        ),
        _request(in_service_date=date(2026, 1, 16)),
    )
    for row in result.monthly_schedule:
        assert row.opening_book_value - row.amount == row.closing_book_value
        assert row.amount - row.business_amount == row.private_use_reduction
    for row in result.yearly_deltas:
        assert row.opening_book_value - row.realized_asset_depreciation == row.closing_book_value
        assert (
            row.realized_asset_depreciation - row.fixed_asset_depreciation
            == row.business_use_reduction
        )
        assert (
            row.planned_opening_book_value - row.asset_depreciation
            == row.planned_closing_book_value
        )
        assert (
            row.asset_depreciation - row.planned_fixed_asset_depreciation
            == row.planned_business_use_reduction
        )
        assert row.planned_difference == (
            row.planned_fixed_asset_depreciation - row.planned_legacy_expense_amount
        )
        if row.year == 2026:
            assert row.opening_book_value == Decimal("1200.00")
            assert row.realized_asset_depreciation == Decimal("190.32")
            assert row.closing_book_value == Decimal("1009.68")
            assert row.business_use_reduction == Decimal("0.00")
            assert row.planned_difference == Decimal("-9.68")
        if row.year == 2027:
            assert row.realized_asset_depreciation == Decimal("0.00")
            assert row.asset_depreciation == Decimal("240.00")
            assert row.planned_fixed_asset_depreciation == Decimal("240.00")

    for row in result.period_deltas:
        assert row.difference == row.fixed_asset_depreciation - row.legacy_expense_amount
        assert row.planned_difference == (
            row.planned_fixed_asset_depreciation - row.planned_legacy_expense_amount
        )


def test_anniversary_output_stops_at_supported_calendar_end() -> None:
    result = build_fixed_asset_preview(
        _expense(expense_date=date(9999, 1, 1), depreciation_years=2),
        _request(in_service_date=None),
    )
    assert [row.year for row in result.yearly_deltas] == [9999]


def test_extreme_residual_value_is_rejected_by_request_contract() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _request(residual_value=Decimal("1e40"))
    assert exc_info.value.errors()[0]["type"] == "decimal_max_digits"
