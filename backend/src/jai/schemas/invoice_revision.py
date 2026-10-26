"""Closed, versioned contracts for Invoice revision audit and eligibility."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jai.models._enums import (
    DiscountType,
    InvoiceCreditStatus,
    InvoiceDocumentKind,
    InvoicePaidStatus,
    InvoiceSettlementStatus,
    InvoiceStatus,
    InvoiceTaxMode,
)


class InvoiceCreditBasisSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    invoice_line_id: uuid.UUID
    sort_order: int
    name: str
    description: str | None
    unit_name: str | None
    quantity: Decimal
    vat_rate_id: uuid.UUID | None
    vat_rate_label: str | None
    vat_rate_percent: Decimal | None
    effective_vat_percent: Decimal | None
    vat_treatment_code: str
    vat_treatment_effect: str
    vat_treatment_requires_icp: bool
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    base_net_amount: Decimal
    base_vat_amount: Decimal
    base_gross_amount: Decimal


class InvoiceRevisionTaxSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    vat_rate_id: uuid.UUID
    vat_rate_label: str
    vat_rate_percent: Decimal
    effective_vat_percent: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


class InvoiceRevisionLineTaxSnapshot(InvoiceRevisionTaxSnapshot):
    pass


class InvoiceRevisionLineSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    sort_order: int
    product_id: uuid.UUID | None
    name: str
    description: str | None
    quantity: Decimal
    unit_id: uuid.UUID | None
    unit_name: str | None
    unit_price: Decimal
    discount_type: DiscountType
    discount_value: Decimal
    vat_rate_id: uuid.UUID | None
    vat_rate_label: str | None
    vat_rate_percent: Decimal | None
    subtotal_excl_vat: Decimal
    subtotal_incl_vat: Decimal
    line_discount_amount: Decimal
    document_discount_share: Decimal
    taxable_amount: Decimal
    vat_total: Decimal
    total_incl_vat: Decimal
    line_taxes: list[InvoiceRevisionLineTaxSnapshot]


class InvoiceRevisionAddressSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    street: str | None
    house_number: str | None
    house_number_addition: str | None
    postal_code: str | None
    city: str | None
    province: str | None
    country_code: str | None


class InvoiceRevisionPartySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: str
    seller_name: str
    seller_legal_name: str | None
    seller_vat_id: str | None
    seller_coc_number: str | None
    seller_email: str | None
    seller_phone: str | None
    seller_address: InvoiceRevisionAddressSnapshot
    buyer_name: str
    buyer_company_name: str | None
    buyer_contact_name: str | None
    buyer_vat_id: str | None
    buyer_email: str | None
    buyer_phone: str | None
    buyer_address: InvoiceRevisionAddressSnapshot
    locale: str
    logo_id: uuid.UUID | None


class InvoiceRevisionDocumentSnapshot(BaseModel):
    """Frozen business fields, independent from the evolving API read model."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    company_id: uuid.UUID
    customer_id: uuid.UUID
    invoice_number: str | None
    sequence_number: int | None
    customer_sequence_number: int | None
    reference_number: str | None
    invoice_date: str
    due_date: str | None
    supply_or_advance_date: str | None
    status: InvoiceStatus
    paid_status: InvoicePaidStatus
    document_kind: InvoiceDocumentKind
    quote_id: uuid.UUID | None
    issued_at: datetime | None
    issued_by_user_id: uuid.UUID | None
    currency: str
    exchange_rate: Decimal
    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool
    vat_treatment_id: uuid.UUID
    document_vat_rate_id: uuid.UUID | None
    vat_treatment_code: str
    vat_treatment_label: str
    vat_treatment_effect: str
    vat_treatment_requires_icp: bool
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
    notes: str | None
    warranty_text: str | None
    terms_text: str | None
    bank_text: str | None
    payment_terms_text: str | None
    creator_id: uuid.UUID | None
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
    party_snapshot: InvoiceRevisionPartySnapshot | None
    lines: list[InvoiceRevisionLineSnapshot]
    taxes: list[InvoiceRevisionTaxSnapshot]


class InvoiceBusinessSnapshot(BaseModel):
    """Complete snapshot version 1; no ORM, request envelope, or free-form context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    invoice: InvoiceRevisionDocumentSnapshot
    credit_basis: list[InvoiceCreditBasisSnapshot]


class InvoiceRevisionRead(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    revision_order: int
    actor_user_id: uuid.UUID | None
    created_at: datetime
    reason: str
    undelivered_confirmed: bool
    before_snapshot: InvoiceBusinessSnapshot
    after_snapshot: InvoiceBusinessSnapshot


class InvoiceRevisionListRead(BaseModel):
    items: list[InvoiceRevisionRead]
    total: int
    offset: int = 0
    limit: int = 50
