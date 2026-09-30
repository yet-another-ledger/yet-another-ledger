"""Invoice service – CRUD + status transitions + product options (M5 step 3).

Public API
----------
- ``create_invoice``          – calculate + persist an unnumbered DRAFT (within transaction)
- ``update_invoice``          – recalculate + replace sub-tables (preserves number)
- ``delete_invoice``          – DB cascade removes lines/taxes; number not recycled
- ``get_invoice``             – fetch by id + company_id (returns None if not found)
- ``list_invoices``           – filtered/paginated list
- ``transition_status``       – M5 lifecycle state machine; allocates the number
                                on DRAFT -> SENT (issue time)
- ``list_invoice_product_options`` – customer-safe product projection

Red-line compliance
-------------------
1. All money calculation is delegated to ``services.pricing``; this module only
   persists results.
2. company_id is always injected by the service; front-end never provides it.
3. No manual cascade deletes – DB ON DELETE CASCADE handles sub-tables.
4. Numbering via ``services.numbering.allocate_invoice_number`` (row-locked).
"""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import (
    AdvanceInputMode,
    DiscountType,
    DocumentChainEventType,
    InvoiceCreditStatus,
    InvoiceDocumentKind,
    InvoicePaidStatus,
    InvoiceRelationType,
    InvoiceSettlementStatus,
    InvoiceStatus,
    InvoiceTaxMode,
    PartySnapshotProvenance,
    SettingLevel,
    VatTreatmentEffect,
    VatTreatmentSide,
)
from jai.models.address import Address
from jai.models.company import Company
from jai.models.customer import Customer
from jai.models.dictionary import Unit
from jai.models.document import (
    FinalAdvanceApplication,
    FinalAdvanceApplicationTax,
    InvoiceCorrection,
    InvoiceCreditBasisLine,
    InvoicePartySnapshot,
    InvoiceRelation,
)
from jai.models.invoice import Invoice, InvoiceLine, InvoiceLineTax, InvoiceTax
from jai.models.payment import Payment
from jai.models.product import Product
from jai.models.quote import Quote as _Quote
from jai.models.quote import QuoteLine as _QuoteLine
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.invoice import (
    CreditDraftIntentLineRead,
    CreditDraftIntentRead,
    FinalAdvanceApplicationRead,
    FinalAdvanceApplicationTaxRead,
    FinalTotalsRead,
    FinalVarianceRead,
    InvoiceLineRead,
    InvoiceLineReadTax,
    InvoiceListItem,
    InvoiceListResponse,
    InvoiceRead,
    InvoiceStatusWrite,
    InvoiceTaxRowRead,
    InvoiceWrite,
    ProductInvoiceOptionListResponse,
    ProductInvoiceOptionRead,
    VatTreatmentSnapshot,
)
from jai.schemas.setting import (
    SETTING_KEY_DOCUMENT_DEFAULTS,
    SETTING_KEY_INVOICE_NUMBERING,
    DocumentDefaultsSetting,
    InvoiceNumberingConfig,
)
from jai.services.money import quantize_to_minor_unit
from jai.services.numbering import NumberSequenceExhaustedError, allocate_invoice_number
from jai.services.payment import (
    _settle_refund_chain,
    _write_invoice_state,
    lock_settlement_chain,
    recompute_payment_state,
    validate_invoice_tax_coverage,
)
from jai.services.pricing import (
    _derive_treatment_from_customer,
    compute_pricing,
)
from jai.services.settings import get_setting

# ---------------------------------------------------------------------------
# Allowed status transitions for M5
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict[InvoiceStatus, set[InvoiceStatus]] = {
    InvoiceStatus.DRAFT: {InvoiceStatus.SENT, InvoiceStatus.CANCELLED},
    # Once sent, the invoice is a formal document; cancellation must be done via
    # a credit note (M9/M10).  No direct SENT→CANCELLED transition.
    InvoiceStatus.SENT: set(),
    # COMPLETED is driven by payments (M7); cannot be set manually in M5.
    InvoiceStatus.COMPLETED: set(),
    # Only DRAFT invoices can be cancelled, so reactivating back to DRAFT is safe.
    InvoiceStatus.CANCELLED: {InvoiceStatus.DRAFT},
}


class InvoiceLifecycleConflictError(ValueError):
    """A stale or forbidden formal-document lifecycle command.

    The API must distinguish a command which is structurally invalid (422)
    from a valid lifecycle command that conflicts with the document's current
    state (409).  Keep the code independent of localized display text.
    """

    code = "INVOICE_LIFECYCLE_CONFLICT"


class FinalLifecycleConflictError(InvoiceLifecycleConflictError):
    """Final drafts can only release their Advance freeze by deletion."""

    code = "FINAL_DRAFT_LIFECYCLE_FORBIDDEN"


class InvoiceDedicatedUpdateRequiredError(ValueError):
    """Tell callers to use a document-kind-specific draft command."""

    code = "ADVANCE_DEDICATED_UPDATE_REQUIRED"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_numbering_config(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> InvoiceNumberingConfig:
    """Read InvoiceNumberingConfig from COMPANY-level settings, falling back to default."""
    config = await get_setting(
        session,
        SETTING_KEY_INVOICE_NUMBERING,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=InvoiceNumberingConfig,
    )
    return config or InvoiceNumberingConfig()


async def _resolve_treatment(
    session: AsyncSession,
    company_id: uuid.UUID,
    customer: Customer,
    vat_treatment_id: uuid.UUID | None,
) -> VatTreatment:
    """Resolve or derive the VAT treatment; raises ValueError on failure."""
    if vat_treatment_id is not None:
        stmt = select(VatTreatment).where(
            VatTreatment.id == vat_treatment_id,
            VatTreatment.company_id == company_id,
            VatTreatment.side == VatTreatmentSide.SALES,
            VatTreatment.active == True,  # noqa: E712
        )
        result = await session.execute(stmt)
        treatment = result.scalar_one_or_none()
        if treatment is None:
            raise ValueError("VAT treatment not found, inactive, or not a sales treatment.")
        return treatment

    treatment = await _derive_treatment_from_customer(session, company_id, customer)
    if treatment is None:
        raise ValueError(
            "Could not derive a default VAT treatment for this customer. "
            "Please select one manually."
        )
    return treatment


async def _load_vat_rates(
    session: AsyncSession,
    company_id: uuid.UUID,
    body: InvoiceWrite,
) -> dict[uuid.UUID, tuple[str, Decimal]]:
    """Batch-load VAT rates needed for the request; validate ownership.

    LINE mode:   every line vat_rate_id is required and drives calculation.
    DOCUMENT mode: document_vat_rate_id drives calculation; line-level vat_rate_ids
                   are optional draft-defaults that are persisted but not used in
                   calculation – they must still belong to this company.
    """
    required_ids: set[uuid.UUID] = set()
    optional_ids: set[uuid.UUID] = set()

    if body.tax_mode == InvoiceTaxMode.LINE:
        for line in body.lines:
            if line.vat_rate_id is None:
                raise ValueError(f"Line '{line.name}': vat_rate_id is required in LINE tax mode.")
            required_ids.add(line.vat_rate_id)
    else:
        if body.document_vat_rate_id is None:
            raise ValueError("document_vat_rate_id is required in DOCUMENT tax mode.")
        required_ids.add(body.document_vat_rate_id)
        # Collect optional per-line rates (draft defaults); validate ownership if present.
        for line in body.lines:
            if line.vat_rate_id is not None:
                optional_ids.add(line.vat_rate_id)

    all_ids = required_ids | optional_ids
    stmt = select(VatRate).where(
        VatRate.id.in_(all_ids),
        VatRate.company_id == company_id,
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    vat_rates: dict[uuid.UUID, tuple[str, Decimal]] = {
        r.id: (r.label, Decimal(str(r.percent))) for r in rows
    }

    # Every ID (required and optional) must be found within this company.
    for rate_id in all_ids:
        if rate_id not in vat_rates:
            raise ValueError(f"VAT rate {rate_id} not found or not in this company.")

    return vat_rates


async def _validate_line_fks(
    session: AsyncSession,
    company_id: uuid.UUID,
    body: InvoiceWrite,
) -> None:
    """Validate all line-level optional FKs (product_id, unit_id) belong to this company.

    Raises ValueError when any referenced ID is absent or belongs to another company.
    """
    product_ids = {line.product_id for line in body.lines if line.product_id is not None}
    unit_ids = {line.unit_id for line in body.lines if line.unit_id is not None}

    if product_ids:
        count_stmt = select(func.count()).where(
            Product.id.in_(product_ids),
            Product.company_id == company_id,
        )
        found = (await session.execute(count_stmt)).scalar_one()
        if found != len(product_ids):
            raise ValueError("One or more product IDs not found or do not belong to this company.")

    if unit_ids:
        count_stmt = select(func.count()).where(
            Unit.id.in_(unit_ids),
            Unit.company_id == company_id,
        )
        found = (await session.execute(count_stmt)).scalar_one()
        if found != len(unit_ids):
            raise ValueError("One or more unit IDs not found or do not belong to this company.")


def _line_to_read(line: InvoiceLine) -> InvoiceLineRead:
    return InvoiceLineRead(
        id=line.id,
        sort_order=line.sort_order,
        product_id=line.product_id,
        name=line.name,
        description=line.description,
        quantity=Decimal(str(line.quantity)),
        unit_id=line.unit_id,
        unit_name=line.unit_name,
        unit_price=Decimal(str(line.unit_price)),
        discount_type=DiscountType(line.discount_type),
        discount_value=Decimal(str(line.discount_value)),
        vat_rate_id=line.vat_rate_id,
        vat_rate_label=line.vat_rate_label,
        vat_rate_percent=(
            Decimal(str(line.vat_rate_percent)) if line.vat_rate_percent is not None else None
        ),
        subtotal_excl_vat=Decimal(str(line.subtotal_excl_vat)),
        subtotal_incl_vat=Decimal(str(line.subtotal_incl_vat)),
        line_discount_amount=Decimal(str(line.line_discount_amount)),
        document_discount_share=Decimal(str(line.document_discount_share)),
        taxable_amount=Decimal(str(line.taxable_amount)),
        vat_total=Decimal(str(line.vat_total)),
        total_incl_vat=Decimal(str(line.total_incl_vat)),
        line_taxes=[
            InvoiceLineReadTax(
                id=lt.id,
                vat_rate_id=lt.vat_rate_id,
                vat_rate_label=lt.vat_rate_label,
                vat_rate_percent=Decimal(str(lt.vat_rate_percent)),
                effective_vat_percent=Decimal(str(lt.effective_vat_percent)),
                taxable_amount=Decimal(str(lt.taxable_amount)),
                tax_amount=Decimal(str(lt.tax_amount)),
            )
            for lt in line.line_taxes
        ],
    )


def _invoice_to_read(inv: Invoice) -> InvoiceRead:
    treatment_snapshot = VatTreatmentSnapshot(
        id=inv.vat_treatment_id,
        code=inv.vat_treatment_code,
        label=inv.vat_treatment_label,
        effect=inv.vat_treatment_effect,
        requires_icp=inv.vat_treatment_requires_icp,
    )
    original_gross = (
        Decimal(str(inv.final_original_gross_amount))
        if inv.final_original_gross_amount is not None
        else None
    )
    positive_relations = inv.__dict__.get("positive_relations") or []
    replacement_relation = next(
        (
            relation
            for relation in positive_relations
            if InvoiceRelationType(relation.relation_type) == InvoiceRelationType.REPLACEMENT_OF
        ),
        None,
    )
    compensation_relation = next(
        (
            relation
            for relation in positive_relations
            if InvoiceRelationType(relation.relation_type) == InvoiceRelationType.COMPENSATES_CREDIT
        ),
        None,
    )
    return InvoiceRead(
        id=inv.id,
        company_id=inv.company_id,
        customer_id=inv.customer_id,
        invoice_number=inv.invoice_number,
        sequence_number=inv.sequence_number,
        customer_sequence_number=inv.customer_sequence_number,
        unique_hash=inv.unique_hash,
        reference_number=inv.reference_number,
        invoice_date=inv.invoice_date,
        due_date=inv.due_date,
        supply_or_advance_date=inv.supply_or_advance_date,
        status=InvoiceStatus(inv.status),
        paid_status=InvoicePaidStatus(inv.paid_status),
        document_kind=InvoiceDocumentKind(inv.document_kind),
        quote_id=inv.quote_id,
        issued_at=inv.issued_at,
        issued_by_user_id=inv.issued_by_user_id,
        party_snapshot_provenance=(
            PartySnapshotProvenance(snapshot.provenance)
            if (snapshot := inv.__dict__.get("party_snapshot")) is not None
            else None
        ),
        party_snapshot_locale=(
            snapshot.locale
            if (snapshot := inv.__dict__.get("party_snapshot")) is not None
            else None
        ),
        party_snapshot_customer_email=(
            snapshot.buyer_email
            if (snapshot := inv.__dict__.get("party_snapshot")) is not None
            else None
        ),
        source_invoice_id=(
            correction.source_invoice_id
            if (correction := inv.__dict__.get("correction")) is not None
            else None
        ),
        credit_draft_intent=(
            CreditDraftIntentRead(
                full_remaining=correction.full_remaining,
                requires_confirmation=(
                    correction.full_remaining is None
                    or correction.intent_provenance == "MIGRATED_AMBIGUOUS"
                ),
                provenance=(
                    "MIGRATED_AMBIGUOUS"
                    if correction.intent_provenance == "MIGRATED_AMBIGUOUS"
                    else "NATIVE"
                ),
                lines=[
                    CreditDraftIntentLineRead(
                        source_basis_line_id=line.source_basis_line_id,
                        input_mode=line.input_mode,
                        quantity=(Decimal(str(line.input_quantity)) if line.input_quantity is not None else None),
                        gross_amount=(Decimal(str(line.input_gross_amount)) if line.input_gross_amount is not None else None),
                    )
                    for line in correction.lines
                ],
            )
            if correction is not None
            else None
        ),
        advance_input_mode=(AdvanceInputMode(inv.advance_input_mode) if inv.advance_input_mode is not None else None),
        advance_gross_amount=(Decimal(str(inv.advance_gross_amount)) if inv.advance_gross_amount is not None else None),
        advance_percentage=(Decimal(str(inv.advance_percentage)) if inv.advance_percentage is not None else None),
        replacement_of_credit_note_id=(
            replacement_relation.related_credit_note_id
            if replacement_relation is not None
            else None
        ),
        compensates_credit_note_id=(
            compensation_relation.related_credit_note_id
            if compensation_relation is not None
            else None
        ),
        original_quote_totals=(
            FinalTotalsRead(
                taxable_amount=Decimal(str(inv.final_original_taxable_amount)),
                vat_total=Decimal(str(inv.final_original_vat_amount)),
                gross_amount=original_gross,
            )
            if original_gross is not None
            else None
        ),
        final_totals=(
            FinalTotalsRead(
                taxable_amount=Decimal(str(inv.taxable_amount)),
                vat_total=Decimal(str(inv.vat_total)),
                gross_amount=Decimal(str(inv.total_incl_vat)),
            )
            if InvoiceDocumentKind(inv.document_kind) == InvoiceDocumentKind.FINAL
            else None
        ),
        final_variance=(
            FinalVarianceRead(
                taxable_amount=Decimal(str(inv.taxable_amount))
                - Decimal(str(inv.final_original_taxable_amount)),
                vat_amount=Decimal(str(inv.vat_total))
                - Decimal(str(inv.final_original_vat_amount)),
                gross_amount=Decimal(str(inv.total_incl_vat)) - original_gross,
            )
            if original_gross is not None
            else None
        ),
        final_advance_applications=[
            FinalAdvanceApplicationRead(
                advance_invoice_id=app.advance_invoice_id,
                advance_invoice_number=app.advance_invoice_number,
                advance_invoice_date=app.advance_invoice_date,
                sort_order=app.sort_order,
                taxable_amount=Decimal(str(app.taxable_amount)),
                vat_amount=Decimal(str(app.vat_amount)),
                gross_amount=Decimal(str(app.gross_amount)),
                base_taxable_amount=Decimal(str(app.base_taxable_amount)),
                base_vat_amount=Decimal(str(app.base_vat_amount)),
                base_gross_amount=Decimal(str(app.base_gross_amount)),
                taxes=[
                    FinalAdvanceApplicationTaxRead(
                        source_vat_rate_id=tax.source_vat_rate_id,
                        source_vat_rate_label=tax.source_vat_rate_label,
                        source_vat_rate_percent=Decimal(str(tax.source_vat_rate_percent)),
                        taxable_amount=Decimal(str(tax.taxable_amount)),
                        vat_amount=Decimal(str(tax.vat_amount)),
                        gross_amount=Decimal(str(tax.gross_amount)),
                        base_taxable_amount=Decimal(str(tax.base_taxable_amount)),
                        base_vat_amount=Decimal(str(tax.base_vat_amount)),
                        base_gross_amount=Decimal(str(tax.base_gross_amount)),
                    )
                    for tax in app.taxes
                ],
            )
            for app in inv.final_advance_applications
        ],
        currency=inv.currency,
        exchange_rate=Decimal(str(inv.exchange_rate)),
        tax_mode=InvoiceTaxMode(inv.tax_mode),
        amounts_include_vat=inv.amounts_include_vat,
        vat_treatment_id=inv.vat_treatment_id,
        document_vat_rate_id=inv.document_vat_rate_id,
        vat_treatment_snapshot=treatment_snapshot,
        discount_type=DiscountType(inv.discount_type),
        discount_value=Decimal(str(inv.discount_value)),
        document_discount_amount=Decimal(str(inv.document_discount_amount)),
        subtotal_excl_vat=Decimal(str(inv.subtotal_excl_vat)),
        line_discount_total=Decimal(str(inv.line_discount_total)),
        taxable_amount=Decimal(str(inv.taxable_amount)),
        vat_total=Decimal(str(inv.vat_total)),
        total_incl_vat=Decimal(str(inv.total_incl_vat)),
        due_amount=Decimal(str(inv.due_amount)),
        payable_before_payments=Decimal(str(inv.payable_before_payments)),
        incoming_payment_total=Decimal(str(inv.incoming_payment_total)),
        credited_total=Decimal(str(inv.credited_total)),
        refunded_total=Decimal(str(inv.refunded_total)),
        refund_due_amount=Decimal(str(inv.refund_due_amount)),
        settlement_status=InvoiceSettlementStatus(inv.settlement_status),
        credit_status=InvoiceCreditStatus(inv.credit_status),
        base_subtotal_excl_vat=Decimal(str(inv.base_subtotal_excl_vat)),
        base_line_discount_total=Decimal(str(inv.base_line_discount_total)),
        base_taxable_amount=Decimal(str(inv.base_taxable_amount)),
        base_vat_total=Decimal(str(inv.base_vat_total)),
        base_total_incl_vat=Decimal(str(inv.base_total_incl_vat)),
        base_due_amount=Decimal(str(inv.base_due_amount)),
        base_payable_before_payments=Decimal(str(inv.base_payable_before_payments)),
        base_incoming_payment_total=Decimal(str(inv.base_incoming_payment_total)),
        base_credited_total=Decimal(str(inv.base_credited_total)),
        base_refunded_total=Decimal(str(inv.base_refunded_total)),
        base_refund_due_amount=Decimal(str(inv.base_refund_due_amount)),
        notes=inv.notes,
        warranty_text=inv.warranty_text,
        terms_text=inv.terms_text,
        bank_text=inv.bank_text,
        payment_terms_text=inv.payment_terms_text,
        creator_id=inv.creator_id,
        lines=[_line_to_read(line) for line in inv.lines],
        taxes=[
            InvoiceTaxRowRead(
                id=t.id,
                vat_rate_id=t.vat_rate_id,
                vat_rate_label=t.vat_rate_label,
                vat_rate_percent=Decimal(str(t.vat_rate_percent)),
                effective_vat_percent=Decimal(str(t.effective_vat_percent)),
                taxable_amount=Decimal(str(t.taxable_amount)),
                tax_amount=Decimal(str(t.tax_amount)),
            )
            for t in inv.taxes
        ],
        created_at=inv.created_at,
        updated_at=inv.updated_at,
    )


def _invoice_to_list_item(inv: Invoice, *, customer_name: str) -> InvoiceListItem:
    positive_relations = inv.__dict__.get("positive_relations") or []
    replacement_relation = next(
        (
            relation
            for relation in positive_relations
            if InvoiceRelationType(relation.relation_type) == InvoiceRelationType.REPLACEMENT_OF
        ),
        None,
    )
    compensation_relation = next(
        (
            relation
            for relation in positive_relations
            if InvoiceRelationType(relation.relation_type) == InvoiceRelationType.COMPENSATES_CREDIT
        ),
        None,
    )
    return InvoiceListItem(
        id=inv.id,
        company_id=inv.company_id,
        customer_id=inv.customer_id,
        customer_name=customer_name,
        invoice_number=inv.invoice_number,
        reference_number=inv.reference_number,
        invoice_date=inv.invoice_date,
        due_date=inv.due_date,
        status=InvoiceStatus(inv.status),
        paid_status=InvoicePaidStatus(inv.paid_status),
        document_kind=InvoiceDocumentKind(inv.document_kind),
        quote_id=inv.quote_id,
        supply_or_advance_date=inv.supply_or_advance_date,
        issued_at=inv.issued_at,
        issued_by_user_id=inv.issued_by_user_id,
        party_snapshot_provenance=(
            PartySnapshotProvenance(snapshot.provenance)
            if (snapshot := inv.__dict__.get("party_snapshot")) is not None
            else None
        ),
        party_snapshot_locale=(
            snapshot.locale
            if (snapshot := inv.__dict__.get("party_snapshot")) is not None
            else None
        ),
        source_invoice_id=(
            correction.source_invoice_id
            if (correction := inv.__dict__.get("correction")) is not None
            else None
        ),
        replacement_of_credit_note_id=(
            replacement_relation.related_credit_note_id
            if replacement_relation is not None
            else None
        ),
        compensates_credit_note_id=(
            compensation_relation.related_credit_note_id
            if compensation_relation is not None
            else None
        ),
        settlement_status=InvoiceSettlementStatus(inv.settlement_status),
        credit_status=InvoiceCreditStatus(inv.credit_status),
        currency=inv.currency,
        total_incl_vat=Decimal(str(inv.total_incl_vat)),
        payable_before_payments=Decimal(str(inv.payable_before_payments)),
        incoming_payment_total=Decimal(str(inv.incoming_payment_total)),
        credited_total=Decimal(str(inv.credited_total)),
        refunded_total=Decimal(str(inv.refunded_total)),
        due_amount=Decimal(str(inv.due_amount)),
        refund_due_amount=Decimal(str(inv.refund_due_amount)),
        base_total_incl_vat=Decimal(str(inv.base_total_incl_vat)),
        base_payable_before_payments=Decimal(str(inv.base_payable_before_payments)),
        base_incoming_payment_total=Decimal(str(inv.base_incoming_payment_total)),
        base_credited_total=Decimal(str(inv.base_credited_total)),
        base_refunded_total=Decimal(str(inv.base_refunded_total)),
        base_due_amount=Decimal(str(inv.base_due_amount)),
        base_refund_due_amount=Decimal(str(inv.base_refund_due_amount)),
        vat_treatment_snapshot=VatTreatmentSnapshot(
            id=inv.vat_treatment_id,
            code=inv.vat_treatment_code,
            label=inv.vat_treatment_label,
            effect=inv.vat_treatment_effect,
            requires_icp=inv.vat_treatment_requires_icp,
        ),
        created_at=inv.created_at,
        updated_at=inv.updated_at,
    )


async def _load_invoice_read(session: AsyncSession, inv: Invoice) -> InvoiceRead:
    """Load every response relationship before committing a tenant transaction."""
    statement = (
        select(Invoice)
        .where(Invoice.id == inv.id)
        .options(
            selectinload(Invoice.lines).selectinload(InvoiceLine.line_taxes),
            selectinload(Invoice.taxes),
            selectinload(Invoice.party_snapshot),
            selectinload(Invoice.credit_basis_lines),
            selectinload(Invoice.correction).selectinload(InvoiceCorrection.lines),
            selectinload(Invoice.positive_relations),
            selectinload(Invoice.final_advance_applications).selectinload(
                FinalAdvanceApplication.taxes
            ),
        )
    )
    if InvoiceDocumentKind(inv.document_kind) == InvoiceDocumentKind.FINAL:
        # A Final PUT replaces line/tax children, while Final applications are
        # intentionally ``noload`` by default.  Refresh the explicit Final
        # response relationships without changing established Standard/Advance
        # Decimal serialisation behaviour.
        statement = statement.execution_options(populate_existing=True)
    result = await session.execute(statement)
    return _invoice_to_read(result.scalar_one())


# ---------------------------------------------------------------------------
# Core calculation + persistence helpers
# ---------------------------------------------------------------------------


async def _build_and_persist_invoice(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    company_currency: str,
    creator_id: uuid.UUID | None,
    body: InvoiceWrite,
    customer: Customer,
    treatment: VatTreatment,
    vat_rates: dict[uuid.UUID, tuple[str, Decimal]],
    # For update: provide the existing number (kept verbatim); for create:
    # leave None — a DRAFT carries no number, which is allocated only at the
    # DRAFT -> SENT issue transition (see ``transition_status``).
    invoice_number: str | None = None,
    sequence_number: int | None = None,
    customer_sequence_number: int | None = None,
    existing_invoice: Invoice | None = None,
) -> Invoice:
    """Calculate pricing and persist a new or updated invoice.

    For creates: the invoice is persisted as a DRAFT with NO number
    (``invoice_number``/``sequence_number``/``customer_sequence_number`` all
    None).  The legal number is allocated later, at the DRAFT -> SENT
    transition, so deleting an unissued draft leaves no gap in the sequence.
    For updates: the caller passes the existing number, which is preserved.
    """
    request_currency = body.currency or company_currency
    if request_currency != company_currency:
        raise ValueError(
            f"M5 only supports invoices in company base currency "
            f"({company_currency}); got {request_currency}."
        )

    treatment_effect = VatTreatmentEffect(treatment.effect)

    document_vat_rate: tuple[str, Decimal] | None = None
    if body.tax_mode == InvoiceTaxMode.DOCUMENT:
        rate_info = vat_rates.get(body.document_vat_rate_id)  # type: ignore[arg-type]
        document_vat_rate = rate_info

    # Pure pricing calculation
    calc = compute_pricing(
        body.lines,
        tax_mode=body.tax_mode,
        amounts_include_vat=body.amounts_include_vat,
        discount=body.discount,
        treatment_effect=treatment_effect,
        vat_rates=vat_rates,
        document_vat_rate=document_vat_rate,
    )

    # Fix document tax vat_rate_id placeholder
    if body.tax_mode == InvoiceTaxMode.DOCUMENT and calc.document_taxes:
        calc.document_taxes[0].vat_rate_id = body.document_vat_rate_id  # type: ignore[assignment]

    # No number is allocated here.  Drafts are created unnumbered; the legal
    # number is assigned at the DRAFT -> SENT issue transition (red line 4
    # preserved: allocation stays row-locked + same-transaction, just later).
    total_incl_vat = quantize_to_minor_unit(calc.total_incl_vat)

    # Determine treatment effect string for snapshot
    treatment_effect_str = (
        treatment.effect.value
        if isinstance(treatment.effect, VatTreatmentEffect)
        else str(treatment.effect)
    )

    # Common money values
    subtotal_excl_vat = quantize_to_minor_unit(calc.subtotal_excl_vat)
    line_discount_total = quantize_to_minor_unit(calc.line_discount_total)
    taxable_amount = quantize_to_minor_unit(calc.taxable_amount)
    vat_total = quantize_to_minor_unit(calc.vat_total)
    doc_discount_amount = quantize_to_minor_unit(calc.document_discount_amount)

    # Build or update Invoice row
    if existing_invoice is not None:
        inv = existing_invoice
        # Replace sub-tables: delete old lines (DB CASCADE removes line_taxes)
        # and old document taxes via explicit DELETE (avoids ORM selectin load).
        await session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == inv.id))
        await session.execute(delete(InvoiceTax).where(InvoiceTax.invoice_id == inv.id))
    else:
        inv = Invoice()
        inv.company_id = company_id
        inv.invoice_number = invoice_number
        inv.sequence_number = sequence_number
        inv.customer_sequence_number = customer_sequence_number
        inv.status = InvoiceStatus.DRAFT
        inv.paid_status = InvoicePaidStatus.UNPAID
        inv.creator_id = creator_id

    # Set all fields before any flush
    inv.customer_id = customer.id
    inv.reference_number = body.reference_number
    inv.invoice_date = body.invoice_date
    inv.due_date = body.due_date
    inv.supply_or_advance_date = body.supply_or_advance_date
    inv.currency = request_currency
    inv.exchange_rate = Decimal("1")

    inv.tax_mode = body.tax_mode
    inv.amounts_include_vat = body.amounts_include_vat
    inv.vat_treatment_id = treatment.id
    # LINE mode must never persist a document-level rate; clear any stale value.
    inv.document_vat_rate_id = (
        body.document_vat_rate_id if body.tax_mode == InvoiceTaxMode.DOCUMENT else None
    )

    inv.vat_treatment_code = treatment.code
    inv.vat_treatment_label = treatment.label
    inv.vat_treatment_effect = treatment_effect_str
    inv.vat_treatment_requires_icp = treatment.requires_icp

    inv.discount_type = body.discount.type
    inv.discount_value = body.discount.value
    inv.document_discount_amount = doc_discount_amount

    inv.subtotal_excl_vat = subtotal_excl_vat
    inv.line_discount_total = line_discount_total
    inv.taxable_amount = taxable_amount
    inv.vat_total = vat_total
    inv.total_incl_vat = total_incl_vat
    if existing_invoice is None:
        inv.due_amount = total_incl_vat  # M5: full amount owed until M7
        inv.payable_before_payments = total_incl_vat
        inv.incoming_payment_total = Decimal("0")
        inv.credited_total = Decimal("0")
        inv.refunded_total = Decimal("0")
        inv.refund_due_amount = Decimal("0")
        inv.settlement_status = InvoiceSettlementStatus.OPEN
        inv.credit_status = InvoiceCreditStatus.NOT_CREDITED

    # M5: exchange_rate = 1; base_* = *
    inv.base_subtotal_excl_vat = subtotal_excl_vat
    inv.base_line_discount_total = line_discount_total
    inv.base_taxable_amount = taxable_amount
    inv.base_vat_total = vat_total
    inv.base_total_incl_vat = total_incl_vat
    if existing_invoice is None:
        inv.base_due_amount = total_incl_vat
        inv.base_payable_before_payments = total_incl_vat
        inv.base_incoming_payment_total = Decimal("0")
        inv.base_credited_total = Decimal("0")
        inv.base_refunded_total = Decimal("0")
        inv.base_refund_due_amount = Decimal("0")
    inv.document_kind = InvoiceDocumentKind.STANDARD

    inv.notes = body.notes

    # -- Content block snapshot text (M6 step 4) -----------------------------
    inv.warranty_text = body.warranty_text
    inv.terms_text = body.terms_text
    inv.bank_text = body.bank_text
    inv.payment_terms_text = body.payment_terms_text

    # Persist invoice (create: add + flush to get ID; update: just flush)
    if existing_invoice is None:
        session.add(inv)
    await session.flush()  # get inv.id for child FKs

    # Build line rows - avoid ORM relationship append to prevent selectin triggers;
    # set FK directly and add to session.
    new_line_rows: list[InvoiceLine] = []
    for i, (line_input, line_calc) in enumerate(zip(body.lines, calc.lines, strict=True)):
        line_row = InvoiceLine()
        line_row.invoice_id = inv.id
        line_row.sort_order = i
        line_row.product_id = line_input.product_id
        line_row.name = line_input.name
        line_row.description = line_input.description
        line_row.quantity = line_input.quantity
        line_row.unit_id = line_input.unit_id
        line_row.unit_name = line_input.unit_name
        line_row.unit_price = line_input.unit_price
        line_row.discount_type = line_input.discount.type
        line_row.discount_value = line_input.discount.value
        line_row.vat_rate_id = line_input.vat_rate_id

        if body.tax_mode == InvoiceTaxMode.LINE and line_input.vat_rate_id is not None:
            rate_label, rate_percent = vat_rates[line_input.vat_rate_id]
            line_row.vat_rate_label = rate_label
            line_row.vat_rate_percent = rate_percent
        else:
            line_row.vat_rate_label = None
            line_row.vat_rate_percent = None

        line_row.subtotal_excl_vat = quantize_to_minor_unit(line_calc.subtotal_excl_vat)
        line_row.subtotal_incl_vat = quantize_to_minor_unit(line_calc.subtotal_incl_vat)
        line_row.line_discount_amount = quantize_to_minor_unit(line_calc.line_discount_amount)
        line_row.document_discount_share = quantize_to_minor_unit(line_calc.document_discount_share)
        line_row.taxable_amount = quantize_to_minor_unit(line_calc.taxable_amount)
        line_row.vat_total = quantize_to_minor_unit(line_calc.vat_total)
        line_row.total_incl_vat = quantize_to_minor_unit(line_calc.total_incl_vat)

        session.add(line_row)
        new_line_rows.append(line_row)

    await session.flush()  # get line row IDs

    # Build per-line tax rows (LINE mode)
    if body.tax_mode == InvoiceTaxMode.LINE:
        for line_row, lt in zip(new_line_rows, calc.line_taxes, strict=False):
            lt_row = InvoiceLineTax()
            lt_row.invoice_line_id = line_row.id
            lt_row.vat_rate_id = lt.vat_rate_id
            lt_row.vat_rate_label = lt.vat_rate_label
            lt_row.vat_rate_percent = lt.vat_rate_percent
            lt_row.effective_vat_percent = lt.effective_vat_percent
            lt_row.taxable_amount = lt.taxable_amount
            lt_row.tax_amount = lt.tax_amount
            session.add(lt_row)

    # Build document tax rows (DOCUMENT mode)
    if body.tax_mode == InvoiceTaxMode.DOCUMENT:
        for dt in calc.document_taxes:
            tax_row = InvoiceTax()
            tax_row.invoice_id = inv.id
            tax_row.vat_rate_id = dt.vat_rate_id
            tax_row.vat_rate_label = dt.vat_rate_label
            tax_row.vat_rate_percent = dt.vat_rate_percent
            tax_row.effective_vat_percent = dt.effective_vat_percent
            tax_row.taxable_amount = dt.taxable_amount
            tax_row.tax_amount = dt.tax_amount
            session.add(tax_row)

    await session.flush()
    return inv


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def _address_snapshot(address: Address | None) -> dict[str, str | None]:
    """Return a plain immutable structured-address payload."""
    fields = (
        "street",
        "house_number",
        "house_number_addition",
        "postal_code",
        "city",
        "province",
        "country_code",
    )
    return {field: getattr(address, field) if address is not None else None for field in fields}


def _allocate_document_vat(line_nets: list[Decimal], total_vat: Decimal) -> list[Decimal]:
    """Allocate a persisted document-tax amount by persisted line net amounts."""
    vat_units = int(quantize_to_minor_unit(total_vat) * Decimal("100"))
    net_units = [int(quantize_to_minor_unit(net) * Decimal("100")) for net in line_nets]
    total_net_units = sum(net_units)
    if vat_units == 0:
        return [Decimal("0") for _ in line_nets]
    if total_net_units <= 0:
        raise ValueError("Document VAT cannot be allocated without taxable line snapshots.")
    allocated: list[int] = []
    remainders: list[tuple[int, int]] = []
    for index, line_units in enumerate(net_units):
        allocation, remainder = divmod(vat_units * line_units, total_net_units)
        allocated.append(allocation)
        remainders.append((remainder, index))
    for _, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[
        : vat_units - sum(allocated)
    ]:
        allocated[index] += 1
    return [Decimal(amount) / Decimal("100") for amount in allocated]


async def _credit_basis_rows(session: AsyncSession, inv: Invoice) -> list[InvoiceCreditBasisLine]:
    """Build immutable basis rows from persisted invoice snapshots only."""
    lines = list(
        (
            await session.execute(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == inv.id)
                .order_by(InvoiceLine.sort_order, InvoiceLine.id)
            )
        ).scalars()
    )
    taxes = list(
        (await session.execute(select(InvoiceTax).where(InvoiceTax.invoice_id == inv.id))).scalars()
    )
    line_taxes = list(
        (
            await session.execute(
                select(InvoiceLineTax)
                .join(InvoiceLine, InvoiceLineTax.invoice_line_id == InvoiceLine.id)
                .where(InvoiceLine.invoice_id == inv.id)
            )
        ).scalars()
    )
    line_tax_by_line_id = {tax.invoice_line_id: tax for tax in line_taxes}
    document_tax: InvoiceTax | None = None
    if InvoiceTaxMode(inv.tax_mode) == InvoiceTaxMode.DOCUMENT:
        if len(taxes) != 1:
            raise ValueError(
                "Issued DOCUMENT invoice must have exactly one persisted tax snapshot."
            )
        document_tax = taxes[0]
        vat_amounts = _allocate_document_vat(
            [Decimal(str(line.taxable_amount)) for line in lines],
            Decimal(str(document_tax.tax_amount)),
        )
    else:
        vat_amounts = [Decimal(str(line.vat_total)) for line in lines]
    exchange_rate = Decimal(str(inv.exchange_rate))
    rows: list[InvoiceCreditBasisLine] = []
    for line, vat_amount in zip(lines, vat_amounts, strict=True):
        net_amount = Decimal(str(line.taxable_amount))
        gross_amount = net_amount + vat_amount
        rows.append(
            InvoiceCreditBasisLine(
                company_id=inv.company_id,
                invoice_id=inv.id,
                invoice_line_id=line.id,
                sort_order=line.sort_order,
                name=line.name,
                description=line.description,
                quantity=Decimal(str(line.quantity)),
                unit_name=line.unit_name,
                vat_rate_id=document_tax.vat_rate_id if document_tax else line.vat_rate_id,
                vat_rate_label=document_tax.vat_rate_label if document_tax else line.vat_rate_label,
                vat_rate_percent=(
                    Decimal(str(document_tax.vat_rate_percent))
                    if document_tax is not None
                    else (
                        Decimal(str(line.vat_rate_percent))
                        if line.vat_rate_percent is not None
                        else None
                    )
                ),
                effective_vat_percent=(
                    Decimal(str(document_tax.effective_vat_percent))
                    if document_tax is not None
                    else (
                        Decimal(str(line_tax_by_line_id[line.id].effective_vat_percent))
                        if line.id in line_tax_by_line_id
                        else None
                    )
                ),
                vat_treatment_code=inv.vat_treatment_code,
                vat_treatment_effect=inv.vat_treatment_effect,
                vat_treatment_requires_icp=inv.vat_treatment_requires_icp,
                net_amount=net_amount,
                vat_amount=vat_amount,
                gross_amount=gross_amount,
                base_net_amount=net_amount * exchange_rate,
                base_vat_amount=vat_amount * exchange_rate,
                base_gross_amount=gross_amount * exchange_rate,
            )
        )
    return rows


async def _final_residual_credit_basis_rows(
    session: AsyncSession, inv: Invoice
) -> list[InvoiceCreditBasisLine]:
    """Reduce Final's immutable Credit basis by its frozen applications.

    The displayed Final remains the full edited project.  Credit entitlement is
    only its residual charge, apportioned deterministically inside each VAT
    bucket over the persisted line basis; applications are never reread from
    cash payments.
    """
    full_rows = await _credit_basis_rows(session, inv)
    application_rows = list(
        (
            await session.execute(
                select(FinalAdvanceApplicationTax)
                .join(FinalAdvanceApplication)
                .where(FinalAdvanceApplication.final_invoice_id == inv.id)
            )
        ).scalars()
    )
    applied: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for tax in application_rows:
        net, vat = applied.get(tax.source_vat_rate_id, (Decimal("0"), Decimal("0")))
        applied[tax.source_vat_rate_id] = (
            net + Decimal(str(tax.taxable_amount)),
            vat + Decimal(str(tax.vat_amount)),
        )
    grouped: dict[uuid.UUID | None, list[InvoiceCreditBasisLine]] = {}
    for row in full_rows:
        grouped.setdefault(row.vat_rate_id, []).append(row)
    result: list[InvoiceCreditBasisLine] = []
    for rate_id, rows in grouped.items():
        source_net = sum((Decimal(str(row.net_amount)) for row in rows), Decimal("0"))
        source_vat = sum((Decimal(str(row.vat_amount)) for row in rows), Decimal("0"))
        app_net, app_vat = (
            applied.get(rate_id, (Decimal("0"), Decimal("0")))
            if rate_id is not None
            else (Decimal("0"), Decimal("0"))
        )
        residual_net = source_net - app_net
        residual_vat = source_vat - app_vat
        if residual_net < 0 or residual_vat < 0:
            raise ValueError("Final applications exceed the edited Final credit basis.")
        net_shares = _allocate_document_vat(
            [Decimal(str(row.net_amount)) for row in rows], residual_net
        )
        vat_shares = _allocate_document_vat(
            [Decimal(str(row.vat_amount)) for row in rows], residual_vat
        )
        for row, net, vat in zip(rows, net_shares, vat_shares, strict=True):
            if net + vat == 0:
                continue
            row.net_amount = net
            row.vat_amount = vat
            row.gross_amount = net + vat
            row.base_net_amount = net
            row.base_vat_amount = vat
            row.base_gross_amount = net + vat
            result.append(row)
    return result


async def _resolved_issue_locale(
    session: AsyncSession, company_id: uuid.UUID, customer: Customer
) -> str:
    defaults = await get_setting(
        session,
        SETTING_KEY_DOCUMENT_DEFAULTS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=DocumentDefaultsSetting,
    )
    if customer.locale in {"en", "zh"}:
        return customer.locale
    return defaults.locale if defaults is not None else "en"


async def _create_native_issue_foundation(
    session: AsyncSession,
    inv: Invoice,
    *,
    issued_by_user_id: uuid.UUID | None,
) -> None:
    """Freeze party and source-basis snapshots for an issued Standard Invoice.

    This is intentionally invoked only at the legal issue transition and is
    part of the caller's transaction.  It does not calculate money: all values
    are copied from already persisted invoice snapshots.
    """
    if inv.party_snapshot is not None:
        return
    company = (
        await session.execute(select(Company).where(Company.id == inv.company_id))
    ).scalar_one()
    customer = (
        await session.execute(
            select(Customer)
            .where(Customer.id == inv.customer_id)
            .options(selectinload(Customer.addresses))
        )
    ).scalar_one()
    billing_address = next(
        (address for address in customer.addresses if address.type.value == "BILLING"), None
    )
    snapshot = InvoicePartySnapshot(
        company_id=inv.company_id,
        invoice_id=inv.id,
        provenance=PartySnapshotProvenance.NATIVE_ISSUE,
        seller_name=company.name,
        seller_legal_name=company.legal_name,
        seller_vat_id=company.vat_id,
        seller_coc_number=company.coc_number,
        seller_email=company.email,
        seller_phone=company.phone,
        seller_address={
            "street": company.street,
            "house_number": company.house_number,
            "house_number_addition": company.house_number_addition,
            "postal_code": company.postal_code,
            "city": company.city,
            "province": company.province,
            "country_code": company.country_code,
        },
        buyer_name=customer.name,
        buyer_company_name=customer.company_name,
        buyer_contact_name=customer.contact_name,
        buyer_vat_id=customer.vat_id,
        buyer_email=customer.email,
        buyer_phone=customer.phone,
        buyer_address=_address_snapshot(billing_address),
        locale=await _resolved_issue_locale(session, inv.company_id, customer),
        logo_id=company.logo_id,
    )
    # Assign through ORM relationships too: ``inv`` was selectin-loaded before
    # issue, so merely inserting FK rows would leave its in-memory cached
    # relationship at None while we intentionally build the response precommit.
    inv.party_snapshot = snapshot
    inv.credit_basis_lines = (
        await _final_residual_credit_basis_rows(session, inv)
        if InvoiceDocumentKind(inv.document_kind) == InvoiceDocumentKind.FINAL
        else await _credit_basis_rows(session, inv)
    )
    inv.issued_at = datetime.now(UTC)
    inv.issued_by_user_id = issued_by_user_id
    if inv.supply_or_advance_date is None:
        inv.supply_or_advance_date = inv.invoice_date
    await session.flush()


async def clone_quote_to_invoice(
    session: AsyncSession,
    quote: _Quote,
    *,
    company_id: uuid.UUID,
    creator_id: uuid.UUID | None,
) -> InvoiceRead:
    """Build an Invoice by copying the quote's stored snapshots verbatim.

    No pricing re-computation occurs.  All amounts, VAT rate labels/percentages,
    and the treatment snapshot are cloned directly from the quote's persisted
    rows.  The new invoice is a DRAFT with NO number; the legal number is
    allocated later, at the DRAFT -> SENT issue transition.

    The caller owns the ``session.commit()`` so that invoice creation and the
    quote's ``converted_invoice_id`` back-link update are atomic.
    """
    inv = Invoice()
    inv.company_id = company_id
    inv.customer_id = quote.customer_id
    # DRAFT carries no number; allocated at DRAFT -> SENT (see transition_status).
    inv.invoice_number = None
    inv.sequence_number = None
    inv.customer_sequence_number = None
    inv.reference_number = quote.reference_number
    inv.invoice_date = date.today()
    inv.due_date = None
    inv.supply_or_advance_date = None
    inv.status = InvoiceStatus.DRAFT
    inv.paid_status = InvoicePaidStatus.UNPAID
    inv.document_kind = InvoiceDocumentKind.STANDARD
    inv.quote_id = quote.id
    inv.currency = quote.currency
    inv.exchange_rate = Decimal(str(quote.exchange_rate))
    inv.tax_mode = InvoiceTaxMode(quote.tax_mode)
    inv.amounts_include_vat = quote.amounts_include_vat
    inv.vat_treatment_id = quote.vat_treatment_id
    inv.document_vat_rate_id = quote.document_vat_rate_id
    # Copy treatment snapshot verbatim — no re-resolution against current dictionary
    inv.vat_treatment_code = quote.vat_treatment_code
    inv.vat_treatment_label = quote.vat_treatment_label
    inv.vat_treatment_effect = quote.vat_treatment_effect
    inv.vat_treatment_requires_icp = quote.vat_treatment_requires_icp
    # Copy discount
    inv.discount_type = DiscountType(quote.discount_type)
    inv.discount_value = Decimal(str(quote.discount_value))
    inv.document_discount_amount = Decimal(str(quote.document_discount_amount))
    # Copy all computed amounts verbatim
    inv.subtotal_excl_vat = Decimal(str(quote.subtotal_excl_vat))
    inv.line_discount_total = Decimal(str(quote.line_discount_total))
    inv.taxable_amount = Decimal(str(quote.taxable_amount))
    inv.vat_total = Decimal(str(quote.vat_total))
    inv.total_incl_vat = Decimal(str(quote.total_incl_vat))
    inv.due_amount = Decimal(str(quote.total_incl_vat))
    inv.payable_before_payments = Decimal(str(quote.total_incl_vat))
    inv.incoming_payment_total = Decimal("0")
    inv.credited_total = Decimal("0")
    inv.refunded_total = Decimal("0")
    inv.refund_due_amount = Decimal("0")
    inv.settlement_status = InvoiceSettlementStatus.OPEN
    inv.credit_status = InvoiceCreditStatus.NOT_CREDITED
    inv.base_subtotal_excl_vat = Decimal(str(quote.base_subtotal_excl_vat))
    inv.base_line_discount_total = Decimal(str(quote.base_line_discount_total))
    inv.base_taxable_amount = Decimal(str(quote.base_taxable_amount))
    inv.base_vat_total = Decimal(str(quote.base_vat_total))
    inv.base_total_incl_vat = Decimal(str(quote.base_total_incl_vat))
    inv.base_due_amount = Decimal(str(quote.base_total_incl_vat))
    inv.base_payable_before_payments = Decimal(str(quote.base_total_incl_vat))
    inv.base_incoming_payment_total = Decimal("0")
    inv.base_credited_total = Decimal("0")
    inv.base_refunded_total = Decimal("0")
    inv.base_refund_due_amount = Decimal("0")
    inv.notes = quote.notes
    inv.warranty_text = quote.warranty_text
    inv.terms_text = quote.terms_text
    inv.bank_text = quote.bank_text
    inv.payment_terms_text = quote.payment_terms_text
    inv.creator_id = creator_id

    session.add(inv)
    await session.flush()  # get inv.id for child FKs

    # Clone line rows
    line_pairs: list[tuple[_QuoteLine, InvoiceLine]] = []
    for q_line in quote.lines:
        inv_line = InvoiceLine()
        inv_line.invoice_id = inv.id
        inv_line.sort_order = q_line.sort_order
        inv_line.product_id = q_line.product_id
        inv_line.name = q_line.name
        inv_line.description = q_line.description
        inv_line.quantity = Decimal(str(q_line.quantity))
        inv_line.unit_id = q_line.unit_id
        inv_line.unit_name = q_line.unit_name
        inv_line.unit_price = Decimal(str(q_line.unit_price))
        inv_line.discount_type = DiscountType(q_line.discount_type)
        inv_line.discount_value = Decimal(str(q_line.discount_value))
        inv_line.vat_rate_id = q_line.vat_rate_id
        inv_line.vat_rate_label = q_line.vat_rate_label
        inv_line.vat_rate_percent = (
            Decimal(str(q_line.vat_rate_percent)) if q_line.vat_rate_percent is not None else None
        )
        inv_line.subtotal_excl_vat = Decimal(str(q_line.subtotal_excl_vat))
        inv_line.subtotal_incl_vat = Decimal(str(q_line.subtotal_incl_vat))
        inv_line.line_discount_amount = Decimal(str(q_line.line_discount_amount))
        inv_line.document_discount_share = Decimal(str(q_line.document_discount_share))
        inv_line.taxable_amount = Decimal(str(q_line.taxable_amount))
        inv_line.vat_total = Decimal(str(q_line.vat_total))
        inv_line.total_incl_vat = Decimal(str(q_line.total_incl_vat))
        session.add(inv_line)
        line_pairs.append((q_line, inv_line))

    await session.flush()  # get inv_line.id for child FKs

    # Clone per-line taxes (LINE mode)
    for q_line, inv_line in line_pairs:
        for q_lt in q_line.line_taxes:
            inv_lt = InvoiceLineTax()
            inv_lt.invoice_line_id = inv_line.id
            inv_lt.vat_rate_id = q_lt.vat_rate_id
            inv_lt.vat_rate_label = q_lt.vat_rate_label
            inv_lt.vat_rate_percent = Decimal(str(q_lt.vat_rate_percent))
            inv_lt.effective_vat_percent = Decimal(str(q_lt.effective_vat_percent))
            inv_lt.taxable_amount = Decimal(str(q_lt.taxable_amount))
            inv_lt.tax_amount = Decimal(str(q_lt.tax_amount))
            session.add(inv_lt)

    # Clone document-level taxes (DOCUMENT mode)
    for q_tax in quote.taxes:
        inv_tax = InvoiceTax()
        inv_tax.invoice_id = inv.id
        inv_tax.vat_rate_id = q_tax.vat_rate_id
        inv_tax.vat_rate_label = q_tax.vat_rate_label
        inv_tax.vat_rate_percent = Decimal(str(q_tax.vat_rate_percent))
        inv_tax.effective_vat_percent = Decimal(str(q_tax.effective_vat_percent))
        inv_tax.taxable_amount = Decimal(str(q_tax.taxable_amount))
        inv_tax.tax_amount = Decimal(str(q_tax.tax_amount))
        session.add(inv_tax)

    await session.flush()
    # Use an explicit SELECT with nested selectinload so that inv.lines and
    # line.line_taxes are loaded in the async context before _invoice_to_read
    # accesses them synchronously.  A plain session.refresh(inv) only loads
    # inv.lines (one level deep) and leaves line.line_taxes unloaded.
    load_stmt = (
        select(Invoice)
        .options(
            selectinload(Invoice.lines).selectinload(InvoiceLine.line_taxes),
            selectinload(Invoice.taxes),
        )
        .where(Invoice.id == inv.id)
    )
    result = await session.execute(load_stmt)
    return _invoice_to_read(result.scalar_one())


async def create_invoice(
    session: AsyncSession,
    body: InvoiceWrite,
    company_id: uuid.UUID,
    company_currency: str,
    creator_id: uuid.UUID | None,
    *,
    commit: bool = True,
) -> InvoiceRead:
    """Create a new invoice as an unnumbered DRAFT: calculate + persist.

    No number is allocated at create; the legal number is assigned at the
    DRAFT -> SENT issue transition (see ``transition_status``).
    """
    from jai.services.document_chain import append_document_chain_event

    await set_rls_company(session, company_id)
    # Validate customer belongs to this company
    cust_stmt = select(Customer).where(
        Customer.id == body.customer_id,
        Customer.company_id == company_id,
    )
    cust_result = await session.execute(cust_stmt)
    customer = cust_result.scalar_one_or_none()
    if customer is None:
        raise ValueError("Customer not found or does not belong to this company.")

    treatment = await _resolve_treatment(session, company_id, customer, body.vat_treatment_id)
    vat_rates = await _load_vat_rates(session, company_id, body)
    await _validate_line_fks(session, company_id, body)

    inv = await _build_and_persist_invoice(
        session,
        company_id=company_id,
        company_currency=company_currency,
        creator_id=creator_id,
        body=body,
        customer=customer,
        treatment=treatment,
        vat_rates=vat_rates,
    )
    await append_document_chain_event(
        session,
        company_id=company_id,
        invoice_id=inv.id,
        actor_user_id=creator_id,
        event_type=DocumentChainEventType.INVOICE_CREATED,
        metadata={"document_kind": InvoiceDocumentKind(inv.document_kind).value},
    )

    if commit:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("Invoice number already exists (concurrent creation).") from exc
        await session.refresh(inv)
        return _invoice_to_read(inv)
    return await _load_invoice_read(session, inv)


async def get_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
) -> InvoiceRead | None:
    """Return a full InvoiceRead, or None if not found / wrong company."""
    await set_rls_company(session, company_id)
    stmt = select(Invoice).where(
        Invoice.id == invoice_id,
        Invoice.company_id == company_id,
    )
    result = await session.execute(stmt)
    inv = result.scalar_one_or_none()
    if inv is None:
        return None
    return await _load_invoice_read(session, inv)


async def update_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    body: InvoiceWrite,
    company_id: uuid.UUID,
    company_currency: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    commit: bool = True,
    require_standard_draft: bool = False,
) -> InvoiceRead | None:
    """Update an existing invoice: preserve number, recalculate, replace sub-tables."""
    from jai.services.document_chain import append_document_chain_event

    await set_rls_company(session, company_id)
    # Final edits follow the global Quote -> Invoice order.  The unlocked
    # probe is only used to discover that prefix; identity is checked again
    # once the Invoice row is locked below.
    probe = (
        await session.execute(
            select(Invoice.document_kind, Invoice.quote_id).where(
                Invoice.id == invoice_id, Invoice.company_id == company_id
            )
        )
    ).one_or_none()
    if probe is None:
        return None
    relation_probe = await session.scalar(
        select(InvoiceRelation).where(InvoiceRelation.invoice_id == invoice_id)
    )
    related_standard = (
        InvoiceDocumentKind(probe.document_kind) == InvoiceDocumentKind.STANDARD
        and relation_probe is not None
    )
    locked_quote: _Quote | None = None
    if InvoiceDocumentKind(probe.document_kind) == InvoiceDocumentKind.FINAL or (
        related_standard and probe.quote_id is not None
    ):
        if probe.quote_id is None:
            raise InvoiceLifecycleConflictError("Final invoice has no Quote provenance.")
        locked_quote = (
            await session.execute(
                select(_Quote)
                .where(_Quote.id == probe.quote_id, _Quote.company_id == company_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_quote is None:
            raise InvoiceLifecycleConflictError("Invoice Quote no longer exists.")
    if related_standard and probe.quote_id is not None:
        locked_positive = list(
            (
                await session.execute(
                    select(Invoice)
                    .where(
                        Invoice.company_id == company_id,
                        Invoice.quote_id == probe.quote_id,
                        Invoice.document_kind != InvoiceDocumentKind.CREDIT_NOTE,
                    )
                    .order_by(Invoice.id)
                    .with_for_update()
                )
            ).scalars()
        )
        inv = next((item for item in locked_positive if item.id == invoice_id), None)
    else:
        stmt = (
            select(Invoice)
            .where(
                Invoice.id == invoice_id,
                Invoice.company_id == company_id,
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        inv = result.scalar_one_or_none()
    if inv is None:
        return None
    if (
        InvoiceDocumentKind(inv.document_kind) != InvoiceDocumentKind(probe.document_kind)
        or inv.quote_id != probe.quote_id
    ):
        raise InvoiceLifecycleConflictError(
            "Invoice changed while acquiring its Quote lock prefix."
        )

    # Agent edits use the same Quote -> Invoice lock prefix and document-chain
    # validation as web updates, then narrow authority to Standard DRAFT only.
    if require_standard_draft and (
        InvoiceDocumentKind(inv.document_kind) != InvoiceDocumentKind.STANDARD
        or inv.status != InvoiceStatus.DRAFT
    ):
        raise ValueError("MCP_DRAFT_REQUIRED")

    if InvoiceDocumentKind(inv.document_kind) == InvoiceDocumentKind.FINAL:
        from jai.services.final import update_final_draft

        assert locked_quote is not None
        quote = locked_quote
        if inv.status != InvoiceStatus.DRAFT:
            raise InvoiceLifecycleConflictError("Only a DRAFT Final can be updated.")
        # Repricing replaces child line/tax rows.  Keep it inside a savepoint
        # so an uncovered bucket/date/total rejection leaves the persisted
        # Final, its applications, and its prior event stream intact even when
        # a caller keeps using this service session after the exception.
        async with session.begin_nested():
            updated = await update_final_draft(
                session,
                final=inv,
                quote=quote,
                body=body,
                company_id=company_id,
                company_currency=company_currency,
            )
            await append_document_chain_event(
                session,
                company_id=company_id,
                quote_id=quote.id,
                invoice_id=updated.id,
                actor_user_id=actor_user_id,
                event_type=DocumentChainEventType.INVOICE_UPDATED,
                metadata={"document_kind": "FINAL"},
            )
            await session.flush()
        read = await _load_invoice_read(session, updated)
        if commit:
            await session.commit()
        return read
    if InvoiceDocumentKind(inv.document_kind) != InvoiceDocumentKind.STANDARD:
        if InvoiceDocumentKind(inv.document_kind) == InvoiceDocumentKind.ADVANCE:
            raise InvoiceDedicatedUpdateRequiredError(
                "Advance invoices must be updated through the dedicated Advance endpoint."
            )
        raise ValueError("Generic invoice update currently supports STANDARD invoices only.")

    followup_relation = relation_probe
    if followup_relation is not None:
        relation_type = InvoiceRelationType(followup_relation.relation_type)
        if relation_type == InvoiceRelationType.COMPENSATES_CREDIT:
            raise InvoiceLifecycleConflictError(
                "A compensating invoice mirrors its issued Credit basis and cannot be repriced."
            )
        related_credit = await session.scalar(
            select(Invoice).where(Invoice.id == followup_relation.related_credit_note_id)
        )
        if related_credit is None:
            raise InvoiceLifecycleConflictError("Replacement Credit provenance no longer exists.")
        request_currency = body.currency or company_currency
        if (
            body.customer_id != inv.customer_id
            or request_currency != inv.currency
            or (body.vat_treatment_id is not None and body.vat_treatment_id != inv.vat_treatment_id)
        ):
            raise ValueError("Replacement customer, currency and VAT treatment are immutable.")
        if body.invoice_date < related_credit.invoice_date:
            raise ValueError("Replacement date cannot precede its Credit Note date.")

    # Only DRAFT invoices can be fully edited; SENT is a locked formal document.
    if inv.status != InvoiceStatus.DRAFT:
        raise ValueError(
            f"Cannot edit a {inv.status.value.lower()} invoice. "
            "Only DRAFT invoices may be modified."
        )

    payment_result = await session.execute(
        select(Payment).where(Payment.invoice_id == inv.id, Payment.deleted_at.is_(None))
    )
    payments = list(payment_result.scalars().all())
    quote_origin_payments = [p for p in payments if p.quote_id is not None]
    if quote_origin_payments:
        if body.customer_id != inv.customer_id:
            raise ValueError("Cannot change the customer on a draft with quote-origin payments.")
        request_currency = body.currency or company_currency
        if request_currency != inv.currency:
            raise ValueError("Cannot change the currency on a draft with quote-origin payments.")
        latest_payment_date = max(p.payment_date for p in quote_origin_payments)
        if body.invoice_date < latest_payment_date:
            raise ValueError(
                "Final invoice date cannot be earlier than an associated payment date."
            )

    # Validate customer
    cust_stmt = select(Customer).where(
        Customer.id == body.customer_id,
        Customer.company_id == company_id,
    )
    cust_result = await session.execute(cust_stmt)
    customer = cust_result.scalar_one_or_none()
    if customer is None:
        raise ValueError("Customer not found or does not belong to this company.")

    treatment = await _resolve_treatment(session, company_id, customer, body.vat_treatment_id)
    vat_rates = await _load_vat_rates(session, company_id, body)
    await _validate_line_fks(session, company_id, body)

    updated = await _build_and_persist_invoice(
        session,
        company_id=company_id,
        company_currency=company_currency,
        creator_id=inv.creator_id,
        body=body,
        customer=customer,
        treatment=treatment,
        vat_rates=vat_rates,
        invoice_number=inv.invoice_number,
        sequence_number=inv.sequence_number,
        customer_sequence_number=inv.customer_sequence_number,
        existing_invoice=inv,
    )

    payment_state = recompute_payment_state(
        Decimal(str(updated.total_incl_vat)),
        Decimal(str(updated.base_total_incl_vat)),
        payments,
        InvoiceStatus.DRAFT,
    )
    if payments:
        if payment_state.paid_total > Decimal(str(updated.total_incl_vat)):
            raise ValueError("Final invoice total cannot be lower than its associated payments.")
    _write_invoice_state(updated, payment_state)
    updated.status = InvoiceStatus.DRAFT

    if quote_origin_payments:
        await validate_invoice_tax_coverage(session, updated)

    await append_document_chain_event(
        session,
        company_id=company_id,
        quote_id=updated.quote_id,
        invoice_id=updated.id,
        actor_user_id=actor_user_id,
        event_type=DocumentChainEventType.INVOICE_UPDATED,
        metadata={"document_kind": InvoiceDocumentKind(updated.document_kind).value},
    )

    if commit:
        await session.commit()
        await session.refresh(updated)
        return _invoice_to_read(updated)
    return await _load_invoice_read(session, updated)


async def delete_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    """Delete an invoice; returns True if deleted, False if not found.

    Only DRAFT invoices may be deleted.  Raises ValueError for any other status.
    """
    from jai.services.document_chain import append_document_chain_event

    await set_rls_company(session, company_id)
    try:
        # All document mutations share Quote -> Invoice ordering.  Probe
        # without a row lock first, then lock the complete Quote prefix: the
        # Invoice's own provenance (Formal Advance and converted Standards)
        # plus M11.5 quote-origin payment provenance.
        probe = (
            await session.execute(
                select(Invoice.document_kind, Invoice.quote_id).where(
                    Invoice.id == invoice_id,
                    Invoice.company_id == company_id,
                )
            )
        ).one_or_none()
        if probe is None:
            return False
        source_quote_ids_result = await session.execute(
            select(Payment.quote_id)
            .where(
                Payment.invoice_id == invoice_id,
                Payment.company_id == company_id,
                Payment.quote_id.is_not(None),
            )
            .distinct()
        )
        quote_ids = {
            row.quote_id for row in source_quote_ids_result.all() if row.quote_id is not None
        }
        if probe.quote_id is not None:
            quote_ids.add(probe.quote_id)
        ordered_quote_ids = sorted(quote_ids)
        if ordered_quote_ids:
            await session.execute(
                select(_Quote)
                .where(
                    _Quote.id.in_(ordered_quote_ids),
                    _Quote.company_id == company_id,
                )
                .order_by(_Quote.id)
                .with_for_update()
            )

        inv = (
            await session.execute(
                select(Invoice)
                .where(
                    Invoice.id == invoice_id,
                    Invoice.company_id == company_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if inv is None:
            return False
        # The discovery read is deliberately unlocked; never act on it without
        # rechecking identity/provenance after the Invoice lock is held.
        if (
            InvoiceDocumentKind(inv.document_kind) != InvoiceDocumentKind(probe.document_kind)
            or inv.quote_id != probe.quote_id
        ):
            raise InvoiceLifecycleConflictError(
                "Invoice changed while acquiring its Quote lock prefix."
            )
        if InvoiceDocumentKind(inv.document_kind) not in {
            InvoiceDocumentKind.STANDARD,
            InvoiceDocumentKind.ADVANCE,
            InvoiceDocumentKind.FINAL,
            InvoiceDocumentKind.CREDIT_NOTE,
        }:
            raise ValueError(
                "This document kind cannot be deleted by the generic invoice endpoint."
            )
        if inv.status != InvoiceStatus.DRAFT:
            raise ValueError(
                f"Cannot delete a {inv.status.value.lower()} invoice. "
                "Only DRAFT invoices may be deleted."
            )
        await append_document_chain_event(
            session,
            company_id=company_id,
            quote_id=inv.quote_id,
            invoice_id=inv.id,
            actor_user_id=actor_user_id,
            event_type=DocumentChainEventType.INVOICE_DELETED,
            metadata={"document_kind": InvoiceDocumentKind(inv.document_kind).value},
        )
        await session.delete(inv)
        await session.commit()
        return True
    except DBAPIError as exc:
        await session.rollback()
        from jai.services.advance import AdvanceConflictError, is_retryable_transaction_conflict

        if is_retryable_transaction_conflict(exc):
            raise AdvanceConflictError(
                "Concurrent Formal Advance mutation; retry the command."
            ) from exc
        raise


async def list_invoices(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    q: str | None = None,
    customer_id: uuid.UUID | None = None,
    status: str | None = None,
    paid_status: str | None = None,
    document_kind: InvoiceDocumentKind | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "invoice_date",
) -> InvoiceListResponse:
    """Return a paginated list of invoices for the company."""
    await set_rls_company(session, company_id)
    # Always join Customer so we can search by customer name and return it.
    base = (
        select(Invoice)
        .join(Customer, Invoice.customer_id == Customer.id)
        .where(Invoice.company_id == company_id)
    )

    if q:
        like = f"%{q}%"
        base = base.where(
            or_(
                Invoice.invoice_number.ilike(like),
                Invoice.reference_number.ilike(like),
                Customer.name.ilike(like),
            )
        )
    if customer_id is not None:
        base = base.where(Invoice.customer_id == customer_id)
    if status:
        base = base.where(Invoice.status == status)
    if paid_status:
        base = base.where(Invoice.paid_status == paid_status)
    if document_kind is not None:
        base = base.where(Invoice.document_kind == document_kind)
    if date_from:
        base = base.where(Invoice.invoice_date >= date_from)
    if date_to:
        base = base.where(Invoice.invoice_date <= date_to)

    # Count
    count_stmt = select(func.count()).select_from(base.subquery())
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    # Sort
    sort_col = {
        "invoice_date": Invoice.invoice_date,
        "created_at": Invoice.created_at,
        "invoice_number": Invoice.sequence_number,
    }.get(sort_by, Invoice.invoice_date)

    data_stmt = (
        base.options(
            selectinload(Invoice.party_snapshot),
            selectinload(Invoice.correction),
            selectinload(Invoice.positive_relations),
        )
        .order_by(sort_col.desc())
        .limit(limit)
        .offset(offset)
    )
    data_result = await session.execute(data_stmt)
    rows = list(data_result.scalars().all())

    # Batch-load customer names in a single secondary query.
    customer_name_map: dict[uuid.UUID, str] = {}
    if rows:
        cust_ids = list({inv.customer_id for inv in rows})
        cust_result = await session.execute(
            select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))
        )
        customer_name_map = {cid: name for cid, name in cust_result.all()}

    return InvoiceListResponse(
        items=[
            _invoice_to_list_item(r, customer_name=customer_name_map.get(r.customer_id, ""))
            for r in rows
        ],
        total=total,
    )


async def transition_status(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    body: InvoiceStatusWrite,
    *,
    issued_by_user_id: uuid.UUID | None = None,
) -> InvoiceRead | None:
    """Transition invoice lifecycle status.

    M5 allowed transitions:
    - DRAFT → SENT  (allocates the legal invoice number on issue, if not yet set)
    - DRAFT → CANCELLED
    - CANCELLED → DRAFT  (reactivate a mistakenly-cancelled draft)
    SENT is a lock state: use a credit note (M9/M10) to reverse a sent invoice.
    COMPLETED cannot be set manually (M7 drives it via payments).

    Numbering (red line 4): the invoice number is allocated here at the
    DRAFT -> SENT issue transition (not at create), within this same
    transaction and before commit, via the row-locked
    ``allocate_invoice_number``.  Allocation is idempotent — an invoice that
    already carries a number (e.g. re-issued after CANCELLED -> DRAFT) is never
    re-numbered.
    """
    from jai.services.document_chain import append_document_chain_event

    await set_rls_company(session, company_id)
    # Discover the quote without locking first.  Formal issue then follows the
    # global Quote -> Invoice order; Standard documents retain their legacy
    # direct invoice lock path.
    probe = (
        await session.execute(
            select(Invoice.document_kind, Invoice.quote_id).where(
                Invoice.id == invoice_id, Invoice.company_id == company_id
            )
        )
    ).one_or_none()
    if probe is None:
        return None
    # These values are deliberately copied before any subsequent await, lock,
    # or possible autoflush.  A failed flush invalidates ORM state before this
    # function reaches its IntegrityError handler, so that handler must never
    # dereference ``probe`` or ``inv`` to decide how to map the original error.
    probe_kind = InvoiceDocumentKind(probe.document_kind)
    probe_quote_id = probe.quote_id
    if probe_kind == InvoiceDocumentKind.CREDIT_NOTE:
        return await _transition_credit_status(
            session,
            invoice_id=invoice_id,
            company_id=company_id,
            body=body,
            issued_by_user_id=issued_by_user_id,
        )
    related_standard = (
        probe_kind == InvoiceDocumentKind.STANDARD
        and await session.scalar(
            select(InvoiceRelation.id).where(InvoiceRelation.invoice_id == invoice_id)
        )
        is not None
    )
    locked_invoices: list[Invoice] = []
    if probe_kind in {InvoiceDocumentKind.ADVANCE, InvoiceDocumentKind.FINAL} or (
        related_standard and probe_quote_id is not None
    ):
        if probe_quote_id is None:
            raise InvoiceLifecycleConflictError("Formal invoice has no Quote provenance.")
        try:
            quote = (
                await session.execute(
                    select(_Quote)
                    .where(_Quote.id == probe_quote_id, _Quote.company_id == company_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
        except DBAPIError as exc:
            await session.rollback()
            from jai.services.advance import AdvanceConflictError, is_retryable_transaction_conflict

            if is_retryable_transaction_conflict(exc):
                raise AdvanceConflictError(
                    "Concurrent invoice lifecycle mutation; retry the command."
                ) from exc
            raise
        if quote is None:
            raise InvoiceLifecycleConflictError("Follow-up Quote no longer exists.")
        if (
            probe_kind in {InvoiceDocumentKind.ADVANCE, InvoiceDocumentKind.FINAL}
            and quote.settlement_mode.value != "FORMAL_ADVANCE"
        ):
            raise InvoiceLifecycleConflictError("Formal Quote is not in FORMAL_ADVANCE mode.")
    try:
        if probe_kind == InvoiceDocumentKind.FINAL:
            # The Quote lock is the first prefix.  Lock the whole formal
            # charge/source set once, in canonical Invoice.id order, before
            # selecting the target Final from that already locked set.
            from jai.services.final import lock_formal_charge_invoices

            assert quote is not None
            locked_invoices = await lock_formal_charge_invoices(session, quote)
            inv = next((item for item in locked_invoices if item.id == invoice_id), None)
        elif related_standard and probe_quote_id is not None:
            # A supplemental/replacement Standard belongs to its correction
            # chain.  It must honor the same Quote -> sorted positive-source
            # prefix as cancellation and follow-up creation.
            locked_invoices = list(
                (
                    await session.execute(
                        select(Invoice)
                        .where(
                            Invoice.company_id == company_id,
                            Invoice.quote_id == probe_quote_id,
                            Invoice.document_kind != InvoiceDocumentKind.CREDIT_NOTE,
                        )
                        .order_by(Invoice.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            inv = next((item for item in locked_invoices if item.id == invoice_id), None)
        else:
            stmt = (
                select(Invoice)
                .where(
                    Invoice.id == invoice_id,
                    Invoice.company_id == company_id,
                )
                .with_for_update()
            )
            result = await session.execute(stmt)
            inv = result.scalar_one_or_none()
    except DBAPIError as exc:
        await session.rollback()
        from jai.services.advance import AdvanceConflictError, is_retryable_transaction_conflict

        if is_retryable_transaction_conflict(exc):
            raise AdvanceConflictError(
                "Concurrent invoice lifecycle mutation; retry the command."
            ) from exc
        raise
    if inv is None:
        return None
    if InvoiceDocumentKind(inv.document_kind) != probe_kind or inv.quote_id != probe_quote_id:
        raise InvoiceLifecycleConflictError(
            "Invoice changed while acquiring its Quote lock prefix."
        )

    if probe_kind not in {
        InvoiceDocumentKind.STANDARD,
        InvoiceDocumentKind.ADVANCE,
        InvoiceDocumentKind.FINAL,
    }:
        raise InvoiceLifecycleConflictError(
            "Generic invoice lifecycle currently supports STANDARD invoices only."
        )

    current_status = InvoiceStatus(inv.status)
    new_status = body.status

    if probe_kind == InvoiceDocumentKind.FINAL and (
        (current_status == InvoiceStatus.DRAFT and new_status == InvoiceStatus.CANCELLED)
        or (current_status == InvoiceStatus.CANCELLED and new_status == InvoiceStatus.DRAFT)
    ):
        raise FinalLifecycleConflictError(
            "Final DRAFT lifecycle is frozen; delete the DRAFT to remove its Advance freeze."
        )

    if (
        probe_kind == InvoiceDocumentKind.ADVANCE
        and current_status == InvoiceStatus.CANCELLED
        and new_status == InvoiceStatus.DRAFT
    ):
        # The Quote lock acquired above serializes status resurrection with
        # Advance create/delete/issue.  The partial unique index added in 0034
        # remains the final protection for direct/concurrent database writes.
        from jai.services.advance import AdvanceConflictError

        try:
            other_open_draft = (
                await session.execute(
                    select(Invoice.id)
                    .where(
                        Invoice.quote_id == probe_quote_id,
                        Invoice.document_kind == InvoiceDocumentKind.ADVANCE,
                        Invoice.status == InvoiceStatus.DRAFT,
                        Invoice.id != inv.id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
        except DBAPIError as exc:
            await session.rollback()
            from jai.services.advance import AdvanceConflictError, is_retryable_transaction_conflict

            if is_retryable_transaction_conflict(exc):
                raise AdvanceConflictError(
                    "Concurrent invoice lifecycle mutation; retry the command."
                ) from exc
            raise
        if other_open_draft is not None:
            raise AdvanceConflictError("Only one open Advance DRAFT is allowed per Quote.")

    payment_result = await session.execute(
        select(Payment)
        .where(Payment.invoice_id == inv.id, Payment.deleted_at.is_(None))
        .order_by(Payment.payment_date, Payment.created_at, Payment.id)
    )
    payments = list(payment_result.scalars().all())

    if current_status == InvoiceStatus.DRAFT and new_status == InvoiceStatus.CANCELLED and payments:
        raise InvoiceLifecycleConflictError(
            "Cannot cancel a draft invoice that has payments. Delete the draft "
            "to return quote-origin payments, or handle the payments first."
        )

    if new_status == InvoiceStatus.COMPLETED:
        raise InvoiceLifecycleConflictError(
            "COMPLETED status is set automatically by payments (M7). "
            "It cannot be assigned manually in M5."
        )

    allowed = _ALLOWED_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvoiceLifecycleConflictError(
            f"Cannot transition from {current_status} to {new_status}. "
            f"Allowed transitions: {', '.join(s.value for s in allowed) or 'none'}."
        )

    # Allocate the legal number on issue (DRAFT -> SENT), only if not yet set.
    needs_number = (
        current_status == InvoiceStatus.DRAFT
        and new_status == InvoiceStatus.SENT
        and inv.invoice_number is None
    )

    try:
        if current_status == InvoiceStatus.DRAFT and new_status == InvoiceStatus.SENT:
            if probe_kind == InvoiceDocumentKind.ADVANCE:
                # Imported lazily to keep the dedicated Advance service's
                # serializer dependency acyclic.  The Quote row is already
                # held above, before this document lock.
                from jai.services.advance import validate_advance_issue

                assert probe_quote_id is not None
                assert quote is not None
                await validate_advance_issue(session, quote=quote, invoice=inv)
            elif probe_kind == InvoiceDocumentKind.FINAL:
                from jai.services.final import validate_final_issue

                assert quote is not None
                await validate_final_issue(
                    session,
                    quote=quote,
                    final=inv,
                    locked_invoices=locked_invoices,
                )
        inv.status = new_status
        if current_status == InvoiceStatus.DRAFT and new_status == InvoiceStatus.SENT:
            charge = (
                Decimal(str(inv.payable_before_payments))
                if probe_kind == InvoiceDocumentKind.FINAL
                else Decimal(str(inv.total_incl_vat))
            )
            payment_state = recompute_payment_state(
                charge,
                charge,
                payments,
                InvoiceStatus.SENT,
            )
            inv.due_amount = payment_state.due_amount
            inv.base_due_amount = payment_state.base_due_amount
            inv.paid_status = payment_state.paid_status
            inv.status = payment_state.new_status
            inv.payable_before_payments = charge
            inv.incoming_payment_total = payment_state.paid_total
            inv.base_payable_before_payments = charge
            inv.base_incoming_payment_total = payment_state.base_paid_total
            inv.settlement_status = (
                InvoiceSettlementStatus.SETTLED
                if payment_state.paid_status == InvoicePaidStatus.PAID
                else (
                    InvoiceSettlementStatus.PARTIALLY_SETTLED
                    if payment_state.paid_status == InvoicePaidStatus.PARTIALLY_PAID
                    else InvoiceSettlementStatus.OPEN
                )
            )
            await _create_native_issue_foundation(session, inv, issued_by_user_id=issued_by_user_id)
        if needs_number:
            numbering_config = await _load_numbering_config(session, company_id)
            cust_stmt = select(Customer).where(
                Customer.id == inv.customer_id,
                Customer.company_id == company_id,
            )
            cust_result = await session.execute(cust_stmt)
            customer = cust_result.scalar_one()
            (
                inv.invoice_number,
                inv.sequence_number,
                inv.customer_sequence_number,
            ) = await allocate_invoice_number(
                session,
                company_id,
                inv.customer_id,
                inv.invoice_date,
                numbering_config=numbering_config,
                customer_invoice_prefix=customer.invoice_prefix,
            )
        event_type = (
            DocumentChainEventType.INVOICE_ISSUED
            if current_status == InvoiceStatus.DRAFT and new_status == InvoiceStatus.SENT
            else DocumentChainEventType.INVOICE_STATUS_CHANGED
        )
        await append_document_chain_event(
            session,
            company_id=company_id,
            quote_id=probe_quote_id,
            invoice_id=inv.id,
            actor_user_id=issued_by_user_id,
            event_type=event_type,
            metadata={
                "document_kind": probe_kind.value,
                "status": InvoiceStatus(inv.status).value,
            },
        )
        # Build the response while this transaction still carries the RLS GUC.
        # A post-commit refresh would begin a new transaction with an empty
        # setting and can turn a successful legal issue into a client 500.
        await session.flush()
        read = await _load_invoice_read(session, inv)
        await session.commit()
    except NumberSequenceExhaustedError:
        # This command owns the issue transaction.  Roll it back here so a
        # caller that catches the domain error can safely reuse its session and
        # no status/snapshot/counter mutation is left pending.
        await session.rollback()
        raise
    except IntegrityError as exc:
        # Capture only ordinary values and exact database classification before
        # rollback expires ORM attributes.  In particular, a CHECK/FK/NOT NULL
        # violation must remain its original error rather than masquerading as
        # the Formal Advance partial-unique conflict.
        from jai.services.advance import is_advance_draft_conflict, is_invoice_number_conflict

        advance_draft_conflict = is_advance_draft_conflict(exc)
        invoice_number_conflict = is_invoice_number_conflict(exc)
        await session.rollback()
        if probe_kind == InvoiceDocumentKind.ADVANCE and advance_draft_conflict:
            from jai.services.advance import AdvanceConflictError

            raise AdvanceConflictError("Only one open Advance DRAFT is allowed per Quote.") from exc
        if invoice_number_conflict:
            raise ValueError("Invoice number already exists (concurrent issue).") from exc
        raise
    return read


async def _transition_credit_status(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    body: InvoiceStatusWrite,
    issued_by_user_id: uuid.UUID | None,
) -> InvoiceRead | None:
    """Issue a Credit in global source -> Credit -> numbering lock order."""
    from jai.models.document import InvoiceCorrection
    from jai.services.credit import CreditConflictError, issue_credit
    from jai.services.document_chain import append_document_chain_event

    source_id = await session.scalar(
        select(InvoiceCorrection.source_invoice_id)
        .join(Invoice, Invoice.id == InvoiceCorrection.credit_note_id)
        .where(Invoice.id == invoice_id, Invoice.company_id == company_id)
    )
    if source_id is None:
        return None
    try:
        context = await lock_settlement_chain(
            session, anchor_invoice_id=source_id, company_id=company_id
        )
        credit = next((item for item in context.credits if item.id == invoice_id), None)
        if credit is None:
            return None
        source = next((item for item in context.chain_sources if item.id == source_id), None)
        correction = next(
            (
                item
                for item in context.corrections
                if item.credit_note_id == invoice_id and item.source_invoice_id == source_id
            ),
            None,
        )
        if source is None or correction is None:
            raise CreditConflictError("Credit source changed while acquiring settlement locks.")
        current_status = InvoiceStatus(credit.status)
        if current_status == InvoiceStatus.DRAFT and body.status == InvoiceStatus.SENT:
            await issue_credit(
                session,
                credit=credit,
                company_id=company_id,
                actor_user_id=issued_by_user_id,
                locked_source=source,
                locked_correction=correction,
            )
            _settle_refund_chain(
                source=source,
                chain_sources=context.chain_sources,
                credits=context.credits,
                credit_source_ids=context.credit_source_ids,
                payments=context.payments,
                quote=context.quote,
            )
        elif (current_status, body.status) in {
            (InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED),
            (InvoiceStatus.CANCELLED, InvoiceStatus.DRAFT),
        }:
            credit.status = body.status
            await append_document_chain_event(
                session,
                company_id=company_id,
                quote_id=credit.quote_id,
                invoice_id=credit.id,
                actor_user_id=issued_by_user_id,
                event_type=DocumentChainEventType.INVOICE_STATUS_CHANGED,
                metadata={
                    "document_kind": InvoiceDocumentKind.CREDIT_NOTE.value,
                    "status": body.status.value,
                },
            )
        else:
            raise InvoiceLifecycleConflictError(
                "Credit Notes permit only DRAFT -> SENT and DRAFT <-> CANCELLED."
            )
        read = await _load_invoice_read(session, credit)
        await session.commit()
        return read
    except CreditConflictError:
        await session.rollback()
        raise
    except NumberSequenceExhaustedError:
        await session.rollback()
        raise
    except IntegrityError:
        await session.rollback()
        raise CreditConflictError("Concurrent Credit issue conflict; retry.") from None
    except DBAPIError as exc:
        await session.rollback()
        from jai.services.advance import AdvanceConflictError, is_retryable_transaction_conflict

        if is_retryable_transaction_conflict(exc):
            raise AdvanceConflictError(
                "Concurrent invoice lifecycle mutation; retry the command."
            ) from exc
        raise


async def list_invoice_product_options(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    q: str | None = None,
    limit: int = 20,
) -> ProductInvoiceOptionListResponse:
    """Return customer-safe product options for invoice line auto-fill.

    Explicitly excludes: purchase_cost_excl_vat, margin_rate, supplier, extra.
    """
    stmt = (
        select(Product, Unit)
        .outerjoin(Unit, Product.unit_id == Unit.id)
        .where(
            Product.company_id == company_id,
            Product.active == True,  # noqa: E712
        )
    )

    if q:
        stmt = stmt.where(Product.name.ilike(f"%{q}%"))

    stmt = stmt.order_by(Product.name).limit(limit)
    result = await session.execute(stmt)
    rows = result.all()

    items = [
        ProductInvoiceOptionRead(
            id=product.id,
            name=product.name,
            unit_id=product.unit_id,
            unit_name=unit.name if unit is not None else None,
            default_vat_rate_id=product.default_vat_rate_id,
        )
        for product, unit in rows
    ]
    return ProductInvoiceOptionListResponse(items=items)
