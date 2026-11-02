"""Pydantic schemas for recurring expenses (M8 step 3).

RecurringExpenseInput         – request body (raw user input only; no gross/snapshots).
RecurringExpenseRead          – full template record as returned by read endpoints.
RecurringExpenseListResponse  – paginated list.
RunNowResponse                – result of POST /recurring-expenses/{id}/run-now.

Schema layer never computes amounts (red-line 1).  All monetary fields are
``Decimal``.  Text fields use plain ``str`` (red-line 10).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from jai.models._enums import PaidBy, PlCostBasis

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class RecurringExpenseInput(BaseModel):
    """Request body for POST/PUT /api/v1/recurring-expenses.

    Only collects raw user input.  Computed fields (gross, snapshots,
    next_run_date, occurrences_generated) are derived by the service (red-line 1).
    """

    name: str = Field(description="Human-readable name for this recurring template.")
    category_id: uuid.UUID
    supplier_name: str | None = None
    vat_treatment_id: uuid.UUID
    vat_rate_id: uuid.UUID

    net_amount: Decimal = Field(ge=0, description="Net (excl. VAT) amount ≥ 0.")
    vat_amount: Decimal = Field(ge=0, description="VAT amount ≥ 0.")

    deductible: bool | None = Field(
        default=None,
        description=(
            "Whether expenses generated from this template are VAT-deductible. "
            "Defaults to category.default_deductible, then True."
        ),
    )
    note: str | None = None

    # -- Bookkeeping fields (M8.5 parity, D8) ---------------------------------
    paid_by: PaidBy = PaidBy.BUSINESS
    business_percentage: Decimal = Field(
        default=Decimal("100"),
        ge=0,
        le=100,
        description="Business-use percentage 0–100.",
    )
    depreciation_years: int = Field(
        default=1,
        ge=1,
        description="Depreciation years; 1 = fully expensed this year.",
    )
    include_in_pl: bool = True
    pl_cost_basis: PlCostBasis = PlCostBasis.NET

    # -- Schedule --------------------------------------------------------------
    frequency: str = Field(
        description="One of: MONTHLY, QUARTERLY, YEARLY.",
        pattern="^(MONTHLY|QUARTERLY|YEARLY)$",
    )
    start_date: date = Field(
        description="Date from which the first occurrence is scheduled."
    )
    end_date: date | None = None
    max_occurrences: int | None = Field(
        default=None,
        ge=1,
        description="Optional cap on total generated occurrences.",
    )
    active: bool = Field(default=True, description="Whether the template is active.")

    @field_validator("net_amount", "vat_amount")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("Amount must be ≥ 0.")
        return v


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class RecurringExpenseRead(BaseModel):
    """Full recurring expense template as returned by read endpoints."""

    id: uuid.UUID
    name: str

    # Category (FK SET NULL + snapshot)
    category_id: uuid.UUID | None = None
    category_name: str | None = None

    supplier_name: str | None = None

    # VAT treatment (FK RESTRICT + snapshot)
    vat_treatment_id: uuid.UUID | None = None
    vat_treatment_code: str
    vat_treatment_label: str
    vat_treatment_effect: str

    # VAT rate (FK RESTRICT + snapshot)
    vat_rate_id: uuid.UUID | None = None
    vat_rate_percent: Decimal
    vat_rate_label: str

    # Template amounts
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal

    # Deductibility
    deductible: bool

    note: str | None = None

    # Bookkeeping fields (M8.5 parity, D8)
    paid_by: PaidBy
    business_percentage: Decimal
    depreciation_years: int
    include_in_pl: bool
    pl_cost_basis: PlCostBasis

    # Schedule
    frequency: str
    start_date: date
    end_date: date | None = None
    max_occurrences: int | None = None

    # Generation tracking
    occurrences_generated: int
    next_run_date: date
    last_generated_at: datetime | None = None

    # Active flag
    active: bool

    # Timestamps
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# List response
# ---------------------------------------------------------------------------


class RecurringExpenseListResponse(BaseModel):
    """Paginated recurring expenses list."""

    items: list[RecurringExpenseRead]
    total: int


# ---------------------------------------------------------------------------
# run-now response
# ---------------------------------------------------------------------------


class RunNowResponse(BaseModel):
    """Response for POST /api/v1/recurring-expenses/{id}/run-now."""

    generated: int = Field(
        description="Number of draft expenses created in this run (0 = already up-to-date)."
    )
    next_run_date: date = Field(
        description="The next_run_date on the template after the run."
    )
