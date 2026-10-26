"""Read-only SI2 history and eligibility projections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jai.db import set_rls_company
from jai.models._enums import (
    EmailRelatedType,
    EmailStatus,
    InvoiceDocumentKind,
    InvoiceStatus,
    InvoiceTaxMode,
    QuoteSettlementMode,
    VatTreatmentEffect,
    VatTreatmentSide,
)
from jai.models.dictionary import Unit
from jai.models.document import (
    FinalAdvanceApplication,
    InvoiceCorrection,
    InvoiceRelation,
    InvoiceRevision,
)
from jai.models.email_log import EmailLog
from jai.models.invoice import Invoice, InvoiceLine
from jai.models.payment import Payment
from jai.models.product import Product
from jai.models.quote import Quote
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.invoice import (
    DiscountInput,
    InvoiceCalculationRead,
    InvoiceLineInput,
    InvoiceRevisionEligibilityRead,
    VatTreatmentSnapshot,
)
from jai.schemas.invoice_revision import (
    InvoiceRevisionCalculateRequest,
    InvoiceRevisionChanges,
    InvoiceRevisionListRead,
    InvoiceRevisionRead,
)
from jai.services.document_actions import invoice_revision_eligibility
from jai.services.pricing import compute_pricing


class InvoiceRevisionConflictError(Exception):
    """A current business condition makes revision preview unavailable."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class InvoiceRevisionStaleError(Exception):
    """The preview was based on an obsolete Invoice version."""




@dataclass(frozen=True)
class MergedInvoiceRevision:
    """Validated target state shared by revision preview and the later Save step."""

    invoice_date: date
    due_date: date | None
    supply_or_advance_date: date | None
    reference_number: str | None
    notes: str | None
    warranty_text: str | None
    terms_text: str | None
    bank_text: str | None
    payment_terms_text: str | None
    tax_mode: InvoiceTaxMode
    amounts_include_vat: bool
    vat_treatment_id: uuid.UUID
    document_vat_rate_id: uuid.UUID | None
    discount: DiscountInput
    lines: list[tuple[InvoiceLine | None, InvoiceLineInput]]


def merge_invoice_revision_changes(
    invoice: Invoice, changes: InvoiceRevisionChanges
) -> MergedInvoiceRevision:
    """Apply the closed patch and retained-line identities without side effects."""
    fields = changes.model_fields_set
    old_lines = sorted(invoice.lines, key=lambda row: (row.sort_order, str(row.id)))
    by_id = {line.id: line for line in old_lines}
    lines: list[tuple[InvoiceLine | None, InvoiceLineInput]]
    if "lines" in fields:
        lines = []
        seen: set[uuid.UUID] = set()
        for entry in changes.lines or []:
            existing = None
            if entry.line_id is not None:
                if entry.line_id in seen:
                    raise ValueError("Duplicate line_id in revision lines.")
                seen.add(entry.line_id)
                existing = by_id.get(entry.line_id)
                if existing is None:
                    raise ValueError("Unknown line_id for this Invoice.")
            lines.append(
                (
                    existing,
                    InvoiceLineInput.model_validate(entry.model_dump(exclude={"line_id"})),
                )
            )
    else:
        lines = [(line, _line_input(line)) for line in old_lines]
    discount = (
        changes.discount
        if "discount" in fields
        else DiscountInput(type=invoice.discount_type, value=Decimal(str(invoice.discount_value)))
    )
    invoice_date = changes.invoice_date if "invoice_date" in fields else invoice.invoice_date
    tax_mode = changes.tax_mode if "tax_mode" in fields else invoice.tax_mode
    include_vat = (
        changes.amounts_include_vat
        if "amounts_include_vat" in fields
        else invoice.amounts_include_vat
    )
    treatment_id = (
        changes.vat_treatment_id if "vat_treatment_id" in fields else invoice.vat_treatment_id
    )
    if invoice_date is None or tax_mode is None or include_vat is None or treatment_id is None:
        raise ValueError("Required revision fields cannot be null.")
    if discount is None:
        raise ValueError("discount cannot be null.")
    return MergedInvoiceRevision(
        invoice_date=invoice_date,
        due_date=changes.due_date if "due_date" in fields else invoice.due_date,
        supply_or_advance_date=(
            changes.supply_or_advance_date
            if "supply_or_advance_date" in fields
            else invoice.supply_or_advance_date
        ),
        reference_number=(
            changes.reference_number
            if "reference_number" in fields
            else invoice.reference_number
        ),
        notes=changes.notes if "notes" in fields else invoice.notes,
        warranty_text=changes.warranty_text if "warranty_text" in fields else invoice.warranty_text,
        terms_text=changes.terms_text if "terms_text" in fields else invoice.terms_text,
        bank_text=changes.bank_text if "bank_text" in fields else invoice.bank_text,
        payment_terms_text=(
            changes.payment_terms_text
            if "payment_terms_text" in fields
            else invoice.payment_terms_text
        ),
        tax_mode=tax_mode,
        amounts_include_vat=include_vat,
        vat_treatment_id=treatment_id,
        document_vat_rate_id=(
            changes.document_vat_rate_id
            if "document_vat_rate_id" in fields
            else invoice.document_vat_rate_id
        ),
        discount=discount,
        lines=lines,
    )


def _line_input(line: InvoiceLine) -> InvoiceLineInput:
    return InvoiceLineInput(
        product_id=line.product_id,
        name=line.name,
        description=line.description,
        quantity=Decimal(str(line.quantity)),
        unit_id=line.unit_id,
        unit_name=line.unit_name,
        unit_price=Decimal(str(line.unit_price)),
        discount=DiscountInput(type=line.discount_type, value=Decimal(str(line.discount_value))),
        vat_rate_id=line.vat_rate_id,
    )


def _stored_calculation(invoice: Invoice, lines: list[InvoiceLine]) -> InvoiceCalculationRead:
    """Project persisted pricing snapshots exactly, preserving legacy precision."""
    from jai.schemas.invoice import InvoiceLineCalculationRead, InvoiceLineTaxRead, InvoiceTaxRead

    result = InvoiceCalculationRead(
        tax_mode=invoice.tax_mode,
        amounts_include_vat=invoice.amounts_include_vat,
        discount_type=invoice.discount_type,
        discount_value=Decimal(str(invoice.discount_value)),
        vat_treatment_snapshot=VatTreatmentSnapshot(
            id=invoice.vat_treatment_id,
            code=invoice.vat_treatment_code,
            label=invoice.vat_treatment_label,
            effect=invoice.vat_treatment_effect,
            requires_icp=invoice.vat_treatment_requires_icp,
        ),
        subtotal_excl_vat=Decimal(str(invoice.subtotal_excl_vat)),
        line_discount_total=Decimal(str(invoice.line_discount_total)),
        document_discount_amount=Decimal(str(invoice.document_discount_amount)),
        taxable_amount=Decimal(str(invoice.taxable_amount)),
        vat_total=Decimal(str(invoice.vat_total)),
        total_incl_vat=Decimal(str(invoice.total_incl_vat)),
        lines=[],
        line_taxes=[],
        document_taxes=[],
    )
    for line in lines:
        result.lines.append(
            InvoiceLineCalculationRead(
                product_id=line.product_id,
                name=line.name,
                description=line.description,
                quantity=Decimal(str(line.quantity)),
                unit_id=line.unit_id,
                unit_name=line.unit_name,
                unit_price=Decimal(str(line.unit_price)),
                discount_type=line.discount_type,
                discount_value=Decimal(str(line.discount_value)),
                vat_rate_id=line.vat_rate_id,
                vat_rate_label=line.vat_rate_label,
                vat_rate_percent=(
                    Decimal(str(line.vat_rate_percent))
                    if line.vat_rate_percent is not None
                    else None
                ),
                subtotal_excl_vat=Decimal(str(line.subtotal_excl_vat)),
                subtotal_incl_vat=Decimal(str(line.subtotal_incl_vat)),
                line_discount_amount=Decimal(str(line.line_discount_amount)),
                document_discount_share=Decimal(str(line.document_discount_share)),
                taxable_amount=Decimal(str(line.taxable_amount)),
                vat_total=Decimal(str(line.vat_total)),
                total_incl_vat=Decimal(str(line.total_incl_vat)),
            )
        )
        result.line_taxes.extend(
            InvoiceLineTaxRead(
                vat_rate_id=tax.vat_rate_id,
                vat_rate_label=tax.vat_rate_label,
                vat_rate_percent=Decimal(str(tax.vat_rate_percent)),
                effective_vat_percent=Decimal(str(tax.effective_vat_percent)),
                taxable_amount=Decimal(str(tax.taxable_amount)),
                tax_amount=Decimal(str(tax.tax_amount)),
            )
            for tax in line.line_taxes
        )
    result.document_taxes = [
        InvoiceTaxRead(
            vat_rate_id=tax.vat_rate_id,
            vat_rate_label=tax.vat_rate_label,
            vat_rate_percent=Decimal(str(tax.vat_rate_percent)),
            effective_vat_percent=Decimal(str(tax.effective_vat_percent)),
            taxable_amount=Decimal(str(tax.taxable_amount)),
            tax_amount=Decimal(str(tax.tax_amount)),
        )
        for tax in invoice.taxes
    ]
    return result


async def calculate_invoice_revision(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    request: InvoiceRevisionCalculateRequest,
) -> InvoiceCalculationRead | None:
    """Consistently read, merge and preview a revision without persistence."""
    await set_rls_company(session, company_id)
    invoice = (
        (
            await session.scalars(
                select(Invoice)
                .where(Invoice.id == invoice_id, Invoice.company_id == company_id)
                .options(
                    selectinload(Invoice.lines).selectinload(InvoiceLine.line_taxes),
                    selectinload(Invoice.taxes),
                )
                .with_for_update()
            )
        )
        .unique()
        .one_or_none()
    )
    if invoice is None:
        return None
    if invoice.updated_at != request.expected_updated_at:
        raise InvoiceRevisionStaleError("STALE_VERSION")
    eligibility = await get_revision_eligibility(session, invoice, company_id)
    if not eligibility.available:
        raise InvoiceRevisionConflictError(eligibility.reason_code or "REVISION_UNAVAILABLE")
    changes = request.changes
    fields = changes.model_fields_set
    old_lines = sorted(invoice.lines, key=lambda row: (row.sort_order, str(row.id)))
    merged = merge_invoice_revision_changes(invoice, changes)
    target = merged.lines
    tax_mode = merged.tax_mode
    include_vat = merged.amounts_include_vat
    discount = merged.discount
    treatment_id = merged.vat_treatment_id
    document_rate_id = merged.document_vat_rate_id
    merged_invoice_date = merged.invoice_date
    merged_due_date = merged.due_date
    if merged_due_date is not None and merged_due_date < merged_invoice_date:
        raise ValueError("due_date cannot be before invoice_date.")
    if (
        "reference_number" in fields
        and changes.reference_number is not None
        and not changes.reference_number.strip()
    ):
        raise ValueError("reference_number must not be blank.")
    treatment = None
    if treatment_id == invoice.vat_treatment_id:
        treatment_snapshot = VatTreatmentSnapshot(
            id=invoice.vat_treatment_id,
            code=invoice.vat_treatment_code,
            label=invoice.vat_treatment_label,
            effect=invoice.vat_treatment_effect,
            requires_icp=invoice.vat_treatment_requires_icp,
        )
        treatment_effect = VatTreatmentEffect(invoice.vat_treatment_effect)
    else:
        treatment = await session.scalar(
            select(VatTreatment).where(
                VatTreatment.id == treatment_id,
                VatTreatment.company_id == company_id,
                VatTreatment.side == VatTreatmentSide.SALES,
                VatTreatment.active.is_(True),
            )
        )
        if treatment is None:
            raise ValueError("VAT treatment not found, inactive, or not a sales treatment.")
        treatment_effect = VatTreatmentEffect(treatment.effect)
        treatment_snapshot = VatTreatmentSnapshot(
            id=treatment.id,
            code=treatment.code,
            label=treatment.label,
            effect=str(
                treatment.effect.value if hasattr(treatment.effect, "value") else treatment.effect
            ),
            requires_icp=treatment.requires_icp,
        )

    def raw_line_key(line: InvoiceLineInput) -> tuple[object, ...]:
        return (
            line.quantity,
            line.unit_price,
            line.discount.type,
            line.discount.value,
            line.vat_rate_id,
        )

    same_raw = len(target) == len(old_lines) and all(
        prior is not None and raw_line_key(raw) == raw_line_key(_line_input(prior))
        for prior, raw in target
    )
    same_order = [prior.id if prior is not None else None for prior, _ in target] == [
        row.id for row in old_lines
    ]
    financial_changed = (
        tax_mode != invoice.tax_mode
        or include_vat != invoice.amounts_include_vat
        or treatment_id != invoice.vat_treatment_id
        or document_rate_id != invoice.document_vat_rate_id
        or discount.type != invoice.discount_type
        or discount.value != Decimal(str(invoice.discount_value))
        or not same_raw
        or not same_order
    )
    product_ids = {raw.product_id for _, raw in target if raw.product_id is not None}
    unit_ids = {raw.unit_id for _, raw in target if raw.unit_id is not None}
    for model, ids, message in (
        (
            Product,
            product_ids,
            "One or more product IDs not found or do not belong to this company.",
        ),
        (Unit, unit_ids, "One or more unit IDs not found or do not belong to this company."),
    ):
        if ids:
            found = await session.scalar(
                select(func.count()).select_from(model).where(
                    model.id.in_(ids), model.company_id == company_id
                )
            )
            if found != len(ids):
                raise ValueError(message)
    if not financial_changed:
        # Reorder output snapshots by requested array while preserving exact values.
        reordered = [prior for prior, _ in target]
        if all(prior is not None for prior in reordered):
            response = _stored_calculation(
                invoice, [prior for prior in reordered if prior is not None]
            )
            for row, (_, raw) in zip(response.lines, target, strict=True):
                row.product_id, row.name, row.description = (
                    raw.product_id,
                    raw.name,
                    raw.description,
                )
                row.quantity, row.unit_id, row.unit_name = raw.quantity, raw.unit_id, raw.unit_name
                row.unit_price = raw.unit_price
            return response
        return _stored_calculation(invoice, old_lines)

    if tax_mode != invoice.tax_mode and tax_mode is None:
        raise ValueError("tax_mode cannot be null")
    rate_ids = {raw.vat_rate_id for _, raw in target if raw.vat_rate_id is not None}
    if document_rate_id is not None and (
        tax_mode == InvoiceTaxMode.DOCUMENT
        or document_rate_id != invoice.document_vat_rate_id
    ):
        rate_ids.add(document_rate_id)
    rates = (
        (
            await session.scalars(
                select(VatRate).where(VatRate.id.in_(rate_ids), VatRate.company_id == company_id)
            )
        ).all()
        if rate_ids
        else []
    )
    rate_map = {row.id: (row.label, Decimal(str(row.percent))) for row in rates}
    active_rate_ids = {row.id for row in rates if row.active}
    if tax_mode == InvoiceTaxMode.DOCUMENT and document_rate_id is None:
        raise ValueError("document_vat_rate_id is required in DOCUMENT tax mode.")
    if (
        document_rate_id is not None
        and document_rate_id != invoice.document_vat_rate_id
        and document_rate_id not in active_rate_ids
    ):
        raise ValueError("Document VAT rate not found or inactive.")
    line_overrides: list[tuple[str, Decimal] | None] = []
    for prior, raw in target:
        if (
            prior is not None
            and tax_mode == InvoiceTaxMode.LINE
            and raw.vat_rate_id == prior.vat_rate_id
            and prior.vat_rate_label is not None
            and prior.vat_rate_percent is not None
        ):
            line_overrides.append((prior.vat_rate_label, Decimal(str(prior.vat_rate_percent))))
        else:
            line_overrides.append(None)
    document_rate = None
    if tax_mode == InvoiceTaxMode.DOCUMENT:
        assert document_rate_id is not None
        if document_rate_id == invoice.document_vat_rate_id and invoice.taxes:
            document_rate = (
                invoice.taxes[0].vat_rate_label,
                Decimal(str(invoice.taxes[0].vat_rate_percent)),
            )
        else:
            document_rate = rate_map.get(document_rate_id)
        if document_rate is None:
            raise ValueError("Document VAT rate not found.")
    for prior, raw in target:
        if tax_mode == InvoiceTaxMode.LINE and raw.vat_rate_id is None:
            raise ValueError(f"Line '{raw.name}': vat_rate_id is required in LINE tax mode.")
        if (
            raw.vat_rate_id is not None
            and (
                prior is None
                or raw.vat_rate_id != prior.vat_rate_id
                or (
                    tax_mode == InvoiceTaxMode.LINE
                    and (prior.vat_rate_label is None or prior.vat_rate_percent is None)
                )
            )
            and raw.vat_rate_id not in active_rate_ids
        ):
            raise ValueError(f"Line '{raw.name}': VAT rate not found.")
    calc = compute_pricing(
        [raw for _, raw in target],
        tax_mode=tax_mode,
        amounts_include_vat=include_vat,
        discount=discount,
        treatment_effect=treatment_effect,
        vat_rates=rate_map,
        document_vat_rate=document_rate,
        line_rate_overrides=line_overrides,
    )
    calc.vat_treatment_snapshot = treatment_snapshot
    if tax_mode == InvoiceTaxMode.DOCUMENT and calc.document_taxes:
        if document_rate_id is None:
            raise ValueError("document_vat_rate_id is required in DOCUMENT tax mode.")
        calc.document_taxes[0].vat_rate_id = document_rate_id
    return calc


async def get_revision_eligibility(
    session: AsyncSession, invoice: Invoice, company_id: uuid.UUID
) -> InvoiceRevisionEligibilityRead:
    """Evaluate only the requested Invoice, using live payment/mail/chain facts."""
    await set_rls_company(session, company_id)
    has_payment = bool(
        await session.scalar(
            select(Payment.id)
            .where(
                Payment.company_id == company_id,
                Payment.invoice_id == invoice.id,
                Payment.deleted_at.is_(None),
            )
            .limit(1)
        )
    )
    has_mail = bool(
        await session.scalar(
            select(EmailLog.id)
            .where(
                EmailLog.company_id == company_id,
                EmailLog.related_type == EmailRelatedType.INVOICE,
                EmailLog.related_id == invoice.id,
                EmailLog.status == EmailStatus.SENT,
            )
            .limit(1)
        )
    )
    has_dependency = any(
        [
            await session.scalar(
                select(InvoiceCorrection.id)
                .where(
                    InvoiceCorrection.company_id == company_id,
                    InvoiceCorrection.source_invoice_id == invoice.id,
                )
                .limit(1)
            ),
            await session.scalar(
                select(InvoiceRelation.id)
                .where(
                    InvoiceRelation.company_id == company_id,
                    (InvoiceRelation.invoice_id == invoice.id)
                    | (InvoiceRelation.related_credit_note_id == invoice.id),
                )
                .limit(1)
            ),
            await session.scalar(
                select(FinalAdvanceApplication.id)
                .where(
                    FinalAdvanceApplication.company_id == company_id,
                    FinalAdvanceApplication.advance_invoice_id == invoice.id,
                )
                .limit(1)
            ),
        ]
    )
    mode = None
    has_receipt_provenance = False
    if invoice.quote_id is not None:
        quote = await session.get(Quote, invoice.quote_id)
        if quote is not None and quote.company_id == company_id:
            mode = quote.settlement_mode
            if mode == QuoteSettlementMode.RECEIPT_ONLY or await session.scalar(
                select(Payment.id)
                .where(
                    Payment.company_id == company_id,
                    Payment.quote_id == quote.id,
                    Payment.deleted_at.is_(None),
                )
                .limit(1)
            ):
                # Persisted receipt rows remain evidence even if a cached
                # settlement mode was later repaired.
                has_receipt_provenance = True
    result = invoice_revision_eligibility(
        document_kind=InvoiceDocumentKind(invoice.document_kind),
        status=InvoiceStatus(invoice.status),
        settlement_mode=mode,
        has_effective_payment=has_payment,
        has_successful_email=has_mail,
        has_dependent_chain=bool(has_dependency),
        has_receipt_only_provenance=has_receipt_provenance,
    )
    return InvoiceRevisionEligibilityRead(
        available=result.available, reason_code=result.reason_code
    )


async def list_invoice_revisions(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    company_id: uuid.UUID,
    offset: int = 0,
    limit: int = 50,
) -> InvoiceRevisionListRead | None:
    """Return stable newest-first history; None means inaccessible Invoice."""
    await set_rls_company(session, company_id)
    exists = await session.scalar(
        select(Invoice.id).where(Invoice.id == invoice_id, Invoice.company_id == company_id)
    )
    if exists is None:
        return None
    base = select(InvoiceRevision).where(
        InvoiceRevision.invoice_id == invoice_id,
        InvoiceRevision.company_id == company_id,
    )
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (
        await session.scalars(
            base.order_by(InvoiceRevision.revision_order.desc()).offset(offset).limit(limit)
        )
    ).all()
    return InvoiceRevisionListRead(
        items=[InvoiceRevisionRead.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )
