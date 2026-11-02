"""Expense ORM model (M8 step 1).

Design:
- Expense is a standalone business document rooted on company_id (red-line 2).
  company_id FK RESTRICT prevents orphaned expenses.
- category_id FK SET NULL: deleting the category keeps the category_name
  snapshot intact (D12).
- vat_treatment_id / vat_rate_id FK RESTRICT: snapshot fields capture the
  values at creation time so deleting dictionary entries is blocked while
  referenced (code/label/effect for treatment; percent/label for rate).
- creator_id FK SET NULL: deleting the user preserves the expense.
- All money columns are NUMERIC(18,3) – same scale as quantize_money().
- exchange_rate is NUMERIC(18,8); v1 always 1 (single base currency, D10).
- Text columns use ``Text`` (red-line 10).
- recurring_expense_id is NOT added in step 1; it will be added as an
  additive column + FK in step 3 when the recurring_expense table is built.
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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from jai.db import Base
from jai.models._enums import ExpenseKind

# ---------------------------------------------------------------------------
# Money column type alias – matches invoice.py / payment.py convention
# ---------------------------------------------------------------------------

_MONEY = Numeric(18, 3)
_RATE = Numeric(6, 3)
_FX = Numeric(18, 8)


class Expense(Base):
    """Single expense record (purchase / cost entry)."""

    __tablename__ = "expense"
    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_expense_company_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # -- Tenant scoping (red-line 2) ------------------------------------------
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    kind: Mapped[ExpenseKind] = mapped_column(
        String(8),
        nullable=False,
        server_default=text("'PURCHASE'"),
        comment="PURCHASE for existing expenses; MILEAGE for generated trip projections.",
    )

    # -- Date -----------------------------------------------------------------
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # -- Category (FK SET NULL + name snapshot, D12) --------------------------
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expense_category.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Snapshot of category name; preserved after category deletion.",
    )

    # -- Supplier (v1: free-text, D12) ----------------------------------------
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- VAT treatment (FK RESTRICT + snapshot, D7) ---------------------------
    vat_treatment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_treatment.id", ondelete="RESTRICT"),
        nullable=True,
        comment="FK RESTRICT: cannot delete treatment while expense references it.",
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
        comment="FK RESTRICT: cannot delete rate while expense references it.",
    )
    vat_rate_percent: Mapped[object] = mapped_column(
        _RATE,
        nullable=False,
        comment="Snapshot of vat_rate.percent at creation time.",
    )
    vat_rate_label: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Snapshot of vat_rate.label."
    )

    # -- Amounts (D8: net + vat are both authoritative inputs; D11: NUMERIC(18,3)) --
    net_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    vat_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    gross_amount: Mapped[object] = mapped_column(
        _MONEY,
        nullable=False,
        comment="gross = net + vat, calculated by the service (D8).",
    )

    # -- Deductibility (D9) ---------------------------------------------------
    deductible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # -- Currency (D10: single base currency, FX columns reserved for future) --
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        comment="ISO 4217; v1 always equals company.base_currency.",
    )
    exchange_rate: Mapped[object] = mapped_column(
        _FX,
        nullable=False,
        server_default=text("1"),
        comment="Always 1 in v1 (single base currency).",
    )
    base_net_amount: Mapped[object] = mapped_column(
        _MONEY,
        nullable=False,
        comment="Base-currency net; equals net_amount while exchange_rate = 1.",
    )
    base_vat_amount: Mapped[object] = mapped_column(
        _MONEY,
        nullable=False,
        comment="Base-currency VAT; equals vat_amount while exchange_rate = 1.",
    )
    base_gross_amount: Mapped[object] = mapped_column(
        _MONEY,
        nullable=False,
        comment="Base-currency gross; equals gross_amount while exchange_rate = 1.",
    )

    # -- Reference / notes (text, red-line 10) --------------------------------
    reference: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Ticket / invoice reference number from supplier."
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Bookkeeping fields (M8.5: pure storage, no calculation, D1–D4) --------
    paid_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'BUSINESS'"),
        comment=(
            "PaidBy enum: PRIVATE | BUSINESS. Payment source indicator – "
            "pure bookkeeping, does not affect any calculation (D2)."
        ),
    )
    business_percentage: Mapped[object] = mapped_column(
        _RATE,  # NUMERIC(6,3) – same scale as vat_rate_percent
        nullable=False,
        server_default=text("100"),
        comment=(
            "Business-use percentage 0–100. M10 will use this to prorate "
            "deductible VAT and cost amounts (D3). Stored as-is, no calculation here."
        ),
    )
    depreciation_years: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        comment=(
            "Depreciation years ≥ 1. 1 = fully expensed this year; "
            ">1 = multi-year asset, amortisation calculated by M10 (D4)."
        ),
    )

    include_in_pl: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    pl_cost_basis: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'NET'"))

    # -- Draft flag (D3: recurring expenses generate as draft) ----------------
    is_draft: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="True for auto-generated recurring-expense drafts (step 3).",
    )

    # -- Recurring expense link (step 3: additive column) ---------------------
    # Added in migration 0020.  FK SET NULL: deleting the template does NOT
    # cascade-delete the generated expense rows – they are independent records.
    recurring_expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recurring_expense.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "FK back to the recurring_expense template that generated this expense. "
            "SET NULL on template deletion: generated expenses remain as independent records."
        ),
    )

    # -- Creator (nullable FK: preserve expense if user is deleted) -----------
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
