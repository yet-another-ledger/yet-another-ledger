"""Pydantic schemas for invoice (M5 steps 1 & 3).

Step 1 schemas (pricing preview):
  DiscountInput, InvoiceLineInput, InvoiceCalculationRequest,
  InvoiceLineCalculationRead, InvoiceLineTaxRead, InvoiceTaxRead,
  VatTreatmentSnapshot, InvoiceCalculationRead

Step 3 schemas (CRUD):
  InvoiceWrite, InvoiceRead, InvoiceLineRead, InvoiceLineReadTax,
  InvoiceListResponse, InvoiceStatusWrite,
  ProductInvoiceOptionRead, ProductInvoiceOptionListResponse

All monetary fields are ``Decimal``; the pricing engine quantises to 3 dp
(ROUND_HALF_UP).  Schema layer never computes amounts (red-line 1).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jai.models._enums import (
    AdvanceInputMode,
    CreditLineInputMode,
    DiscountType,
    InvoiceCreditStatus,
    InvoiceDocumentKind,
    InvoicePaidStatus,
    InvoiceSettlementStatus,
    InvoiceStatus,
    InvoiceTaxMode,
    PartySnapshotProvenance,
)

# ---------------------------------------------------------------------------
# Discount
# ---------------------------------------------------------------------------


class DiscountInput(BaseModel):
    """Discount specification applied to a line or document."""

    type: DiscountType = DiscountType.NONE
    value: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Discount value. Ignored when type=NONE.",
    )


# ---------------------------------------------------------------------------
# Line input
# ---------------------------------------------------------------------------


class InvoiceLineInput(BaseModel):
    """Single line item in an invoice calculation or write request."""

    product_id: uuid.UUID | None = None
    name: str = Field(min_length=1)
    description: str | None = None
    quantity: Decimal = Field(gt=0, description="Must be > 0.")
    unit_id: uuid.UUID | None = None
    unit_name: str | None = None
    unit_price: Decimal = Field(
        ge=0,
        description="Per-unit price (excl or incl VAT depending on flag).",
    )
    discount: DiscountInput = DiscountInput()
    vat_rate_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("description")
    @classmethod
    def strip_description(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
        return v


# ---------------------------------------------------------------------------
# Calculation request (step 1)
# ---------------------------------------------------------------------------


class InvoiceCalculationRequest(BaseModel):
    """Request body for ``POST /api/v1/invoices/calculate``."""

    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    invoice_date: date
    due_date: date | None = None
    currency: str | None = Field(
        default=None,
        max_length=3,
        min_length=3,
        description="ISO 4217. Must equal company base_currency in M5.",
    )
    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool = False
    vat_treatment_id: uuid.UUID | None = None
    document_vat_rate_id: uuid.UUID | None = None
    discount: DiscountInput = DiscountInput()
    lines: list[InvoiceLineInput] = Field(min_length=1, description="At least 1 line required.")


class AdvanceCalculationRequest(BaseModel):
    """Frozen M12 Advance intent; accepted only by the dedicated future route."""

    model_config = ConfigDict(extra="forbid")

    input_mode: AdvanceInputMode
    gross_amount: Decimal | None = Field(default=None, gt=0)
    percentage: Decimal | None = Field(
        default=None,
        gt=0,
        le=100,
        description="Percentage of the original accepted Quote gross; at most 3 decimal places.",
    )

    @model_validator(mode="after")
    def validate_mode_payload(self) -> AdvanceCalculationRequest:
        if (
            self.input_mode == AdvanceInputMode.GROSS_AMOUNT
            and self.gross_amount is not None
            and self.percentage is None
        ):
            return self
        if (
            self.input_mode == AdvanceInputMode.PERCENTAGE
            and self.percentage is not None
            and self.gross_amount is None
        ):
            return self
        raise ValueError("Advance input_mode requires exactly its matching amount field.")


class AdvanceCalculationRead(BaseModel):
    """Authoritative, side-effect-free Formal Advance allocation."""

    input_mode: AdvanceInputMode
    requested_gross_amount: Decimal
    original_quote_gross_amount: Decimal
    remaining_capacity: Decimal
    taxable_amount: Decimal
    vat_total: Decimal
    gross_amount: Decimal
    buckets: list[AdvanceTaxBucketRead]


class AdvanceTaxBucketRead(BaseModel):
    """One persisted VAT bucket selected from accepted Quote snapshots."""

    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    taxable_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal


class AdvanceDraftCreate(AdvanceCalculationRequest):
    """Create the one open Formal Advance draft for an accepted Quote."""

    invoice_date: date
    due_date: date | None = Field(
        default=None,
        description="Optional due date. When supplied it must not precede invoice_date.",
    )
    supply_or_advance_date: date | None = None
    reference_number: str | None = None


class AdvanceDraftUpdate(AdvanceCalculationRequest):
    """Replace a DRAFT Advance's immutable-snapshot allocation intent."""

    invoice_date: date
    due_date: date | None = Field(
        default=None,
        description="Optional due date. When supplied it must not precede invoice_date.",
    )
    supply_or_advance_date: date | None = None
    reference_number: str | None = None


class FinalDraftCreate(BaseModel):
    """Create the one editable Final from accepted Quote snapshots."""

    model_config = ConfigDict(extra="forbid")

    invoice_date: date
    due_date: date | None = None
    supply_or_advance_date: date | None = None
    reference_number: str | None = None


class FinalAdvanceApplicationTaxRead(BaseModel):
    source_vat_rate_id: uuid.UUID
    source_vat_rate_label: str
    source_vat_rate_percent: Decimal
    taxable_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_taxable_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal


class FinalAdvanceApplicationRead(BaseModel):
    advance_invoice_id: uuid.UUID
    advance_invoice_number: str
    advance_invoice_date: date
    sort_order: int
    taxable_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_taxable_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal
    taxes: list[FinalAdvanceApplicationTaxRead] = []


class FinalTotalsRead(BaseModel):
    taxable_amount: Decimal
    vat_total: Decimal
    gross_amount: Decimal


class FinalVarianceRead(BaseModel):
    taxable_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal


class CreditCalculationLineInput(BaseModel):
    """One source-basis selection for the dedicated future Credit calculator."""

    model_config = ConfigDict(extra="forbid")

    source_basis_line_id: uuid.UUID
    input_mode: CreditLineInputMode
    quantity: Decimal | None = Field(default=None, gt=0, decimal_places=3)
    gross_amount: Decimal | None = Field(default=None, gt=0, multiple_of=0.01)

    @model_validator(mode="after")
    def validate_mode_payload(self) -> CreditCalculationLineInput:
        if (
            self.input_mode == CreditLineInputMode.QUANTITY
            and self.quantity is not None
            and self.gross_amount is None
        ):
            return self
        if (
            self.input_mode == CreditLineInputMode.GROSS_AMOUNT
            and self.gross_amount is not None
            and self.quantity is None
        ):
            return self
        raise ValueError("Credit input_mode requires exactly its matching amount field.")


class CreditCalculationRequest(BaseModel):
    """Frozen M12 Credit intent; generic Standard pricing never accepts it."""

    model_config = ConfigDict(extra="forbid")

    full_remaining: bool = False
    lines: list[CreditCalculationLineInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selection(self) -> CreditCalculationRequest:
        if self.full_remaining != bool(self.lines):
            if len({line.source_basis_line_id for line in self.lines}) == len(self.lines):
                return self
            raise ValueError("Each source basis line may be selected only once.")
        raise ValueError("Select exactly one of full_remaining=true or non-empty lines.")


class CreditCalculationRead(BaseModel):
    """Authoritative source-basis allocation for a Credit DRAFT."""

    source_invoice_id: uuid.UUID
    remaining_gross_amount: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_net_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal
    lines: list[CreditCalculationLineRead]


class CreditCalculationLineRead(BaseModel):
    source_basis_line_id: uuid.UUID
    source_invoice_line_id: uuid.UUID
    name: str
    description: str | None = None
    quantity: Decimal
    unit_name: str | None = None
    vat_rate_id: uuid.UUID | None = None
    vat_rate_label: str | None = None
    vat_rate_percent: Decimal | None = None
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_net_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal


class CreditDraftIntentLineRead(BaseModel):
    """Persisted user selection provenance for editing a Credit DRAFT."""

    source_basis_line_id: uuid.UUID
    input_mode: CreditLineInputMode
    quantity: Decimal | None = None
    gross_amount: Decimal | None = None


class CreditDraftIntentRead(BaseModel):
    """Exact persisted Credit DRAFT input, including its selection mode."""

    full_remaining: bool | None
    requires_confirmation: bool = False
    provenance: Literal["NATIVE", "MIGRATED_AMBIGUOUS"] = "NATIVE"
    lines: list[CreditDraftIntentLineRead] = []


class CreditDraftCreate(CreditCalculationRequest):
    model_config = ConfigDict(extra="forbid")

    invoice_date: date
    due_date: date | None = None
    supply_or_advance_date: date | None = None
    reference_number: str | None = None


class CreditDraftUpdate(CreditCalculationRequest):
    model_config = ConfigDict(extra="forbid")

    invoice_date: date
    due_date: date | None = None
    supply_or_advance_date: date | None = None
    reference_number: str | None = None


class ProjectCancellationRequest(BaseModel):
    """Intent shared by formal-project cancellation preview and confirmation."""

    model_config = ConfigDict(extra="forbid")

    invoice_date: date | None = Field(
        default=None,
        description="Credit date; defaults to the server's current local date.",
    )
    supply_or_advance_date: date | None = None
    reference_number: str | None = None


class ProjectCancellationCreateRequest(ProjectCancellationRequest):
    """Confirmation of one exact previewed formal-chain state."""

    preview_token: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class ProjectCancellationSourceRead(BaseModel):
    source_invoice_id: uuid.UUID
    source_invoice_number: str
    document_kind: InvoiceDocumentKind
    remaining_net_amount: Decimal
    remaining_vat_amount: Decimal
    remaining_gross_amount: Decimal
    remaining_base_net_amount: Decimal
    remaining_base_vat_amount: Decimal
    remaining_base_gross_amount: Decimal


class ProjectCancellationPreview(BaseModel):
    quote_id: uuid.UUID
    preview_token: str
    invoice_date: date
    sources: list[ProjectCancellationSourceRead]
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_net_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal


# ---------------------------------------------------------------------------
# VatTreatment snapshot
# ---------------------------------------------------------------------------


class VatTreatmentSnapshot(BaseModel):
    """Snapshot of the VAT treatment applied to this calculation/invoice."""

    id: uuid.UUID
    code: str
    label: str
    effect: str
    requires_icp: bool


# ---------------------------------------------------------------------------
# Tax read schemas
# ---------------------------------------------------------------------------


class InvoiceLineTaxRead(BaseModel):
    """Per-line tax breakdown (LINE mode)."""

    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


class InvoiceTaxRead(BaseModel):
    """Per-document tax breakdown (DOCUMENT mode)."""

    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


# ---------------------------------------------------------------------------
# Line calculation result (step 1)
# ---------------------------------------------------------------------------


class InvoiceLineCalculationRead(BaseModel):
    """Per-line calculation result returned by the pricing engine."""

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


# ---------------------------------------------------------------------------
# Full calculation response (step 1)
# ---------------------------------------------------------------------------


class InvoiceCalculationRead(BaseModel):
    """Response body for ``POST /api/v1/invoices/calculate``."""

    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool
    discount_type: DiscountType
    discount_value: Decimal
    vat_treatment_snapshot: VatTreatmentSnapshot | None = None
    subtotal_excl_vat: Decimal
    line_discount_total: Decimal
    document_discount_amount: Decimal
    taxable_amount: Decimal
    vat_total: Decimal
    total_incl_vat: Decimal
    lines: list[InvoiceLineCalculationRead]
    line_taxes: list[InvoiceLineTaxRead]
    document_taxes: list[InvoiceTaxRead]


# ---------------------------------------------------------------------------
# Invoice write (step 3)
# ---------------------------------------------------------------------------


class InvoiceWrite(BaseModel):
    """Request body for ``POST /PUT /api/v1/invoices``."""

    # The generic commands remain Standard-only in Step 1.  Rejecting extra
    # fields prevents an M12 document intent from being silently treated as a
    # normal invoice command while the dedicated engines are still deferred.
    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    reference_number: str | None = None
    invoice_date: date
    due_date: date | None = None
    supply_or_advance_date: date | None = Field(
        default=None,
        description=(
            "Optional supply/prepayment date for a draft; defaults to invoice_date on issue."
        ),
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        min_length=3,
        description="Must equal company base_currency in M5.",
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


# ---------------------------------------------------------------------------
# Invoice line read (step 3)
# ---------------------------------------------------------------------------


class InvoiceLineReadTax(BaseModel):
    """Per-line tax read (for InvoiceLineRead)."""

    id: uuid.UUID
    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


class InvoiceLineRead(BaseModel):
    """Invoice line as returned in InvoiceRead.

    Never exposes product.purchase_cost / margin / supplier / extra (red-line 7).
    """

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
    line_taxes: list[InvoiceLineReadTax] = []


class InvoiceTaxRowRead(BaseModel):
    """Document-level tax row as returned in InvoiceRead."""

    id: uuid.UUID
    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


# ---------------------------------------------------------------------------
# Invoice read (step 3)
# ---------------------------------------------------------------------------


class InvoiceRevisionEligibilityRead(BaseModel):
    available: bool
    reason_code: str | None = None


class InvoiceRead(BaseModel):
    """Full invoice representation returned by CRUD endpoints."""

    id: uuid.UUID
    company_id: uuid.UUID
    customer_id: uuid.UUID

    invoice_number: str | None = None
    sequence_number: int | None = None
    customer_sequence_number: int | None = None
    unique_hash: str | None = None
    reference_number: str | None = None

    invoice_date: date
    due_date: date | None = None
    supply_or_advance_date: date | None = None

    status: InvoiceStatus
    paid_status: InvoicePaidStatus
    document_kind: InvoiceDocumentKind = InvoiceDocumentKind.STANDARD
    quote_id: uuid.UUID | None = None
    issued_at: datetime | None = None
    issued_by_user_id: uuid.UUID | None = None
    party_snapshot_provenance: PartySnapshotProvenance | None = None
    party_snapshot_locale: Literal["en", "zh"] | None = None
    party_snapshot_customer_email: str | None = None
    source_invoice_id: uuid.UUID | None = None
    credit_draft_intent: CreditDraftIntentRead | None = None
    advance_input_mode: AdvanceInputMode | None = None
    advance_gross_amount: Decimal | None = None
    advance_percentage: Decimal | None = None
    replacement_of_credit_note_id: uuid.UUID | None = None
    compensates_credit_note_id: uuid.UUID | None = None
    original_quote_totals: FinalTotalsRead | None = None
    final_totals: FinalTotalsRead | None = None
    final_variance: FinalVarianceRead | None = None
    final_advance_applications: list[FinalAdvanceApplicationRead] = []

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
    due_amount: Decimal
    payable_before_payments: Decimal
    incoming_payment_total: Decimal
    credited_total: Decimal
    refunded_total: Decimal
    refund_due_amount: Decimal
    settlement_status: InvoiceSettlementStatus
    credit_status: InvoiceCreditStatus

    base_subtotal_excl_vat: Decimal
    base_line_discount_total: Decimal
    base_taxable_amount: Decimal
    base_vat_total: Decimal
    base_total_incl_vat: Decimal
    base_due_amount: Decimal
    base_payable_before_payments: Decimal
    base_incoming_payment_total: Decimal
    base_credited_total: Decimal
    base_refunded_total: Decimal
    base_refund_due_amount: Decimal

    notes: str | None = None
    warranty_text: str | None = None
    terms_text: str | None = None
    bank_text: str | None = None
    payment_terms_text: str | None = None
    creator_id: uuid.UUID | None = None

    lines: list[InvoiceLineRead] = []
    taxes: list[InvoiceTaxRowRead] = []

    created_at: datetime
    updated_at: datetime
    revision_eligibility: InvoiceRevisionEligibilityRead | None = None


class ProjectCancellationResult(BaseModel):
    quote_id: uuid.UUID
    preview_token: str
    credit_notes: list[InvoiceRead]


# ---------------------------------------------------------------------------
# Invoice list (step 3)
# ---------------------------------------------------------------------------


class InvoiceListItem(BaseModel):
    """Summary row in InvoiceListResponse."""

    id: uuid.UUID
    company_id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str
    invoice_number: str | None = None
    reference_number: str | None = None
    invoice_date: date
    due_date: date | None = None
    status: InvoiceStatus
    paid_status: InvoicePaidStatus
    document_kind: InvoiceDocumentKind = InvoiceDocumentKind.STANDARD
    quote_id: uuid.UUID | None = None
    supply_or_advance_date: date | None = None
    issued_at: datetime | None = None
    issued_by_user_id: uuid.UUID | None = None
    party_snapshot_provenance: PartySnapshotProvenance | None = None
    party_snapshot_locale: Literal["en", "zh"] | None = None
    source_invoice_id: uuid.UUID | None = None
    replacement_of_credit_note_id: uuid.UUID | None = None
    compensates_credit_note_id: uuid.UUID | None = None
    settlement_status: InvoiceSettlementStatus
    credit_status: InvoiceCreditStatus
    currency: str
    total_incl_vat: Decimal
    payable_before_payments: Decimal
    incoming_payment_total: Decimal
    credited_total: Decimal
    refunded_total: Decimal
    due_amount: Decimal
    refund_due_amount: Decimal
    base_total_incl_vat: Decimal
    base_payable_before_payments: Decimal
    base_incoming_payment_total: Decimal
    base_credited_total: Decimal
    base_refunded_total: Decimal
    base_due_amount: Decimal
    base_refund_due_amount: Decimal
    vat_treatment_snapshot: VatTreatmentSnapshot
    created_at: datetime
    updated_at: datetime


class InvoiceListResponse(BaseModel):
    """Paginated invoice list."""

    items: list[InvoiceListItem]
    total: int


# ---------------------------------------------------------------------------
# Status transition (step 3)
# ---------------------------------------------------------------------------


class InvoiceStatusWrite(BaseModel):
    """Request body for ``POST /api/v1/invoices/{id}/status``."""

    model_config = ConfigDict(extra="forbid")

    status: InvoiceStatus


# ---------------------------------------------------------------------------
# Invoice product options (step 3) – customer-safe projection
# ---------------------------------------------------------------------------


class ProductInvoiceOptionRead(BaseModel):
    """Customer-safe product option for invoice line auto-fill.

    Never includes: purchase_cost_excl_vat, margin_rate, effective_margin_rate,
    supplier, extra (red-line 7 extension).
    """

    id: uuid.UUID
    name: str
    unit_id: uuid.UUID | None = None
    unit_name: str | None = None
    default_vat_rate_id: uuid.UUID | None = None


class ProductInvoiceOptionListResponse(BaseModel):
    """List of customer-safe product options."""

    items: list[ProductInvoiceOptionRead]
