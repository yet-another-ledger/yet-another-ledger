"""Quote ORM models – Quote / QuoteLine / QuoteTax / QuoteLineTax (M6 step 2).

Mirrors the four invoice tables but omits paid_status / due_amount / base_due_amount.
Adds valid_until, converted_invoice_id (reverse link from Convert), and uses
QuoteStatus instead of InvoiceStatus.

Design:
- company_id + customer_id use RESTRICT (same as invoice).
- converted_invoice_id is nullable FK to invoice.id ON DELETE SET NULL.
- Sub-tables cascade on DELETE from parent (red-line 3).
- product_id / unit_id use SET NULL so deleting catalogue entries never
  breaks historical quotes.
- vat_rate_id on lines / taxes uses RESTRICT.
- UNIQUE(company_id, quote_number) is the DB-level safeguard (red-line 4).
- All money columns are NUMERIC(18, 3) (red-line 1).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jai.db import Base
from jai.models._enums import DiscountType, InvoiceTaxMode, QuoteSettlementMode, QuoteStatus

if TYPE_CHECKING:
    from jai.models.pdf_appendix import PdfAppendixFile, QuoteAppendix

_MONEY = Numeric(18, 3)
_RATE = Numeric(6, 3)
_FX = Numeric(18, 8)


class Quote(Base):
    """Top-level quote document."""

    __tablename__ = "quote"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # -- Numbering (red-line 4) -----------------------------------------------
    quote_number: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    customer_sequence_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unique_hash: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Reserved for M9 public link; not active in M6."
    )
    reference_number: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Dates ----------------------------------------------------------------
    quote_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    # -- Lifecycle status (no paid dimension for quotes) ----------------------
    status: Mapped[QuoteStatus] = mapped_column(
        nullable=False, server_default="DRAFT"
    )
    appendix_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    settlement_mode: Mapped[QuoteSettlementMode] = mapped_column(
        nullable=False, server_default="UNSET"
    )
    settlement_mode_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # -- Convert reverse link -------------------------------------------------
    converted_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- Currency (M6: must equal company.base_currency) ----------------------
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[object] = mapped_column(
        _FX, nullable=False, server_default=text("1")
    )

    # -- VAT settings ---------------------------------------------------------
    tax_mode: Mapped[InvoiceTaxMode] = mapped_column(nullable=False)
    amounts_include_vat: Mapped[bool] = mapped_column(Boolean, nullable=False)
    vat_treatment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_treatment.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_rate.id", ondelete="RESTRICT"),
        nullable=True,
        comment="DOCUMENT mode only; NULL in LINE mode.",
    )

    # -- VAT treatment snapshot -----------------------------------------------
    vat_treatment_code: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_label: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_effect: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_requires_icp: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # -- Discount -------------------------------------------------------------
    discount_type: Mapped[DiscountType] = mapped_column(
        nullable=False, server_default="NONE"
    )
    discount_value: Mapped[object] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("0")
    )
    document_discount_amount: Mapped[object] = mapped_column(
        _MONEY, nullable=False, server_default=text("0")
    )

    # -- Calculated amounts (no due_amount: quotes don't have a payment dimension)
    subtotal_excl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)
    line_discount_total: Mapped[object] = mapped_column(_MONEY, nullable=False)
    taxable_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    vat_total: Mapped[object] = mapped_column(_MONEY, nullable=False)
    total_incl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)

    # -- Base-currency mirrors (M6: exchange_rate = 1) -----------------------
    base_subtotal_excl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)
    base_line_discount_total: Mapped[object] = mapped_column(_MONEY, nullable=False)
    base_taxable_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    base_vat_total: Mapped[object] = mapped_column(_MONEY, nullable=False)
    base_total_incl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)

    # -- Content block snapshot text (M6 step 4) -----------------------------
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    warranty_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    bank_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_terms_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Creator (nullable FK: preserve quote if user is deleted) -------------
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

    # -- Relationships --------------------------------------------------------
    lines: Mapped[list[QuoteLine]] = relationship(
        "QuoteLine",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="QuoteLine.sort_order",
    )
    taxes: Mapped[list[QuoteTax]] = relationship(
        "QuoteTax",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    appendix_memberships: Mapped[list[QuoteAppendix]] = relationship(
        "QuoteAppendix",
        back_populates="quote",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="QuoteAppendix.quote_id",
        # Appendix collections are loaded only by Quote read/output paths that
        # explicitly request them; ordinary Quote projections must not pay
        # extra queries for appendix metadata.
        lazy="raise",
    )
    own_appendix_files: Mapped[list[PdfAppendixFile]] = relationship(
        "PdfAppendixFile",
        back_populates="owner_quote",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="PdfAppendixFile.owner_quote_id",
        lazy="raise",
    )

    # -- Table constraints ----------------------------------------------------
    __table_args__ = (
        UniqueConstraint("company_id", "quote_number", name="uq_quote_company_number"),
        UniqueConstraint("company_id", "id", name="uq_quote_company_id"),
    )


class QuoteLine(Base):
    """Single line item within a quote."""

    __tablename__ = "quote_line"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Long-form description; text (red-line 10)."
    )
    quantity: Mapped[object] = mapped_column(_MONEY, nullable=False)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("unit.id", ondelete="SET NULL"),
        nullable=True,
    )
    unit_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    unit_price: Mapped[object] = mapped_column(_MONEY, nullable=False)
    discount_type: Mapped[DiscountType] = mapped_column(
        nullable=False, server_default="NONE"
    )
    discount_value: Mapped[object] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("0")
    )
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_rate.id", ondelete="RESTRICT"),
        nullable=True,
        comment="NULL in DOCUMENT mode; RESTRICT when set.",
    )

    vat_rate_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    vat_rate_percent: Mapped[object | None] = mapped_column(_RATE, nullable=True)

    subtotal_excl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)
    subtotal_incl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)
    line_discount_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    document_discount_share: Mapped[object] = mapped_column(_MONEY, nullable=False)
    taxable_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    vat_total: Mapped[object] = mapped_column(_MONEY, nullable=False)
    total_incl_vat: Mapped[object] = mapped_column(_MONEY, nullable=False)

    line_taxes: Mapped[list[QuoteLineTax]] = relationship(
        "QuoteLineTax",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class QuoteTax(Base):
    """Document-level tax entry (DOCUMENT mode)."""

    __tablename__ = "quote_tax"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vat_rate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_rate.id", ondelete="RESTRICT"),
        nullable=False,
    )

    vat_rate_label: Mapped[str] = mapped_column(Text, nullable=False)
    vat_rate_percent: Mapped[object] = mapped_column(_RATE, nullable=False)
    effective_vat_percent: Mapped[object] = mapped_column(_RATE, nullable=False)
    taxable_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    tax_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)


class QuoteLineTax(Base):
    """Per-line tax entry (LINE mode)."""

    __tablename__ = "quote_line_tax"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    quote_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quote_line.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vat_rate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vat_rate.id", ondelete="RESTRICT"),
        nullable=False,
    )

    vat_rate_label: Mapped[str] = mapped_column(Text, nullable=False)
    vat_rate_percent: Mapped[object] = mapped_column(_RATE, nullable=False)
    effective_vat_percent: Mapped[object] = mapped_column(_RATE, nullable=False)
    taxable_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
    tax_amount: Mapped[object] = mapped_column(_MONEY, nullable=False)
