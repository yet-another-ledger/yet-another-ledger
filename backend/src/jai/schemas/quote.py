"""Pydantic schemas for quote (M6 steps 1 & 2).

Step 1 schemas (pricing preview):
  QuoteCalculationRequest – mirrors InvoiceCalculationRequest with quote_date
  QuoteCalculationRead    – proper Pydantic subclass (identical fields, own OpenAPI schema)

Step 2 schemas (CRUD):
  QuoteWrite, QuoteRead, QuoteLineRead, QuoteLineReadTax,
  QuoteTaxRowRead, QuoteListItem, QuoteListResponse, QuoteStatusWrite

Line/discount inputs reuse invoice schema types directly (same model, red-line 1).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from jai.models._enums import DiscountType, InvoiceTaxMode, QuoteSettlementMode, QuoteStatus
from jai.schemas.invoice import (
    DiscountInput,
    InvoiceCalculationRead,
    InvoiceLineInput,
    VatTreatmentSnapshot,
)
from jai.schemas.pdf_appendix import QuoteAppendixRead

# ---------------------------------------------------------------------------
# Calculation request (step 1)
# ---------------------------------------------------------------------------


class QuoteCalculationRequest(BaseModel):
    """Request body for ``POST /api/v1/quotes/calculate``."""

    customer_id: uuid.UUID
    quote_date: date
    valid_until: date | None = None
    currency: str | None = Field(
        default=None,
        max_length=3,
        min_length=3,
        description="ISO 4217. Must equal company base_currency in M6.",
    )
    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool = False
    vat_treatment_id: uuid.UUID | None = None
    document_vat_rate_id: uuid.UUID | None = None
    discount: DiscountInput = DiscountInput()
    lines: list[InvoiceLineInput] = Field(min_length=1, description="At least 1 line required.")


# ---------------------------------------------------------------------------
# Calculation response (step 1)
# ---------------------------------------------------------------------------


class QuoteCalculationRead(InvoiceCalculationRead):
    """Quote pricing preview result.

    Identical field structure to ``InvoiceCalculationRead``; defined as its own
    Pydantic model so FastAPI emits a distinct ``QuoteCalculationRead`` component
    in OpenAPI and ``schema.d.ts`` carries the quote-specific name.
    """


# ---------------------------------------------------------------------------
# Quote write (step 2)
# ---------------------------------------------------------------------------


class QuoteWrite(BaseModel):
    """Request body for POST / PUT /api/v1/quotes."""

    customer_id: uuid.UUID
    reference_number: str | None = None
    quote_date: date
    valid_until: date | None = None
    currency: str | None = Field(
        default=None,
        max_length=3,
        min_length=3,
        description="Must equal company base_currency in M6.",
    )
    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool = False
    vat_treatment_id: uuid.UUID | None = None
    document_vat_rate_id: uuid.UUID | None = None
    discount: DiscountInput = DiscountInput()
    notes: str | None = None
    warranty_text: str | None = None
    terms_text: str | None = None
    bank_text: str | None = None
    payment_terms_text: str | None = None
    lines: list[InvoiceLineInput] = Field(min_length=1, description="At least 1 line required.")
    appendix_file_ids: list[uuid.UUID] = Field(default=None)  # type: ignore[arg-type]
    expected_appendix_version: int = Field(default=None, ge=0)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Quote line read (step 2)
# ---------------------------------------------------------------------------


class QuoteLineReadTax(BaseModel):
    """Per-line tax read (for QuoteLineRead)."""

    id: uuid.UUID
    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


class QuoteLineRead(BaseModel):
    """Quote line as returned in QuoteRead."""

    id: uuid.UUID
    sort_order: int
    product_id: uuid.UUID | None = None
    name: str
    description: str | None = None
    quantity: Decimal
    unit_id: uuid.UUID | None = None
    unit_name: str | None = None
    unit_price: Decimal
    discount_type: DiscountType
    discount_value: Decimal
    vat_rate_id: uuid.UUID | None = None
    vat_rate_label: str | None = None
    vat_rate_percent: Decimal | None = None
    subtotal_excl_vat: Decimal
    subtotal_incl_vat: Decimal
    line_discount_amount: Decimal
    document_discount_share: Decimal
    taxable_amount: Decimal
    vat_total: Decimal
    total_incl_vat: Decimal
    line_taxes: list[QuoteLineReadTax] = []


class QuoteTaxRowRead(BaseModel):
    """Document-level tax row as returned in QuoteRead."""

    id: uuid.UUID
    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


class DocumentChainTotals(BaseModel):
    """Compact, backend-authoritative settlement projection for one Quote chain."""

    charge_total: Decimal = Decimal("0")
    credit_total: Decimal = Decimal("0")
    incoming_payment_total: Decimal = Decimal("0")
    refund_total: Decimal = Decimal("0")
    application_total: Decimal = Decimal("0")
    due_amount: Decimal = Decimal("0")
    refund_due_amount: Decimal = Decimal("0")
    base_charge_total: Decimal = Decimal("0")
    base_credit_total: Decimal = Decimal("0")
    base_incoming_payment_total: Decimal = Decimal("0")
    base_refund_total: Decimal = Decimal("0")
    base_application_total: Decimal = Decimal("0")
    base_due_amount: Decimal = Decimal("0")
    base_refund_due_amount: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# Quote read (step 2)
# ---------------------------------------------------------------------------


class QuoteRead(BaseModel):
    """Full quote representation returned by CRUD endpoints."""

    id: uuid.UUID
    company_id: uuid.UUID
    customer_id: uuid.UUID

    quote_number: str
    sequence_number: int
    customer_sequence_number: int | None = None
    unique_hash: str | None = None
    reference_number: str | None = None

    quote_date: date
    valid_until: date | None = None

    status: QuoteStatus
    converted_invoice_id: uuid.UUID | None = None
    settlement_mode: QuoteSettlementMode = QuoteSettlementMode.UNSET
    settlement_mode_locked_at: datetime | None = None
    chain_totals: DocumentChainTotals = DocumentChainTotals()
    # M11.5 compatibility aliases. New clients must use chain_totals.
    incoming_payment_total: Decimal = Decimal("0")
    remaining_amount: Decimal = Decimal("0")

    currency: str
    exchange_rate: Decimal

    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool
    vat_treatment_id: uuid.UUID
    document_vat_rate_id: uuid.UUID | None = None

    vat_treatment_snapshot: VatTreatmentSnapshot

    discount_type: DiscountType
    discount_value: Decimal
    document_discount_amount: Decimal

    subtotal_excl_vat: Decimal
    line_discount_total: Decimal
    taxable_amount: Decimal
    vat_total: Decimal
    total_incl_vat: Decimal

    base_subtotal_excl_vat: Decimal
    base_line_discount_total: Decimal
    base_taxable_amount: Decimal
    base_vat_total: Decimal
    base_total_incl_vat: Decimal
    base_incoming_payment_total: Decimal = Decimal("0")
    base_remaining_amount: Decimal = Decimal("0")

    notes: str | None = None
    warranty_text: str | None = None
    terms_text: str | None = None
    bank_text: str | None = None
    payment_terms_text: str | None = None
    creator_id: uuid.UUID | None = None
    appendices: list[QuoteAppendixRead] = Field(default_factory=list)
    appendix_version: int = 0

    lines: list[QuoteLineRead] = []
    taxes: list[QuoteTaxRowRead] = []

    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Quote list (step 2)
# ---------------------------------------------------------------------------


class QuoteListItem(BaseModel):
    """Summary row in QuoteListResponse."""

    id: uuid.UUID
    company_id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str
    quote_number: str
    reference_number: str | None = None
    quote_date: date
    valid_until: date | None = None
    status: QuoteStatus
    converted_invoice_id: uuid.UUID | None = None
    settlement_mode: QuoteSettlementMode = QuoteSettlementMode.UNSET
    currency: str
    total_incl_vat: Decimal
    vat_treatment_snapshot: VatTreatmentSnapshot
    created_at: datetime
    updated_at: datetime


class QuoteListResponse(BaseModel):
    """Paginated quote list."""

    items: list[QuoteListItem]
    total: int


# ---------------------------------------------------------------------------
# Status transition (step 2)
# ---------------------------------------------------------------------------


class QuoteStatusWrite(BaseModel):
    """Request body for ``POST /api/v1/quotes/{id}/status``."""

    status: QuoteStatus


# ---------------------------------------------------------------------------
# Reactivate (step 3)
# ---------------------------------------------------------------------------


class QuoteReactivateWrite(BaseModel):
    """Request body for ``POST /api/v1/quotes/{id}/reactivate``."""

    valid_until: date | None = None


# Re-export shared input types for consumers that import from schemas.quote
__all__ = [
    "DiscountInput",
    "InvoiceLineInput",
    "QuoteCalculationRead",
    "QuoteCalculationRequest",
    "DocumentChainTotals",
    "QuoteLineRead",
    "QuoteLineReadTax",
    "QuoteListItem",
    "QuoteListResponse",
    "QuoteRead",
    "QuoteReactivateWrite",
    "QuoteStatusWrite",
    "QuoteTaxRowRead",
    "QuoteWrite",
]
