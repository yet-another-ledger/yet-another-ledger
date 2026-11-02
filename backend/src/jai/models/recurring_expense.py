"""RecurringExpense ORM model (M8 step 3).

Design:
- RecurringExpense is a template rooted on company_id (red-line 2).
  company_id FK RESTRICT prevents orphaned templates.
- category_id FK SET NULL: deleting the category keeps the category_name
  snapshot intact (same pattern as Expense, D12).
- vat_treatment_id / vat_rate_id FK RESTRICT: snapshot fields capture the
  values at creation time (D7).
- creator_id FK SET NULL: deleting the user preserves the template.
- All money columns are NUMERIC(18,3) – same scale as Expense (D11).
- Text columns use ``Text`` (red-line 10).
- On delete of a RecurringExpense the generated Expense rows are NOT deleted;
  their ``recurring_expense_id`` column is SET NULL so they remain as
  independent accounting records (D3).
- ``recurring_expense_id`` on the ``expense`` table is added as an additive
  column in migration 0020 (step 3).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from jai.db import Base
from jai.models._enums import RecurringFrequency

# ---------------------------------------------------------------------------
# Money column type alias – matches expense.py / invoice.py convention
# ---------------------------------------------------------------------------

_MONEY = Numeric(18, 3)
_RATE = Numeric(6, 3)


class RecurringExpense(Base):
    """Recurring expense template that generates draft expenses on a schedule."""

    __tablename__ = "recurring_expense"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # -- Tenant scoping (red-line 2) ------------------------------------------
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # -- Human-readable template name (red-line 10) ---------------------------
    name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Display name for the recurring expense template."
    )

    # -- Category (FK SET NULL + name snapshot, D12) --------------------------
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expense_category.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Snapshot of category name; preserved after category deletion.",
    )

    # -- Supplier (v1: free-text) -----------------------------------------------
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- VAT treatment (FK RESTRICT + snapshot, D7) ---------------------------
    vat_treatment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_treatment.id", ondelete="RESTRICT"),
        nullable=True,
        comment="FK RESTRICT: cannot delete treatment while template references it.",
    )
    vat_treatment_code: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Snapshot: preserved after treatment update."
    )
    vat_treatment_label: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_effect: Mapped[str] = mapped_column(Text, nullable=False)

    # -- VAT rate (FK RESTRICT + snapshot, D7) --------------------------------
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_rate.id", ondelete="RESTRICT"),
        nullable=True,
        comment="FK RESTRICT: cannot delete rate while template references it.",
    )
    vat_rate_percent: Mapped[object] = mapped_column(
        _RATE,
        nullable=False,
        comment="Snapshot of vat_rate.percent at creation time.",
    )
    vat_rate_label: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Snapshot of vat_rate.label."
    )

    # -- Template amounts (D11: NUMERIC(18,3), quantised to minor unit) -------
    net_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    vat_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    gross_amount: Mapped[object] = mapped_column(
        _MONEY,
        nullable=False,
        comment="gross = net + vat, calculated by the service (D8).",
    )

    # -- Deductibility (D9) ---------------------------------------------------
    deductible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # -- Bookkeeping fields (M8.5 parity: same three columns as expense, D8) --
    paid_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'BUSINESS'"),
        comment=(
            "PaidBy enum: PRIVATE | BUSINESS. Template value; copied to "
            "generated Expense on generation (D8). Pure bookkeeping, no calc."
        ),
    )
    business_percentage: Mapped[object] = mapped_column(
        _RATE,  # NUMERIC(6,3)
        nullable=False,
        server_default=text("100"),
        comment=(
            "Business-use percentage 0–100. Template value; copied to "
            "generated Expense on generation (D8)."
        ),
    )
    depreciation_years: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        comment=(
            "Depreciation years ≥ 1. Template value; copied to generated "
            "Expense on generation (D8). Amortisation deferred to M10."
        ),
    )
    include_in_pl: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    pl_cost_basis: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'NET'")
    )

    # -- Notes (text, red-line 10) --------------------------------------------
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Schedule configuration -----------------------------------------------
    frequency: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="RecurringFrequency: MONTHLY | QUARTERLY | YEARLY.",
    )
    start_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="Date from which the first occurrence is scheduled.",
    )
    end_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Optional hard stop date; no occurrences generated after this.",
    )
    max_occurrences: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Optional cap on total generated occurrences.",
    )

    # -- Generation tracking --------------------------------------------------
    occurrences_generated: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="Total occurrences generated so far.",
    )
    next_run_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
        comment="Date of the next scheduled generation (inclusive).",
    )
    last_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of the last successful generation run.",
    )

    # -- Active flag ----------------------------------------------------------
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
        comment=(
            "False when paused by user or when max_occurrences / end_date is reached. "
            "Inactive templates are skipped by the scheduler."
        ),
    )

    # -- Creator (nullable FK: preserve template if user is deleted) ----------
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- Timestamps -----------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def frequency_enum(self) -> RecurringFrequency:
        """Return the frequency as a typed enum value."""
        return RecurringFrequency(self.frequency)
