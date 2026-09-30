"""Quote service – CRUD + status transitions + convert + expiry (M6 steps 2 & 3).

Public API
----------
- ``create_quote``           – allocate number, calculate, persist (within transaction)
- ``update_quote``           – recalculate + replace sub-tables (preserves number; ACCEPTED blocks)
- ``delete_quote``           – DB cascade removes lines/taxes; number not recycled
- ``get_quote``              – fetch by id + company_id (returns None if not found)
- ``list_quotes``            – filtered/paginated list
- ``transition_status``      – M6 lifecycle state machine
- ``convert_to_invoice``     – Convert a quote to a new DRAFT invoice (M6 step 3)
- ``reactivate``             – EXPIRED → SENT + extend valid_until (M6 step 3)
- ``expire_due_quotes``      – batch-expire by company (used by list)
- ``expire_due_quotes_all``  – batch-expire across all companies (APScheduler job)

Red-line compliance
-------------------
1. All money calculation is delegated to ``services.pricing``; this module only persists.
2. company_id is always injected by the service; front-end never provides it.
3. No manual cascade deletes – DB ON DELETE CASCADE handles sub-tables.
4. Numbering via ``services.numbering.allocate_quote_number`` (row-locked).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models._enums import (
    DiscountType,
    DocumentChainEventType,
    InvoiceStatus,
    InvoiceTaxMode,
    QuoteSettlementMode,
    QuoteStatus,
    SettingLevel,
    VatTreatmentEffect,
    VatTreatmentSide,
)
from jai.models.customer import Customer
from jai.models.dictionary import Unit
from jai.models.invoice import Invoice
from jai.models.payment import Payment
from jai.models.product import Product
from jai.models.quote import Quote, QuoteLine, QuoteLineTax, QuoteTax
from jai.models.vat import VatRate, VatTreatment
from jai.schemas.invoice import (
    InvoiceRead,
    VatTreatmentSnapshot,
)
from jai.schemas.quote import (
    DocumentChainTotals,
    QuoteLineRead,
    QuoteLineReadTax,
    QuoteListItem,
    QuoteListResponse,
    QuoteReactivateWrite,
    QuoteRead,
    QuoteStatusWrite,
    QuoteTaxRowRead,
    QuoteWrite,
)
from jai.schemas.setting import (
    SETTING_KEY_QUOTE_DEFAULT_VALID_DAYS,
    SETTING_KEY_QUOTE_NUMBERING,
    QuoteDefaultValidDaysRead,
    QuoteNumberingConfig,
)
from jai.services.document_chain import (
    append_document_chain_event,
    compute_document_chain_totals,
    conversion_is_available,
    lock_quote_mode,
    quote_has_converted_invoice,
)
from jai.services.invoice import clone_quote_to_invoice, get_invoice
from jai.services.money import quantize_to_minor_unit
from jai.services.numbering import allocate_quote_number
from jai.services.payment import _write_invoice_state, recompute_payment_state
from jai.services.pricing import (
    _derive_treatment_from_customer,
    compute_pricing,
)
from jai.services.settings import get_setting

_DEFAULT_VALID_DAYS = 30

# ---------------------------------------------------------------------------
# Allowed status transitions (M6 state machine)
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict[QuoteStatus, set[QuoteStatus]] = {
    QuoteStatus.DRAFT: {QuoteStatus.SENT},
    QuoteStatus.SENT: {QuoteStatus.DRAFT, QuoteStatus.REJECTED, QuoteStatus.ACCEPTED},
    QuoteStatus.REJECTED: {QuoteStatus.SENT},
    QuoteStatus.EXPIRED: {QuoteStatus.REJECTED, QuoteStatus.ACCEPTED},
    QuoteStatus.ACCEPTED: set(),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_numbering_config(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> QuoteNumberingConfig:
    config = await get_setting(
        session,
        SETTING_KEY_QUOTE_NUMBERING,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=QuoteNumberingConfig,
    )
    return config or QuoteNumberingConfig()


async def _load_default_valid_days(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> int:
    raw = await get_setting(
        session,
        SETTING_KEY_QUOTE_DEFAULT_VALID_DAYS,
        level=SettingLevel.COMPANY,
        scope_id=company_id,
        value_type=QuoteDefaultValidDaysRead,
    )
    return raw.default_valid_days if raw is not None else _DEFAULT_VALID_DAYS


async def _resolve_treatment(
    session: AsyncSession,
    company_id: uuid.UUID,
    customer: Customer,
    vat_treatment_id: uuid.UUID | None,
) -> VatTreatment:
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
    body: QuoteWrite,
) -> dict[uuid.UUID, tuple[str, Decimal]]:
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

    for rate_id in all_ids:
        if rate_id not in vat_rates:
            raise ValueError(f"VAT rate {rate_id} not found or not in this company.")

    return vat_rates


async def _validate_line_fks(
    session: AsyncSession,
    company_id: uuid.UUID,
    body: QuoteWrite,
) -> None:
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


def _line_to_read(line: QuoteLine) -> QuoteLineRead:
    return QuoteLineRead(
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
            QuoteLineReadTax(
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


def _quote_to_read(
    q: Quote,
    *,
    incoming_payment_total: Decimal = Decimal("0"),
    base_incoming_payment_total: Decimal = Decimal("0"),
    chain_totals: DocumentChainTotals | None = None,
) -> QuoteRead:
    totals = chain_totals or DocumentChainTotals(
        incoming_payment_total=incoming_payment_total,
        base_incoming_payment_total=base_incoming_payment_total,
    )
    treatment_snapshot = VatTreatmentSnapshot(
        id=q.vat_treatment_id,
        code=q.vat_treatment_code,
        label=q.vat_treatment_label,
        effect=q.vat_treatment_effect,
        requires_icp=q.vat_treatment_requires_icp,
    )
    return QuoteRead(
        id=q.id,
        company_id=q.company_id,
        customer_id=q.customer_id,
        quote_number=q.quote_number,
        sequence_number=q.sequence_number,
        customer_sequence_number=q.customer_sequence_number,
        unique_hash=q.unique_hash,
        reference_number=q.reference_number,
        quote_date=q.quote_date,
        valid_until=q.valid_until,
        status=QuoteStatus(q.status),
        converted_invoice_id=q.converted_invoice_id,
        settlement_mode=q.settlement_mode,
        settlement_mode_locked_at=q.settlement_mode_locked_at,
        chain_totals=totals,
        incoming_payment_total=totals.incoming_payment_total,
        remaining_amount=max(
            Decimal("0"), Decimal(str(q.total_incl_vat)) - totals.incoming_payment_total
        ),
        currency=q.currency,
        exchange_rate=Decimal(str(q.exchange_rate)),
        tax_mode=InvoiceTaxMode(q.tax_mode),
        amounts_include_vat=q.amounts_include_vat,
        vat_treatment_id=q.vat_treatment_id,
        document_vat_rate_id=q.document_vat_rate_id,
        vat_treatment_snapshot=treatment_snapshot,
        discount_type=DiscountType(q.discount_type),
        discount_value=Decimal(str(q.discount_value)),
        document_discount_amount=Decimal(str(q.document_discount_amount)),
        subtotal_excl_vat=Decimal(str(q.subtotal_excl_vat)),
        line_discount_total=Decimal(str(q.line_discount_total)),
        taxable_amount=Decimal(str(q.taxable_amount)),
        vat_total=Decimal(str(q.vat_total)),
        total_incl_vat=Decimal(str(q.total_incl_vat)),
        base_subtotal_excl_vat=Decimal(str(q.base_subtotal_excl_vat)),
        base_line_discount_total=Decimal(str(q.base_line_discount_total)),
        base_taxable_amount=Decimal(str(q.base_taxable_amount)),
        base_vat_total=Decimal(str(q.base_vat_total)),
        base_total_incl_vat=Decimal(str(q.base_total_incl_vat)),
        base_incoming_payment_total=totals.base_incoming_payment_total,
        base_remaining_amount=max(
            Decimal("0"),
            Decimal(str(q.base_total_incl_vat)) - totals.base_incoming_payment_total,
        ),
        notes=q.notes,
        warranty_text=q.warranty_text,
        terms_text=q.terms_text,
        bank_text=q.bank_text,
        payment_terms_text=q.payment_terms_text,
        creator_id=q.creator_id,
        lines=[_line_to_read(line) for line in q.lines],
        taxes=[
            QuoteTaxRowRead(
                id=t.id,
                vat_rate_id=t.vat_rate_id,
                vat_rate_label=t.vat_rate_label,
                vat_rate_percent=Decimal(str(t.vat_rate_percent)),
                effective_vat_percent=Decimal(str(t.effective_vat_percent)),
                taxable_amount=Decimal(str(t.taxable_amount)),
                tax_amount=Decimal(str(t.tax_amount)),
            )
            for t in q.taxes
        ],
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


async def _quote_chain_totals(session: AsyncSession, quote: Quote) -> DocumentChainTotals:
    """Load inputs for the authoritative M12 compact chain projection."""
    invoices = list(
        (
            await session.execute(
                select(Invoice).where(
                    Invoice.company_id == quote.company_id,
                    Invoice.quote_id == quote.id,
                )
            )
        ).scalars()
    )
    quote_payments = list(
        (
            await session.execute(
                select(Payment).where(
                    Payment.company_id == quote.company_id,
                    Payment.quote_id == quote.id,
                    Payment.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    return compute_document_chain_totals(invoices, quote_payments, quote=quote)


def _quote_to_list_item(q: Quote, *, customer_name: str) -> QuoteListItem:
    return QuoteListItem(
        id=q.id,
        company_id=q.company_id,
        customer_id=q.customer_id,
        customer_name=customer_name,
        quote_number=q.quote_number,
        reference_number=q.reference_number,
        quote_date=q.quote_date,
        valid_until=q.valid_until,
        status=QuoteStatus(q.status),
        converted_invoice_id=q.converted_invoice_id,
        settlement_mode=q.settlement_mode,
        currency=q.currency,
        total_incl_vat=Decimal(str(q.total_incl_vat)),
        vat_treatment_snapshot=VatTreatmentSnapshot(
            id=q.vat_treatment_id,
            code=q.vat_treatment_code,
            label=q.vat_treatment_label,
            effect=q.vat_treatment_effect,
            requires_icp=q.vat_treatment_requires_icp,
        ),
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


def _apply_expiry_in_memory(q: Quote) -> bool:
    """Return True if quote should be flipped to EXPIRED (does not persist).

    Logic: SENT + valid_until is not None + valid_until < today.
    """
    if q.status == QuoteStatus.SENT and q.valid_until is not None:
        return q.valid_until < date.today()
    return False


# ---------------------------------------------------------------------------
# Core calculation + persistence helper
# ---------------------------------------------------------------------------


async def _build_and_persist_quote(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    company_currency: str,
    creator_id: uuid.UUID | None,
    body: QuoteWrite,
    customer: Customer,
    treatment: VatTreatment,
    vat_rates: dict[uuid.UUID, tuple[str, Decimal]],
    quote_number: str | None = None,
    sequence_number: int | None = None,
    customer_sequence_number: int | None = None,
    existing_quote: Quote | None = None,
    valid_until_default: date | None = None,
) -> Quote:
    """Calculate pricing and persist a new or updated quote."""
    request_currency = body.currency or company_currency
    if request_currency != company_currency:
        raise ValueError(
            f"M6 only supports quotes in company base currency "
            f"({company_currency}); got {request_currency}."
        )

    treatment_effect = VatTreatmentEffect(treatment.effect)

    document_vat_rate: tuple[str, Decimal] | None = None
    if body.tax_mode == InvoiceTaxMode.DOCUMENT:
        rate_info = vat_rates.get(body.document_vat_rate_id)  # type: ignore[arg-type]
        document_vat_rate = rate_info

    calc = compute_pricing(
        body.lines,
        tax_mode=body.tax_mode,
        amounts_include_vat=body.amounts_include_vat,
        discount=body.discount,
        treatment_effect=treatment_effect,
        vat_rates=vat_rates,
        document_vat_rate=document_vat_rate,
    )

    if body.tax_mode == InvoiceTaxMode.DOCUMENT and calc.document_taxes:
        calc.document_taxes[0].vat_rate_id = body.document_vat_rate_id  # type: ignore[assignment]

    if quote_number is None:
        numbering_config = await _load_numbering_config(session, company_id)
        quote_number, sequence_number, customer_sequence_number = await allocate_quote_number(
            session,
            company_id,
            customer.id,
            body.quote_date,
            numbering_config=numbering_config,
            customer_invoice_prefix=customer.invoice_prefix,
        )

    total_incl_vat = quantize_to_minor_unit(calc.total_incl_vat)
    treatment_effect_str = (
        treatment.effect.value
        if isinstance(treatment.effect, VatTreatmentEffect)
        else str(treatment.effect)
    )

    subtotal_excl_vat = quantize_to_minor_unit(calc.subtotal_excl_vat)
    line_discount_total = quantize_to_minor_unit(calc.line_discount_total)
    taxable_amount = quantize_to_minor_unit(calc.taxable_amount)
    vat_total = quantize_to_minor_unit(calc.vat_total)
    doc_discount_amount = quantize_to_minor_unit(calc.document_discount_amount)

    if existing_quote is not None:
        q = existing_quote
        await session.execute(delete(QuoteLine).where(QuoteLine.quote_id == q.id))
        await session.execute(delete(QuoteTax).where(QuoteTax.quote_id == q.id))
    else:
        q = Quote()
        q.company_id = company_id
        q.quote_number = quote_number
        q.sequence_number = sequence_number  # type: ignore[assignment]
        q.customer_sequence_number = customer_sequence_number
        q.status = QuoteStatus.DRAFT
        q.creator_id = creator_id

    q.customer_id = customer.id
    q.reference_number = body.reference_number
    q.quote_date = body.quote_date

    # valid_until: use body value if provided; for new quotes fall back to default
    if body.valid_until is not None:
        q.valid_until = body.valid_until
    elif existing_quote is None and valid_until_default is not None:
        q.valid_until = valid_until_default
    elif existing_quote is not None:
        q.valid_until = body.valid_until  # allow clearing on update

    q.currency = request_currency
    q.exchange_rate = Decimal("1")

    q.tax_mode = body.tax_mode
    q.amounts_include_vat = body.amounts_include_vat
    q.vat_treatment_id = treatment.id
    q.document_vat_rate_id = (
        body.document_vat_rate_id if body.tax_mode == InvoiceTaxMode.DOCUMENT else None
    )

    q.vat_treatment_code = treatment.code
    q.vat_treatment_label = treatment.label
    q.vat_treatment_effect = treatment_effect_str
    q.vat_treatment_requires_icp = treatment.requires_icp

    q.discount_type = body.discount.type
    q.discount_value = body.discount.value
    q.document_discount_amount = doc_discount_amount

    q.subtotal_excl_vat = subtotal_excl_vat
    q.line_discount_total = line_discount_total
    q.taxable_amount = taxable_amount
    q.vat_total = vat_total
    q.total_incl_vat = total_incl_vat

    q.base_subtotal_excl_vat = subtotal_excl_vat
    q.base_line_discount_total = line_discount_total
    q.base_taxable_amount = taxable_amount
    q.base_vat_total = vat_total
    q.base_total_incl_vat = total_incl_vat

    q.notes = body.notes

    # -- Content block snapshot text (M6 step 4) -----------------------------
    q.warranty_text = body.warranty_text
    q.terms_text = body.terms_text
    q.bank_text = body.bank_text
    q.payment_terms_text = body.payment_terms_text

    if existing_quote is None:
        session.add(q)
    await session.flush()

    new_line_rows: list[QuoteLine] = []
    for i, (line_input, line_calc) in enumerate(zip(body.lines, calc.lines, strict=True)):
        line_row = QuoteLine()
        line_row.quote_id = q.id
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

    await session.flush()

    if body.tax_mode == InvoiceTaxMode.LINE:
        for line_row, lt in zip(new_line_rows, calc.line_taxes, strict=False):
            lt_row = QuoteLineTax()
            lt_row.quote_line_id = line_row.id
            lt_row.vat_rate_id = lt.vat_rate_id
            lt_row.vat_rate_label = lt.vat_rate_label
            lt_row.vat_rate_percent = lt.vat_rate_percent
            lt_row.effective_vat_percent = lt.effective_vat_percent
            lt_row.taxable_amount = lt.taxable_amount
            lt_row.tax_amount = lt.tax_amount
            session.add(lt_row)

    if body.tax_mode == InvoiceTaxMode.DOCUMENT:
        for dt in calc.document_taxes:
            tax_row = QuoteTax()
            tax_row.quote_id = q.id
            tax_row.vat_rate_id = dt.vat_rate_id
            tax_row.vat_rate_label = dt.vat_rate_label
            tax_row.vat_rate_percent = dt.vat_rate_percent
            tax_row.effective_vat_percent = dt.effective_vat_percent
            tax_row.taxable_amount = dt.taxable_amount
            tax_row.tax_amount = dt.tax_amount
            session.add(tax_row)

    await session.flush()
    return q


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------


async def apply_expiry(session: AsyncSession, q: Quote) -> None:
    """Flip a single quote to EXPIRED in-place if conditions are met and persist."""
    if _apply_expiry_in_memory(q):
        q.status = QuoteStatus.EXPIRED
        await session.flush()


async def expire_due_quotes(session: AsyncSession, company_id: uuid.UUID) -> int:
    """Batch-expire all SENT quotes past their valid_until for a company.

    Idempotent: re-running produces no extra changes.  Returns count flipped.
    """
    today = date.today()
    stmt = select(Quote).where(
        Quote.company_id == company_id,
        Quote.status == QuoteStatus.SENT,
        Quote.valid_until.isnot(None),
        Quote.valid_until < today,
    )
    result = await session.execute(stmt)
    quotes = result.scalars().all()
    for q in quotes:
        q.status = QuoteStatus.EXPIRED
    if quotes:
        await session.flush()
    return len(quotes)


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def create_quote(
    session: AsyncSession,
    body: QuoteWrite,
    company_id: uuid.UUID,
    company_currency: str,
    creator_id: uuid.UUID | None,
) -> QuoteRead:
    """Create a new quote: allocate number, calculate, persist."""
    q = await create_quote_in_transaction(
        session, body, company_id, company_currency, creator_id
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("Quote number already exists (concurrent creation).") from exc

    await session.refresh(q)
    return _quote_to_read(q)


async def create_quote_in_transaction(
    session: AsyncSession,
    body: QuoteWrite,
    company_id: uuid.UUID,
    company_currency: str,
    creator_id: uuid.UUID | None,
) -> Quote:
    """Create and flush a quote without committing the caller's transaction."""
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

    # Default valid_until from company setting if not provided in body
    valid_until_default: date | None = None
    if body.valid_until is None:
        days = await _load_default_valid_days(session, company_id)
        valid_until_default = date.today() + timedelta(days=days)

    q = await _build_and_persist_quote(
        session,
        company_id=company_id,
        company_currency=company_currency,
        creator_id=creator_id,
        body=body,
        customer=customer,
        treatment=treatment,
        vat_rates=vat_rates,
        valid_until_default=valid_until_default,
    )
    return q


async def get_quote(
    session: AsyncSession,
    quote_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    apply_expiry: bool = True,
) -> QuoteRead | None:
    """Return a full QuoteRead, or None if not found / wrong company.

    Applies read-time expiry check before returning.
    """
    # Compact totals can read quote-origin Payment rows.  The runtime role is
    # NOBYPASSRLS, so establish the tenant context before any such read.
    await set_rls_company(session, company_id)
    stmt = select(Quote).where(
        Quote.id == quote_id,
        Quote.company_id == company_id,
    )
    result = await session.execute(stmt)
    q = result.scalar_one_or_none()
    if q is None:
        return None
    chain_totals = await _quote_chain_totals(session, q)
    if apply_expiry and _apply_expiry_in_memory(q):
        q.status = QuoteStatus.EXPIRED
        # Build read model before commit (avoids post-commit attribute expiry)
        read = _quote_to_read(q, chain_totals=chain_totals)
        await session.flush()
        await session.commit()
        return read
    return _quote_to_read(q, chain_totals=chain_totals)


async def update_quote(
    session: AsyncSession,
    quote_id: uuid.UUID,
    body: QuoteWrite,
    company_id: uuid.UUID,
    company_currency: str,
) -> QuoteRead | None:
    """Update an existing quote: preserve number, recalculate, replace sub-tables.

    ACCEPTED quotes cannot be edited (409 raised as ValueError).
    """
    q = await update_quote_draft_in_transaction(
        session,
        quote_id,
        body,
        company_id,
        company_currency,
        require_draft=False,
    )
    if q is None:
        return None
    await session.commit()
    await session.refresh(q)
    return _quote_to_read(q)


async def update_quote_draft_in_transaction(
    session: AsyncSession,
    quote_id: uuid.UUID,
    body: QuoteWrite,
    company_id: uuid.UUID,
    company_currency: str,
    *,
    require_draft: bool = True,
) -> Quote | None:
    """Update and flush a quote without committing the caller's transaction.

    Agent calls request a row lock and the strict DRAFT-only gate. Web calls
    retain their existing ACCEPTED-only restriction and transaction behavior.
    """
    stmt = select(Quote).where(
        Quote.id == quote_id,
        Quote.company_id == company_id,
    )
    if require_draft:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    q = result.scalar_one_or_none()
    if q is None:
        return None

    if require_draft and q.status != QuoteStatus.DRAFT:
        raise ValueError("MCP_DRAFT_REQUIRED")

    if not require_draft and q.status == QuoteStatus.ACCEPTED:
        raise ValueError(
            "Cannot edit an ACCEPTED quote. "
            "Reactivate it first or edit the converted invoice directly."
        )

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

    updated = await _build_and_persist_quote(
        session,
        company_id=company_id,
        company_currency=company_currency,
        creator_id=q.creator_id,
        body=body,
        customer=customer,
        treatment=treatment,
        vat_rates=vat_rates,
        quote_number=q.quote_number,
        sequence_number=q.sequence_number,
        customer_sequence_number=q.customer_sequence_number,
        existing_quote=q,
    )

    return updated


async def delete_quote(
    session: AsyncSession,
    quote_id: uuid.UUID,
    company_id: uuid.UUID,
) -> bool:
    """Delete a quote; returns True if deleted, False if not found.

    Numbers are never recycled (red-line 4).
    """
    stmt = select(Quote).where(
        Quote.id == quote_id,
        Quote.company_id == company_id,
    )
    result = await session.execute(stmt)
    q = result.scalar_one_or_none()
    if q is None:
        return False

    await session.delete(q)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("Cannot delete a quote that has payment history.") from exc
    return True


async def list_quotes(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    q_param: str | None = None,
    customer_id: uuid.UUID | None = None,
    status: QuoteStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "quote_date",
) -> QuoteListResponse:
    """Return a paginated list of quotes for the company.

    Batch-expires all SENT-but-past-valid_until quotes for this company BEFORE
    applying the status filter and count, so that ?status=EXPIRED and ?status=SENT
    always reflect the true post-expiry state.
    """
    # Flush expiry changes into the transaction so the filter/count below see them.
    expired_count = await expire_due_quotes(session, company_id)

    base = (
        select(Quote)
        .join(Customer, Quote.customer_id == Customer.id)
        .where(Quote.company_id == company_id)
    )

    if q_param:
        like = f"%{q_param}%"
        base = base.where(
            or_(
                Quote.quote_number.ilike(like),
                Quote.reference_number.ilike(like),
                Customer.name.ilike(like),
            )
        )
    if customer_id is not None:
        base = base.where(Quote.customer_id == customer_id)
    if status is not None:
        base = base.where(Quote.status == status)
    if date_from:
        base = base.where(Quote.quote_date >= date_from)
    if date_to:
        base = base.where(Quote.quote_date <= date_to)

    count_stmt = select(func.count()).select_from(base.subquery())
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    sort_col = {
        "quote_date": Quote.quote_date,
        "created_at": Quote.created_at,
        "quote_number": Quote.sequence_number,
    }.get(sort_by, Quote.quote_date)

    data_stmt = base.order_by(sort_col.desc()).limit(limit).offset(offset)
    data_result = await session.execute(data_stmt)
    rows = list(data_result.scalars().all())

    customer_name_map: dict[uuid.UUID, str] = {}
    if rows:
        cust_ids = list({r.customer_id for r in rows})
        cust_result = await session.execute(
            select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))
        )
        customer_name_map = {cid: name for cid, name in cust_result.all()}

    response = QuoteListResponse(
        items=[
            _quote_to_list_item(r, customer_name=customer_name_map.get(r.customer_id, ""))
            for r in rows
        ],
        total=total,
    )

    if expired_count > 0:
        await session.commit()

    return response


_CONVERTIBLE_STATUSES: frozenset[QuoteStatus] = frozenset(
    {QuoteStatus.SENT, QuoteStatus.ACCEPTED, QuoteStatus.EXPIRED}
)


class ConversionConflictError(ValueError):
    """A stable conflict for a duplicate conversion command."""

    code = "ALREADY_CONVERTED"


async def convert_to_invoice(
    session: AsyncSession,
    quote_id: uuid.UUID,
    company_id: uuid.UUID,
    creator_id: uuid.UUID | None,
) -> InvoiceRead | None:
    """Convert a quote to a new DRAFT/UNPAID invoice.

    - Status must be SENT, ACCEPTED, or EXPIRED (soft-expiry rule).
    - ``converted_invoice_id`` must be NULL (re-convert → ValueError).
    - Quote is set to ACCEPTED + ``converted_invoice_id`` as a single commit.
    Returns None if the quote is not found.
    """
    await set_rls_company(session, company_id)
    # Row-lock the quote so concurrent Convert requests serialise and the
    # second sees converted_invoice_id != NULL (preventing duplicate invoices).
    stmt = (
        select(Quote)
        .where(
            Quote.id == quote_id,
            Quote.company_id == company_id,
        )
        .with_for_update()
    )
    result = await session.execute(stmt)
    q = result.scalar_one_or_none()
    if q is None:
        return None

    # Apply read-time expiry before evaluating convertibility.
    if _apply_expiry_in_memory(q):
        q.status = QuoteStatus.EXPIRED
        await session.flush()

    if QuoteStatus(q.status) not in _CONVERTIBLE_STATUSES:
        raise ValueError(
            f"Only SENT, ACCEPTED, or EXPIRED quotes can be converted; "
            f"this quote is {q.status.value}."
        )

    if await quote_has_converted_invoice(session, company_id=company_id, quote_id=q.id):
        raise ConversionConflictError("Quote already has an authoritative converted invoice.")

    # A mode mismatch is never reported as a conversion-state conflict.  This
    # preserves an actionable, stable contract for FORMAL and other branches.
    if QuoteSettlementMode(q.settlement_mode) not in {
        QuoteSettlementMode.UNSET,
        QuoteSettlementMode.DIRECT_INVOICE,
        QuoteSettlementMode.RECEIPT_ONLY,
    }:
        await lock_quote_mode(
            session, q, QuoteSettlementMode.DIRECT_INVOICE, actor_user_id=creator_id
        )
    if not await conversion_is_available(session, q):
        raise ConversionConflictError("Quote already has an authoritative converted invoice.")

    # The branch lock and conversion belong to the same outer quote-locked
    # transaction.  A later failure/rollback therefore leaves UNSET intact.
    # M11.5 receipt-only conversion remains its established continuation:
    # quote-origin cash becomes attached to the one final Standard invoice.
    # Only an UNSET quote selects the direct branch here.
    if QuoteSettlementMode(q.settlement_mode) != QuoteSettlementMode.RECEIPT_ONLY:
        await lock_quote_mode(
            session, q, QuoteSettlementMode.DIRECT_INVOICE, actor_user_id=creator_id
        )

    inv_read = await clone_quote_to_invoice(
        session, q, company_id=company_id, creator_id=creator_id
    )

    # The quote lock is also the parent lock used by quote-payment mutations.
    # Lock all current payments after it, then transfer them inside this same
    # transaction so no accepted payment can be missed by a concurrent convert.
    payment_result = await session.execute(
        select(Payment)
        .where(Payment.quote_id == q.id, Payment.deleted_at.is_(None))
        .order_by(Payment.payment_date, Payment.created_at, Payment.id)
        .with_for_update()
    )
    payments = list(payment_result.scalars().all())
    invoice_result = await session.execute(select(Invoice).where(Invoice.id == inv_read.id))
    invoice = invoice_result.scalar_one()
    for payment in payments:
        payment.invoice_id = invoice.id

    if payments:
        invoice.invoice_date = max(
            invoice.invoice_date,
            max(payment.payment_date for payment in payments),
        )
    payment_state = recompute_payment_state(
        Decimal(str(invoice.total_incl_vat)),
        Decimal(str(invoice.base_total_incl_vat)),
        payments,
        InvoiceStatus.DRAFT,
    )
    _write_invoice_state(invoice, payment_state)
    invoice.status = InvoiceStatus.DRAFT

    q.status = QuoteStatus.ACCEPTED
    q.converted_invoice_id = inv_read.id
    await append_document_chain_event(
        session,
        company_id=company_id,
        quote_id=q.id,
        invoice_id=inv_read.id,
        actor_user_id=creator_id,
        event_type=DocumentChainEventType.INVOICE_CREATED,
        metadata={"document_kind": "STANDARD"},
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("Invoice number already exists (concurrent creation).") from exc

    refreshed = await get_invoice(session, invoice.id, company_id)
    assert refreshed is not None
    return refreshed


async def reactivate(
    session: AsyncSession,
    quote_id: uuid.UUID,
    company_id: uuid.UUID,
    body: QuoteReactivateWrite,
) -> QuoteRead | None:
    """Reactivate an EXPIRED quote: set status to SENT and extend valid_until.

    If ``body.valid_until`` is not provided, extends by the company default
    number of days from today.  Returns None if the quote is not found.
    """
    stmt = select(Quote).where(
        Quote.id == quote_id,
        Quote.company_id == company_id,
    )
    result = await session.execute(stmt)
    q = result.scalar_one_or_none()
    if q is None:
        return None

    if QuoteStatus(q.status) != QuoteStatus.EXPIRED:
        raise ValueError(f"Only EXPIRED quotes can be reactivated; this quote is {q.status.value}.")

    q.status = QuoteStatus.SENT

    if body.valid_until is not None:
        if body.valid_until < date.today():
            raise ValueError(f"valid_until must be today or a future date; got {body.valid_until}.")
        q.valid_until = body.valid_until
    else:
        days = await _load_default_valid_days(session, company_id)
        q.valid_until = date.today() + timedelta(days=days)

    await session.commit()
    await session.refresh(q)
    return _quote_to_read(q)


async def expire_due_quotes_all(session: AsyncSession) -> int:
    """Batch-expire SENT quotes past their valid_until across all companies.

    Called by the APScheduler daily job.  Idempotent – re-running produces no
    extra changes.  Returns the number of quotes flipped to EXPIRED.
    """
    today = date.today()
    stmt = select(Quote).where(
        Quote.status == QuoteStatus.SENT,
        Quote.valid_until.isnot(None),
        Quote.valid_until < today,
    )
    result = await session.execute(stmt)
    quotes = result.scalars().all()
    for q in quotes:
        q.status = QuoteStatus.EXPIRED
    if quotes:
        await session.flush()
    return len(quotes)


async def transition_status(
    session: AsyncSession,
    quote_id: uuid.UUID,
    company_id: uuid.UUID,
    body: QuoteStatusWrite,
) -> QuoteRead | None:
    """Transition quote lifecycle status per M6 state machine.

    Allowed manual transitions:
    - DRAFT ↔ SENT
    - SENT / EXPIRED → REJECTED
    - REJECTED → SENT
    - SENT / EXPIRED → ACCEPTED (manual)

    EXPIRED cannot be set manually; it is driven by date check / APScheduler.
    """
    stmt = select(Quote).where(
        Quote.id == quote_id,
        Quote.company_id == company_id,
    )
    result = await session.execute(stmt)
    q = result.scalar_one_or_none()
    if q is None:
        return None

    # Read-time expiry before evaluating transition
    await apply_expiry(session, q)

    current_status = QuoteStatus(q.status)
    new_status = body.status

    if new_status == QuoteStatus.EXPIRED:
        raise ValueError(
            "EXPIRED status is set automatically by the expiry job / read-time check. "
            "It cannot be assigned manually."
        )

    allowed = _ALLOWED_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition from {current_status} to {new_status}. "
            f"Allowed transitions: {', '.join(s.value for s in allowed) or 'none'}."
        )

    q.status = new_status
    await session.commit()
    await session.refresh(q)
    return _quote_to_read(q)
