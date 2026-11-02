"""Unit tests for M8.5 step 1 – bookkeeping field schema validation.

These are pure schema-layer tests (no DB required); they validate that
``ExpenseInput`` and ``RecurringExpenseInput`` enforce the range constraints
for the three new bookkeeping fields.

Coverage:
- Default values: paid_by=BUSINESS, business_percentage=100, depreciation_years=1
- Explicit values accepted: paid_by=PRIVATE, business_percentage=0–100, depreciation_years≥1
- Invalid: business_percentage <0 → ValidationError
- Invalid: business_percentage >100 → ValidationError
- Invalid: depreciation_years <1 → ValidationError
- Invalid: paid_by unknown value → ValidationError
- Same constraints apply to RecurringExpenseInput (parity)
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jai.models._enums import PaidBy, PlCostBasis
from jai.schemas.expense import ExpenseInput
from jai.schemas.recurring_expense import RecurringExpenseInput

# Minimal valid expense input (shared)
_MINIMAL_EXPENSE: dict = {
    "expense_date": "2026-06-14",
    "category_id": str(uuid.uuid4()),
    "vat_treatment_id": str(uuid.uuid4()),
    "vat_rate_id": str(uuid.uuid4()),
    "net_amount": "100.00",
    "vat_amount": "21.00",
}

_MINIMAL_RECURRING: dict = {
    "name": "Monthly Internet",
    "category_id": str(uuid.uuid4()),
    "vat_treatment_id": str(uuid.uuid4()),
    "vat_rate_id": str(uuid.uuid4()),
    "net_amount": "50.00",
    "vat_amount": "10.50",
    "frequency": "MONTHLY",
    "start_date": "2026-01-01",
}


# ---------------------------------------------------------------------------
# ExpenseInput – default values
# ---------------------------------------------------------------------------


class TestExpenseInputDefaults:
    """``ExpenseInput`` bookkeeping fields default to BUSINESS / 100 / 1."""

    def test_defaults_when_omitted(self) -> None:
        """Omitting all three bookkeeping fields → defaults applied."""
        inp = ExpenseInput(**_MINIMAL_EXPENSE)
        assert inp.paid_by == PaidBy.BUSINESS
        assert inp.business_percentage == Decimal("100")
        assert inp.depreciation_years == 1
        assert inp.include_in_pl is True
        assert inp.pl_cost_basis == PlCostBasis.NET

    def test_explicit_private(self) -> None:
        """paid_by='PRIVATE' is accepted and returned as PaidBy.PRIVATE."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "paid_by": "PRIVATE"})
        assert inp.paid_by == PaidBy.PRIVATE

    def test_explicit_business(self) -> None:
        """paid_by='BUSINESS' is accepted and returned as PaidBy.BUSINESS."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "paid_by": "BUSINESS"})
        assert inp.paid_by == PaidBy.BUSINESS

    def test_explicit_business_percentage_zero(self) -> None:
        """business_percentage=0 is accepted (lower bound)."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "business_percentage": "0"})
        assert inp.business_percentage == Decimal("0")

    def test_explicit_business_percentage_100(self) -> None:
        """business_percentage=100 is accepted (upper bound)."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "business_percentage": "100"})
        assert inp.business_percentage == Decimal("100")

    def test_explicit_business_percentage_partial(self) -> None:
        """business_percentage between 0 and 100 is accepted."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "business_percentage": "80.5"})
        assert inp.business_percentage == Decimal("80.5")

    def test_explicit_depreciation_years_1(self) -> None:
        """depreciation_years=1 is accepted (minimum)."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "depreciation_years": 1})
        assert inp.depreciation_years == 1
        assert inp.include_in_pl is True
        assert inp.pl_cost_basis == PlCostBasis.NET

    def test_explicit_cost_controls(self) -> None:
        inp = ExpenseInput(**{
            **_MINIMAL_EXPENSE,
            "include_in_pl": False,
            "pl_cost_basis": "GROSS",
        })
        assert inp.include_in_pl is False
        assert inp.pl_cost_basis == PlCostBasis.GROSS

    def test_explicit_depreciation_years_5(self) -> None:
        """depreciation_years=5 is accepted."""
        inp = ExpenseInput(**{**_MINIMAL_EXPENSE, "depreciation_years": 5})
        assert inp.depreciation_years == 5


# ---------------------------------------------------------------------------
# ExpenseInput – validation errors
# ---------------------------------------------------------------------------


class TestExpenseInputValidation:
    """``ExpenseInput`` bookkeeping field range validation."""

    def test_business_percentage_below_zero_rejected(self) -> None:
        """business_percentage=-1 → ValidationError."""
        with pytest.raises(ValidationError):
            ExpenseInput(**{**_MINIMAL_EXPENSE, "business_percentage": "-1"})

    def test_business_percentage_above_100_rejected(self) -> None:
        """business_percentage=101 → ValidationError."""
        with pytest.raises(ValidationError):
            ExpenseInput(**{**_MINIMAL_EXPENSE, "business_percentage": "101"})

    def test_business_percentage_100_001_rejected(self) -> None:
        """business_percentage=100.001 → ValidationError (> 100)."""
        with pytest.raises(ValidationError):
            ExpenseInput(**{**_MINIMAL_EXPENSE, "business_percentage": "100.001"})

    def test_depreciation_years_zero_rejected(self) -> None:
        """depreciation_years=0 → ValidationError (must be ≥ 1)."""
        with pytest.raises(ValidationError):
            ExpenseInput(**{**_MINIMAL_EXPENSE, "depreciation_years": 0})

    def test_depreciation_years_negative_rejected(self) -> None:
        """depreciation_years=-1 → ValidationError."""
        with pytest.raises(ValidationError):
            ExpenseInput(**{**_MINIMAL_EXPENSE, "depreciation_years": -1})

    def test_paid_by_invalid_value_rejected(self) -> None:
        """paid_by='CASH' (unknown enum) → ValidationError."""
        with pytest.raises(ValidationError):
            ExpenseInput(**{**_MINIMAL_EXPENSE, "paid_by": "CASH"})


# ---------------------------------------------------------------------------
# RecurringExpenseInput – parity (same constraints)
# ---------------------------------------------------------------------------


class TestRecurringExpenseInputDefaults:
    """``RecurringExpenseInput`` parity: same defaults and constraints as ExpenseInput."""

    def test_defaults_when_omitted(self) -> None:
        """Omitting bookkeeping fields → defaults BUSINESS / 100 / 1."""
        inp = RecurringExpenseInput(**_MINIMAL_RECURRING)
        assert inp.paid_by == PaidBy.BUSINESS
        assert inp.business_percentage == Decimal("100")
        assert inp.depreciation_years == 1
        assert inp.include_in_pl is True
        assert inp.pl_cost_basis == PlCostBasis.NET

    def test_explicit_private(self) -> None:
        inp = RecurringExpenseInput(**{**_MINIMAL_RECURRING, "paid_by": "PRIVATE"})
        assert inp.paid_by == PaidBy.PRIVATE

    def test_explicit_business_percentage_partial(self) -> None:
        inp = RecurringExpenseInput(**{**_MINIMAL_RECURRING, "business_percentage": "60"})
        assert inp.business_percentage == Decimal("60")

    def test_explicit_depreciation_years_10(self) -> None:
        inp = RecurringExpenseInput(**{**_MINIMAL_RECURRING, "depreciation_years": 10})
        assert inp.depreciation_years == 10


class TestRecurringExpenseInputValidation:
    """``RecurringExpenseInput`` bookkeeping field validation (parity)."""

    def test_business_percentage_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecurringExpenseInput(**{**_MINIMAL_RECURRING, "business_percentage": "-1"})

    def test_business_percentage_above_100_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecurringExpenseInput(**{**_MINIMAL_RECURRING, "business_percentage": "101"})

    def test_depreciation_years_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecurringExpenseInput(**{**_MINIMAL_RECURRING, "depreciation_years": 0})

    def test_paid_by_invalid_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecurringExpenseInput(**{**_MINIMAL_RECURRING, "paid_by": "UNKNOWN"})


# ---------------------------------------------------------------------------
# PaidBy enum
# ---------------------------------------------------------------------------


class TestPaidByEnum:
    """Verify the PaidBy StrEnum values."""

    def test_private_value(self) -> None:
        assert PaidBy.PRIVATE == "PRIVATE"

    def test_business_value(self) -> None:
        assert PaidBy.BUSINESS == "BUSINESS"

    def test_is_str(self) -> None:
        assert isinstance(PaidBy.PRIVATE, str)
        assert isinstance(PaidBy.BUSINESS, str)

    def test_from_string(self) -> None:
        assert PaidBy("PRIVATE") == PaidBy.PRIVATE
        assert PaidBy("BUSINESS") == PaidBy.BUSINESS

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            PaidBy("CASH")


def test_cost_basis_enum_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ExpenseInput(**{**_MINIMAL_EXPENSE, "pl_cost_basis": "VAT"})


@pytest.mark.parametrize("basis", list(PlCostBasis))
@pytest.mark.parametrize("included", [True, False])
def test_stored_cost_preview_retains_three_decimal_history(
    basis: PlCostBasis, included: bool,
) -> None:
    """Historical amounts are allocated before rounding, matching the existing P/L slice."""
    from jai.models.expense import Expense
    from jai.models.recurring_expense import RecurringExpense
    from jai.services.expense import compute_stored_cost_preview
    from jai.services.reporting.pl import _annual_slice

    common = {
        "vat_treatment_code": "NL_DOMESTIC_PURCH", "vat_treatment_effect": "APPLY_RATE",
        "deductible": False, "pl_cost_basis": basis.value, "include_in_pl": included,
        "business_percentage": Decimal("50"), "depreciation_years": 1,
    }
    expense = Expense(
        **common, kind="PURCHASE", base_net_amount=Decimal("100.005"),
        base_vat_amount=Decimal("21.005"), base_gross_amount=Decimal("121.010"),
    )
    template = RecurringExpense(
        **common, net_amount=Decimal("100.005"), vat_amount=Decimal("21.005"),
        gross_amount=Decimal("121.010"),
    )
    expected = Decimal("50.00") if basis == PlCostBasis.NET else Decimal("60.51")
    if not included:
        expected = Decimal("0.00")
    for snapshot in (expense, template):
        result = compute_stored_cost_preview(snapshot)
        assert result.annual_cost == expected
        assert result.annual_cost == _annual_slice(expense)
        assert result.included_in_pl is included
