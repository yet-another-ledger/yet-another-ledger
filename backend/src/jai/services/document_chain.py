"""M12 chain projection and append-only lifecycle-event helpers.

This module is the only place that derives document-chain totals.  It keeps
the API and Vue layer from performing settlement arithmetic.
"""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models._enums import (
    DocumentChainEventType,
    InvoiceDocumentKind,
    InvoiceRelationType,
    InvoiceStatus,
    QuoteSettlementMode,
    QuoteStatus,
)
from jai.models.document import (
    DocumentChainEvent,
    FinalAdvanceApplication,
    InvoiceCorrection,
    InvoiceRelation,
)
from jai.models.invoice import Invoice, InvoiceLine
from jai.models.payment import Payment
from jai.models.quote import Quote
from jai.schemas.document_chain import (
    DocumentChainApplicationRead,
    DocumentChainAvailableActionRead,
    DocumentChainEventRead,
    DocumentChainNodeRead,
    DocumentChainRead,
    DocumentChainRelationRead,
    DocumentChainTimelineApplicationRead,
    DocumentChainTimelineEventRead,
    DocumentChainTimelineItemRead,
    DocumentChainTimelineNodeRead,
    DocumentChainTimelineRelationRead,
    FollowupContextRead,
)
from jai.schemas.quote import DocumentChainTotals
from jai.services.document_actions import (
    advance_replacement_capacity_eligibility,
    cancellation_eligibility,
    credit_note_eligibility,
    followup_eligibility,
)

type SafeValue = object


def _action(code: str, available: bool, reason_code: str | None = None, *, target_id: uuid.UUID | None = None, target_type: str | None = None, followup_context: FollowupContextRead | None = None) -> DocumentChainAvailableActionRead:
    """Keep command eligibility and its safe UI explanation in one projection."""
    return DocumentChainAvailableActionRead(
        code=code,
        available=available,
        reason_code=None if available else (reason_code or "ACTION_UNAVAILABLE"),
        target_id=target_id,
        target_type=target_type,  # type: ignore[arg-type]
        followup_context=followup_context,
    )


def _followup_context(
    *, credit: Invoice, source: Invoice | None, relation_type: InvoiceRelationType, mode: QuoteSettlementMode | None, final_exists: bool
) -> FollowupContextRead | None:
    """Derive the actual positive target kind once, on the backend."""
    if source is None:
        return None
    source_kind = InvoiceDocumentKind(source.document_kind)
    target_kind = (
        source_kind
        if relation_type == InvoiceRelationType.REPLACEMENT_OF
        else (
            InvoiceDocumentKind.ADVANCE
            if source_kind == InvoiceDocumentKind.ADVANCE
            and mode == QuoteSettlementMode.FORMAL_ADVANCE
            and not final_exists
            else InvoiceDocumentKind.STANDARD
        )
    )
    return FollowupContextRead(
        credit_note_id=credit.id,
        source_invoice_id=source.id,
        relation_type=relation_type.value,
        target_document_kind=target_kind,
        gross_amount=Decimal(str(credit.total_incl_vat)),
    )


def _timeline(
    nodes: list[DocumentChainNodeRead],
    events: list[DocumentChainEventRead],
    relations: list[DocumentChainRelationRead],
    applications: list[DocumentChainApplicationRead] | None = None,
) -> list[DocumentChainTimelineItemRead]:
    """Return one stable chronological display order for all chain facts."""
    node_dates = {node.id: node.occurred_on for node in nodes}
    # Date-only facts deliberately precede events on that date; events retain
    # their full timestamp and database sequence, so CREATE → ISSUE → cash
    # cannot be shuffled by UUID ties.  Relations/applications follow their
    # date-only endpoints in deterministic domain-key order.
    ranked: list[tuple[object, int, object, DocumentChainTimelineItemRead]] = []
    for node in nodes:
        ranked.append((datetime.combine(node.occurred_on, time.min, tzinfo=UTC), 0, str(node.id), DocumentChainTimelineNodeRead(kind="NODE", order=0, node=node)))
    for event in events:
        ranked.append((event.occurred_at, 1, event.event_order, DocumentChainTimelineEventRead(kind="EVENT", order=0, event=event)))
    for relation in relations:
        # Relations have no user-entered date; their target document/cash is
        # the authoritative visible occurrence.  This is deterministic even
        # for migrated rows without an event record.
        occurred_on = max(
            node_dates.get(relation.from_node_id, datetime.min.date()),
            node_dates.get(relation.to_node_id, datetime.min.date()),
        )
        ranked.append((datetime.combine(occurred_on, time.min, tzinfo=UTC), 2, f"{relation.relation_type}:{relation.from_node_id}:{relation.to_node_id}", DocumentChainTimelineRelationRead(kind="RELATION", order=0, relation=relation)))
    for application in applications or []:
        ranked.append((datetime.combine(application.occurred_on, time.min, tzinfo=UTC), 3, f"{application.final_invoice_id}:{application.advance_invoice_id}", DocumentChainTimelineApplicationRead(kind="APPLICATION", order=0, application=application)))
    ordered = [item for _, _, _, item in sorted(ranked, key=lambda row: row[:3])]
    for order, item in enumerate(ordered):
        item.order = order
    return ordered


class _EventMetadata(BaseModel):
    """Closed, non-renderable metadata shared by one event type only."""

    model_config = ConfigDict(extra="forbid")


class _ModeLockedMetadata(_EventMetadata):
    mode: QuoteSettlementMode


class _InvoiceKindMetadata(_EventMetadata):
    document_kind: InvoiceDocumentKind


class _InvoiceStatusMetadata(_InvoiceKindMetadata):
    status: InvoiceStatus


class _PaymentMetadata(_EventMetadata):
    payment_id: uuid.UUID
    amount: Decimal

    @field_validator("amount")
    @classmethod
    def _amount_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Document-chain event amount must be finite.")
        return value


class _CreditRelationMetadata(_EventMetadata):
    credit_note_id: uuid.UUID


class _CancellationCreditMetadata(_EventMetadata):
    source_invoice_id: uuid.UUID


class _InvoiceRevisionMetadata(_EventMetadata):
    revision_id: uuid.UUID


_EVENT_METADATA_MODELS: dict[DocumentChainEventType, type[_EventMetadata]] = {
    DocumentChainEventType.MODE_LOCKED: _ModeLockedMetadata,
    DocumentChainEventType.INVOICE_CREATED: _InvoiceKindMetadata,
    DocumentChainEventType.INVOICE_UPDATED: _InvoiceKindMetadata,
    DocumentChainEventType.INVOICE_ISSUED: _InvoiceStatusMetadata,
    DocumentChainEventType.INVOICE_STATUS_CHANGED: _InvoiceStatusMetadata,
    DocumentChainEventType.INVOICE_DELETED: _InvoiceKindMetadata,
    DocumentChainEventType.QUOTE_PAYMENT_CREATED: _PaymentMetadata,
    DocumentChainEventType.QUOTE_PAYMENT_UPDATED: _PaymentMetadata,
    DocumentChainEventType.QUOTE_PAYMENT_DELETED: _PaymentMetadata,
    DocumentChainEventType.INVOICE_PAYMENT_CREATED: _PaymentMetadata,
    DocumentChainEventType.INVOICE_PAYMENT_UPDATED: _PaymentMetadata,
    DocumentChainEventType.INVOICE_PAYMENT_DELETED: _PaymentMetadata,
    DocumentChainEventType.REFUND_CREATED: _PaymentMetadata,
    DocumentChainEventType.REFUND_UPDATED: _PaymentMetadata,
    DocumentChainEventType.REFUND_DELETED: _PaymentMetadata,
    DocumentChainEventType.REPLACEMENT_CREATED: _CreditRelationMetadata,
    DocumentChainEventType.COMPENSATING_INVOICE_CREATED: _CreditRelationMetadata,
    DocumentChainEventType.PROJECT_CANCELLATION_CREDIT_CREATED: _CancellationCreditMetadata,
    DocumentChainEventType.INVOICE_REVISED: _InvoiceRevisionMetadata,
}


class ModeConflictError(ValueError):
    """A deterministic, machine-readable quote-branch conflict."""

    code = "MODE_CONFLICT"


def _safe_metadata(
    event_type: DocumentChainEventType, metadata: dict[str, SafeValue] | None
) -> dict[str, object]:
    """Validate and JSON-serialise the one closed schema for an event type."""
    try:
        # JSON mode deliberately turns UUID/Decimal into canonical strings.
        return (
            _EVENT_METADATA_MODELS[event_type]
            .model_validate(metadata or {})
            .model_dump(mode="json")
        )
    except ValidationError as exc:
        raise ValueError("Invalid document-chain event metadata.") from exc


async def append_document_chain_event(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    event_type: DocumentChainEventType,
    quote_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    metadata: dict[str, SafeValue] | None = None,
) -> DocumentChainEvent:
    """Add one event without committing; caller owns the outer transaction."""
    event = DocumentChainEvent(
        company_id=company_id,
        quote_id=quote_id,
        invoice_id=invoice_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        metadata_json=_safe_metadata(event_type, metadata),
    )
    session.add(event)
    await session.flush()
    return event


async def lock_quote_mode(
    session: AsyncSession,
    quote: Quote,
    target: QuoteSettlementMode,
    *,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Lock a Quote branch within its already-held transaction/row lock."""
    current = QuoteSettlementMode(quote.settlement_mode)
    if current not in (QuoteSettlementMode.UNSET, target):
        raise ModeConflictError(
            f"Quote is locked to {current.value}; {target.value} is not allowed."
        )
    if current == target:
        return
    quote.settlement_mode = target
    quote.settlement_mode_locked_at = datetime.now().astimezone()
    await append_document_chain_event(
        session,
        company_id=quote.company_id,
        quote_id=quote.id,
        actor_user_id=actor_user_id,
        event_type=DocumentChainEventType.MODE_LOCKED,
        metadata={"mode": target.value},
    )


async def quote_has_converted_invoice(
    session: AsyncSession, *, company_id: uuid.UUID, quote_id: uuid.UUID
) -> bool:
    """Invoice.quote_id is the conversion authority; backlink is only a cache."""
    return (
        await session.scalar(
            select(Invoice.id)
            .where(Invoice.company_id == company_id, Invoice.quote_id == quote_id)
            .limit(1)
        )
    ) is not None


async def conversion_is_available(session: AsyncSession, quote: Quote) -> bool:
    """One predicate shared by projection and quote conversion command."""
    return (
        QuoteSettlementMode(quote.settlement_mode)
        in {
            QuoteSettlementMode.UNSET,
            QuoteSettlementMode.DIRECT_INVOICE,
            QuoteSettlementMode.RECEIPT_ONLY,
        }
        and QuoteStatus(quote.status)
        in {
            QuoteStatus.SENT,
            QuoteStatus.ACCEPTED,
            QuoteStatus.EXPIRED,
        }
        and not await quote_has_converted_invoice(
            session, company_id=quote.company_id, quote_id=quote.id
        )
    )


def compute_document_chain_totals(
    invoices: list[Invoice], quote_payments: list[Payment], *, quote: Quote | None = None
) -> DocumentChainTotals:
    """Return the one settlement projection shared by chain and Quote reads."""
    zero = Decimal("0")
    issued_charges = [
        invoice
        for invoice in invoices
        if InvoiceDocumentKind(invoice.document_kind) != InvoiceDocumentKind.CREDIT_NOTE
        and InvoiceStatus(invoice.status) in {InvoiceStatus.SENT, InvoiceStatus.COMPLETED}
    ]
    # M11.5 receipt-only cash is permanently linked to its one complete
    # Standard Invoice at conversion, including while it remains a DRAFT. It
    # must therefore carry that pending settlement basis in this projection;
    # otherwise the exact same cash becomes a false ``refund_due_amount``.
    # No ordinary DRAFT or unissued formal document is admitted here.
    receipt_only_draft_charges: list[Invoice] = []
    receipt_only_pending_quote_charge = False
    if (
        quote is not None
        and QuoteSettlementMode(quote.settlement_mode) == QuoteSettlementMode.RECEIPT_ONLY
    ):
        receipt_only_draft_charges = [
            invoice
            for invoice in invoices
            if InvoiceDocumentKind(invoice.document_kind) == InvoiceDocumentKind.STANDARD
            and InvoiceStatus(invoice.status) == InvoiceStatus.DRAFT
            and any(payment.invoice_id == invoice.id for payment in quote_payments)
        ]
        # Before conversion, or after deleting that DRAFT, quote-origin cash
        # has no Invoice row but is still deferred M11.5 settlement cash—not
        # an available refund. Pair it with the accepted Quote basis until a
        # complete Standard DRAFT takes ownership again.
        receipt_only_pending_quote_charge = not invoices and bool(quote_payments)
    settlement_charges = [*issued_charges, *receipt_only_draft_charges]
    positive_documents = [
        invoice
        for invoice in invoices
        if InvoiceDocumentKind(invoice.document_kind) != InvoiceDocumentKind.CREDIT_NOTE
    ]
    charge = sum(
        (Decimal(str(i.payable_before_payments)) for i in settlement_charges), zero
    )
    credit = sum((Decimal(str(i.credited_total)) for i in settlement_charges), zero)
    # Receipt-only cash can already be attached to its complete Standard while
    # that document is a DRAFT. Cash caches therefore use every positive node;
    # the narrow receipt-only pairing above supplies its settlement basis.
    incoming = sum(
        (Decimal(str(i.incoming_payment_total)) for i in positive_documents), zero
    )
    refund = sum((Decimal(str(i.refunded_total)) for i in settlement_charges), zero)
    base_charge = sum(
        (Decimal(str(i.base_payable_before_payments)) for i in settlement_charges), zero
    )
    base_credit = sum(
        (Decimal(str(i.base_credited_total)) for i in settlement_charges), zero
    )
    base_incoming = sum(
        (Decimal(str(i.base_incoming_payment_total)) for i in positive_documents), zero
    )
    base_refund = sum(
        (Decimal(str(i.base_refunded_total)) for i in settlement_charges), zero
    )
    if not invoices:
        incoming = sum((Decimal(str(p.amount)) for p in quote_payments), zero)
        base_incoming = sum((Decimal(str(p.base_amount)) for p in quote_payments), zero)
        if receipt_only_pending_quote_charge:
            assert quote is not None
            charge = Decimal(str(quote.total_incl_vat))
            base_charge = Decimal(str(quote.base_total_incl_vat))
    return DocumentChainTotals(
        charge_total=charge,
        credit_total=credit,
        incoming_payment_total=incoming,
        refund_total=refund,
        due_amount=max(charge - credit - incoming + refund, zero),
        refund_due_amount=max(incoming - refund - charge + credit, zero),
        base_charge_total=base_charge,
        base_credit_total=base_credit,
        base_incoming_payment_total=base_incoming,
        base_refund_total=base_refund,
        base_due_amount=max(base_charge - base_credit - base_incoming + base_refund, zero),
        base_refund_due_amount=max(base_incoming - base_refund - base_charge + base_credit, zero),
    )


async def direct_document_component_ids(
    session: AsyncSession, *, company_id: uuid.UUID, invoice_id: uuid.UUID
) -> list[uuid.UUID]:
    """Return the Step 6 direct correction/follow-up component in ID order.

    This is discovery only.  Mutating callers must subsequently lock every
    returned Invoice in canonical order before making a business decision.
    """
    return list(
        (
            await session.execute(
                text(
                    "WITH RECURSIVE component(id, path) AS ("
                    " SELECT CAST(:invoice_id AS uuid), ARRAY[CAST(:invoice_id AS uuid)]"
                    " UNION ALL "
                    " SELECT edge.id, component.path || edge.id "
                    " FROM component "
                    " JOIN LATERAL ("
                    "   SELECT correction.credit_note_id AS id "
                    "   FROM invoice_correction AS correction "
                    "   WHERE correction.company_id = CAST(:company_id AS uuid) "
                    "     AND correction.source_invoice_id = component.id "
                    "   UNION "
                    "   SELECT correction.source_invoice_id AS id "
                    "   FROM invoice_correction AS correction "
                    "   WHERE correction.company_id = CAST(:company_id AS uuid) "
                    "     AND correction.credit_note_id = component.id "
                    "   UNION "
                    "   SELECT relation.invoice_id AS id "
                    "   FROM invoice_relation AS relation "
                    "   WHERE relation.company_id = CAST(:company_id AS uuid) "
                    "     AND relation.related_credit_note_id = component.id "
                    "   UNION "
                    "   SELECT relation.related_credit_note_id AS id "
                    "   FROM invoice_relation AS relation "
                    "   WHERE relation.company_id = CAST(:company_id AS uuid) "
                    "     AND relation.invoice_id = component.id"
                    " ) AS edge ON TRUE "
                    " WHERE NOT edge.id = ANY(component.path)"
                    ") SELECT DISTINCT id FROM component ORDER BY id"
                ),
                {"invoice_id": invoice_id, "company_id": company_id},
            )
        ).scalars()
    )


async def get_document_chain(
    session: AsyncSession, *, company_id: uuid.UUID, quote_id: uuid.UUID
) -> DocumentChainRead | None:
    """Load the bounded-query, ordered projection rooted at a Quote."""
    await set_rls_company(session, company_id)
    quote = (
        await session.execute(
            select(Quote).where(Quote.id == quote_id, Quote.company_id == company_id)
        )
    ).scalar_one_or_none()
    if quote is None:
        return None
    invoice_rows = (
        await session.execute(
            select(
                Invoice,
                InvoiceCorrection.source_invoice_id,
                InvoiceRelation.related_credit_note_id,
                InvoiceRelation.relation_type,
            )
            .outerjoin(InvoiceCorrection, InvoiceCorrection.credit_note_id == Invoice.id)
            .outerjoin(InvoiceRelation, InvoiceRelation.invoice_id == Invoice.id)
            .where(Invoice.company_id == company_id, Invoice.quote_id == quote.id)
            .order_by(Invoice.created_at, Invoice.id)
        )
    ).all()
    invoices = [invoice for invoice, _, _, _ in invoice_rows]
    invoice_ids = [invoice.id for invoice in invoices]
    payments = list(
        (
            await session.execute(
                select(Payment)
                .where(
                    Payment.company_id == company_id,
                    Payment.deleted_at.is_(None),
                    or_(
                        Payment.quote_id == quote.id,
                        Payment.invoice_id.in_(invoice_ids or [uuid.uuid4()]),
                        Payment.credit_note_id.in_(invoice_ids or [uuid.uuid4()]),
                    ),
                )
                .order_by(Payment.created_at, Payment.id)
            )
        ).scalars()
    )
    events = list(
        (
            await session.execute(
                select(DocumentChainEvent)
                .where(
                    DocumentChainEvent.company_id == company_id,
                    or_(
                        DocumentChainEvent.quote_id == quote.id,
                        DocumentChainEvent.invoice_id.in_(invoice_ids or [uuid.uuid4()]),
                    ),
                )
                .order_by(DocumentChainEvent.event_order)
            )
        ).scalars()
    )
    final_invoice_ids = [
        invoice.id
        for invoice in invoices
        if InvoiceDocumentKind(invoice.document_kind) == InvoiceDocumentKind.FINAL
    ]
    application_rows = (
        list(
            (
                await session.execute(
                    select(FinalAdvanceApplication)
                    .where(
                        FinalAdvanceApplication.company_id == company_id,
                        FinalAdvanceApplication.final_invoice_id.in_(final_invoice_ids),
                    )
                    .order_by(FinalAdvanceApplication.final_invoice_id, FinalAdvanceApplication.sort_order)
                )
            ).scalars()
        )
        if final_invoice_ids
        else []
    )
    applications = [
        DocumentChainApplicationRead(
            final_invoice_id=row.final_invoice_id,
            advance_invoice_id=row.advance_invoice_id,
            occurred_on=next(
                invoice.invoice_date
                for invoice in invoices
                if invoice.id == row.final_invoice_id
            ),
            taxable_amount=Decimal(str(row.taxable_amount)),
            vat_amount=Decimal(str(row.vat_amount)),
            gross_amount=Decimal(str(row.gross_amount)),
        )
        for row in application_rows
    ]
    quote_payments = [payment for payment in payments if payment.quote_id == quote.id]
    totals = compute_document_chain_totals(invoices, quote_payments, quote=quote)
    node_rows: list[tuple[tuple[object, int, str], DocumentChainNodeRead]] = [
        (
            (quote.created_at, 0, str(quote.id)),
            DocumentChainNodeRead(
                id=quote.id,
                node_type="QUOTE",
                number=quote.quote_number,
                occurred_on=quote.quote_date,
            ),
        )
    ]
    node_rows.extend(
        (
            (invoice.created_at, 1, str(invoice.id)),
            DocumentChainNodeRead(
                id=invoice.id,
                node_type="INVOICE",
                document_kind=InvoiceDocumentKind(invoice.document_kind),
                number=invoice.invoice_number,
                status=invoice.status,
                occurred_on=invoice.invoice_date,
                charge_amount=Decimal(str(invoice.payable_before_payments)),
                credit_amount=Decimal(str(invoice.credited_total)),
                incoming_payment_amount=Decimal(str(invoice.incoming_payment_total)),
                refund_amount=Decimal(str(invoice.refunded_total)),
                due_amount=Decimal(str(invoice.due_amount)),
                refund_due_amount=Decimal(str(invoice.refund_due_amount)),
            ),
        )
        for invoice in invoices
    )
    node_rows.extend(
        (
            (payment.created_at, 2, str(payment.id)),
            DocumentChainNodeRead(
                id=payment.id,
                node_type="PAYMENT",
                number=None,
                occurred_on=payment.payment_date,
                incoming_payment_amount=(
                    Decimal(str(payment.amount))
                    if payment.direction.value == "INCOMING" else Decimal("0")
                ),
                refund_amount=(
                    Decimal(str(payment.amount))
                    if payment.direction.value == "REFUND" else Decimal("0")
                ),
            ),
        )
        for payment in payments
    )
    nodes = [node for _, node in sorted(node_rows, key=lambda row: row[0])]
    relations = [
        DocumentChainRelationRead(
            relation_type="QUOTE_TO_INVOICE", from_node_id=quote.id, to_node_id=invoice.id
        )
        for invoice in invoices
    ]
    credit_sources = {
        invoice.id: source_id
        for invoice, source_id, _, _ in invoice_rows
        if source_id is not None
    }
    relations.extend(
        DocumentChainRelationRead(
            relation_type="INVOICE_TO_CREDIT_NOTE",
            from_node_id=source_id,
            to_node_id=credit_id,
        )
        for credit_id, source_id in credit_sources.items()
    )
    relations.extend(
        DocumentChainRelationRead(
            relation_type=relation_type.value,
            from_node_id=related_credit_note_id,
            to_node_id=invoice.id,
        )
        for invoice, _, related_credit_note_id, relation_type in invoice_rows
        if related_credit_note_id is not None and relation_type is not None
    )
    for payment in payments:
        if payment.quote_id == quote.id:
            relations.append(
                DocumentChainRelationRead(
                    relation_type="QUOTE_TO_PAYMENT", from_node_id=quote.id, to_node_id=payment.id
                )
            )
        if payment.invoice_id is not None and payment.invoice_id in invoice_ids:
            relations.append(
                DocumentChainRelationRead(
                    relation_type="INVOICE_TO_PAYMENT",
                    from_node_id=payment.invoice_id,
                    to_node_id=payment.id,
                )
            )
        if payment.credit_note_id is not None and payment.credit_note_id in invoice_ids:
            relations.append(
                DocumentChainRelationRead(
                    relation_type="CREDIT_NOTE_TO_REFUND",
                    from_node_id=payment.credit_note_id,
                    to_node_id=payment.id,
                )
            )
    mode = QuoteSettlementMode(quote.settlement_mode)
    # Delayed import keeps the Advance command's event/mode helpers acyclic
    # while making this projection use the exact same structural predicate as
    # the create command.  It intentionally excludes amount/date input checks.
    from jai.services.advance import assess_advance_creation

    advance_creation = await assess_advance_creation(session, quote)
    issued_advance_exists = any(
        InvoiceDocumentKind(invoice.document_kind) == InvoiceDocumentKind.ADVANCE
        and InvoiceStatus(invoice.status) in {InvoiceStatus.SENT, InvoiceStatus.COMPLETED}
        for invoice in invoices
    )
    final_exists = any(
        InvoiceDocumentKind(invoice.document_kind) == InvoiceDocumentKind.FINAL
        for invoice in invoices
    )
    open_advance_exists = any(
        InvoiceDocumentKind(invoice.document_kind) == InvoiceDocumentKind.ADVANCE
        and InvoiceStatus(invoice.status) == InvoiceStatus.DRAFT
        for invoice in invoices
    )
    final_draft_exists = any(
        InvoiceDocumentKind(item.document_kind) == InvoiceDocumentKind.FINAL
        and InvoiceStatus(item.status) == InvoiceStatus.DRAFT
        for item in invoices
    )
    # One bulk line read gives every Advance-follow-up Credit its exact frozen
    # VAT bucket demand.  It replaces the former optimistic action card while
    # preserving the chain GET's constant query shape.
    credit_ids_for_capacity = [
        item.id for item in invoices if InvoiceDocumentKind(item.document_kind) == InvoiceDocumentKind.CREDIT_NOTE
    ]
    credit_bucket_rows = (
        list(
            (
                await session.execute(
                    select(
                        InvoiceLine.invoice_id,
                        InvoiceLine.vat_rate_id,
                        InvoiceLine.taxable_amount,
                        InvoiceLine.vat_total,
                    ).where(InvoiceLine.invoice_id.in_(credit_ids_for_capacity))
                )
            ).all()
        )
        if credit_ids_for_capacity
        else []
    )
    credit_bucket_requirements: dict[uuid.UUID, dict[uuid.UUID, tuple[Decimal, Decimal]]] = {}
    for credit_id, rate_id, taxable, vat in credit_bucket_rows:
        if rate_id is None:
            continue
        required = credit_bucket_requirements.setdefault(credit_id, {})
        old_net, old_vat = required.get(rate_id, (Decimal("0"), Decimal("0")))
        required[rate_id] = (old_net + Decimal(str(taxable)), old_vat + Decimal(str(vat)))
    available_advance_capacity = {
        bucket.vat_rate_id: (bucket.taxable_amount, bucket.vat_amount)
        for bucket in advance_creation.remaining_buckets
    }
    supplemental_standard_ids = {
        invoice.id
        for invoice, _, _, relation_type in invoice_rows
        if relation_type == InvoiceRelationType.COMPENSATES_CREDIT
    }
    cancellation_sources = [
        item for item in invoices
        if InvoiceStatus(item.status) in {InvoiceStatus.SENT, InvoiceStatus.COMPLETED}
        and (
            InvoiceDocumentKind(item.document_kind)
            in {InvoiceDocumentKind.ADVANCE, InvoiceDocumentKind.FINAL}
            or (
                InvoiceDocumentKind(item.document_kind) == InvoiceDocumentKind.STANDARD
                and item.id in supplemental_standard_ids
            )
        )
        and Decimal(str(item.credited_total)) < Decimal(str(item.payable_before_payments))
    ]
    cancellation = cancellation_eligibility(
        mode=mode,
        final_draft_exists=final_draft_exists,
        has_remaining_formal_charge=bool(cancellation_sources),
    )
    actions = [
        _action("CONVERT_TO_INVOICE", await conversion_is_available(session, quote), target_id=quote.id, target_type="QUOTE"),
        _action(
            "RECORD_QUOTE_PAYMENT",
            (
                mode in {QuoteSettlementMode.UNSET, QuoteSettlementMode.RECEIPT_ONLY}
                and QuoteStatus(quote.status) == QuoteStatus.ACCEPTED
                and quote.vat_treatment_code == "NL_DOMESTIC"
                and not invoices
            ),
            target_id=quote.id, target_type="QUOTE",
        ),
        _action("CREATE_ADVANCE", advance_creation.available, getattr(advance_creation, "reason_code", None), target_id=quote.id, target_type="QUOTE"),
        _action(
            "CREATE_FINAL",
            (
                mode == QuoteSettlementMode.FORMAL_ADVANCE
                and QuoteStatus(quote.status) == QuoteStatus.ACCEPTED
                and issued_advance_exists
                and not final_exists
                and not open_advance_exists
            ),
            target_id=quote.id, target_type="QUOTE",
        ),
        _action("CREATE_PROJECT_CANCELLATION", cancellation.available, cancellation.reason_code, target_id=quote.id, target_type="QUOTE"),
    ]
    source_by_credit = {
        credit_id: source_id for credit_id, source_id in credit_sources.items()
    }
    invoice_by_id = {item.id: item for item in invoices}
    followed_credit_ids = {
        related_credit_id
        for _, _, related_credit_id, relation_type in invoice_rows
        if related_credit_id is not None and relation_type is not None
    }
    for item in invoices:
        kind = InvoiceDocumentKind(item.document_kind)
        if kind != InvoiceDocumentKind.CREDIT_NOTE:
            eligibility = credit_note_eligibility(item, final_draft_exists=final_draft_exists)
            actions.append(_action("CREATE_CREDIT_NOTE", eligibility.available, eligibility.reason_code, target_id=item.id, target_type="INVOICE"))
        else:
            replacement, compensation = followup_eligibility(
                item,
                invoice_by_id.get(source_by_credit.get(item.id)),
                mode=mode,
                final_exists=final_exists,
                open_advance_draft_exists=open_advance_exists,
                existing_followup=item.id in followed_credit_ids,
                advance_capacity_confirmed=advance_replacement_capacity_eligibility(
                    available_advance_capacity,
                    credit_bucket_requirements.get(item.id, {}),
                ),
            )
            actions.extend([
                _action("CREATE_REPLACEMENT", replacement.available, replacement.reason_code, target_id=item.id, target_type="INVOICE", followup_context=_followup_context(credit=item, source=invoice_by_id.get(source_by_credit.get(item.id)), relation_type=InvoiceRelationType.REPLACEMENT_OF, mode=mode, final_exists=final_exists)),
                _action("CREATE_COMPENSATING_INVOICE", compensation.available, compensation.reason_code, target_id=item.id, target_type="INVOICE", followup_context=_followup_context(credit=item, source=invoice_by_id.get(source_by_credit.get(item.id)), relation_type=InvoiceRelationType.COMPENSATES_CREDIT, mode=mode, final_exists=final_exists)),
            ])
    event_reads = [
        DocumentChainEventRead(
            id=event.id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            event_order=event.event_order,
            quote_id=event.quote_id,
            invoice_id=event.invoice_id,
            actor_user_id=event.actor_user_id,
            metadata=event.metadata_json,
        )
        for event in events
    ]
    return DocumentChainRead(
        quote_id=quote.id,
        quote_number=quote.quote_number,
        settlement_mode=mode,
        settlement_mode_locked_at=quote.settlement_mode_locked_at,
        nodes=nodes,
        relations=relations,
        events=event_reads,
        timeline=_timeline(nodes, event_reads, relations, applications),
        totals=totals,
        quote_total=Decimal(str(quote.total_incl_vat)),
        available_actions=actions,
    )


async def get_invoice_document_chain(
    session: AsyncSession, *, company_id: uuid.UUID, invoice_id: uuid.UUID
) -> DocumentChainRead | None:
    await set_rls_company(session, company_id)
    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == company_id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        return None
    if invoice.quote_id is None:
        # Direct documents have no Quote root.  Their correction family is an
        # undirected graph: source <-> Credit and Credit <-> positive follow-up.
        # One company-scoped recursive CTE finds the complete component from
        # any member.  The UUID path prevents corrupted/cyclic provenance from
        # causing an unbounded walk; all subsequent reads are fixed bulk queries.
        component_ids = await direct_document_component_ids(
            session, company_id=company_id, invoice_id=invoice.id
        )
        direct_invoices = list(
            (
                await session.execute(
                    select(Invoice)
                    .where(
                        Invoice.company_id == company_id,
                        Invoice.id.in_(component_ids),
                    )
                    .order_by(Invoice.id)
                )
            ).scalars()
        )
        direct_invoice_ids = [item.id for item in direct_invoices]
        corrections = list(
            (
                await session.execute(
                    select(InvoiceCorrection)
                    .where(
                        InvoiceCorrection.company_id == company_id,
                        InvoiceCorrection.source_invoice_id.in_(direct_invoice_ids),
                        InvoiceCorrection.credit_note_id.in_(direct_invoice_ids),
                    )
                    .order_by(InvoiceCorrection.id)
                )
            ).scalars()
        )
        related_positive_rows = list(
            (
                await session.execute(
                    select(InvoiceRelation)
                    .where(
                        InvoiceRelation.company_id == company_id,
                        InvoiceRelation.related_credit_note_id.in_(direct_invoice_ids),
                        InvoiceRelation.invoice_id.in_(direct_invoice_ids),
                    )
                    .order_by(InvoiceRelation.created_at, InvoiceRelation.id)
                )
            ).scalars()
        )
        payments = list(
            (
                await session.execute(
                    select(Payment)
                    .where(
                        Payment.company_id == company_id,
                        Payment.deleted_at.is_(None),
                        or_(
                            Payment.invoice_id.in_(direct_invoice_ids),
                            Payment.credit_note_id.in_(direct_invoice_ids),
                        ),
                    )
                    .order_by(Payment.created_at, Payment.id)
                )
            ).scalars()
        )
        events = list(
            (
                await session.execute(
                    select(DocumentChainEvent)
                    .where(
                        DocumentChainEvent.company_id == company_id,
                        DocumentChainEvent.invoice_id.in_(direct_invoice_ids),
                    )
                    .order_by(DocumentChainEvent.event_order)
                )
            ).scalars()
        )
        nodes = [
            *[
                DocumentChainNodeRead(
                    id=item.id,
                    node_type="INVOICE",
                    document_kind=InvoiceDocumentKind(item.document_kind),
                    number=item.invoice_number,
                    status=item.status,
                    occurred_on=item.invoice_date,
                    charge_amount=Decimal(str(item.payable_before_payments)),
                    credit_amount=Decimal(str(item.credited_total)),
                    incoming_payment_amount=Decimal(str(item.incoming_payment_total)),
                    refund_amount=Decimal(str(item.refunded_total)),
                    due_amount=Decimal(str(item.due_amount)),
                    refund_due_amount=Decimal(str(item.refund_due_amount)),
                )
                for item in direct_invoices
            ],
            *[
                DocumentChainNodeRead(
                    id=payment.id,
                    node_type="PAYMENT",
                    occurred_on=payment.payment_date,
                    incoming_payment_amount=(
                        Decimal(str(payment.amount))
                        if payment.direction.value == "INCOMING" else Decimal("0")
                    ),
                    refund_amount=(
                        Decimal(str(payment.amount))
                        if payment.direction.value == "REFUND" else Decimal("0")
                    ),
                )
                for payment in payments
            ],
        ]
        nodes.sort(key=lambda node: (node.occurred_on, str(node.id)))
        # All action cards are target-scoped even without a Quote root.  The
        # graph can be opened through its source, Credit, or positive follow-up
        # so returning actions only for the requested node makes direct chains
        # needlessly unreachable from their own pages.
        direct_by_id = {item.id: item for item in direct_invoices}
        direct_credit_sources = {
            correction.credit_note_id: correction.source_invoice_id
            for correction in corrections
        }
        direct_followed_credit_ids = {
            relation.related_credit_note_id for relation in related_positive_rows
        }
        direct_actions = [
            _action("CONVERT_TO_INVOICE", False, "ACTION_REQUIRES_QUOTE"),
            _action("RECORD_QUOTE_PAYMENT", False, "ACTION_REQUIRES_QUOTE"),
            _action("CREATE_ADVANCE", False, "ACTION_REQUIRES_QUOTE"),
            _action("CREATE_FINAL", False, "ACTION_REQUIRES_QUOTE"),
            _action("CREATE_PROJECT_CANCELLATION", False, "ACTION_REQUIRES_QUOTE"),
        ]
        for item in direct_invoices:
            if InvoiceDocumentKind(item.document_kind) != InvoiceDocumentKind.CREDIT_NOTE:
                eligibility = credit_note_eligibility(item, final_draft_exists=False)
                direct_actions.append(
                    _action(
                        "CREATE_CREDIT_NOTE", eligibility.available, eligibility.reason_code,
                        target_id=item.id, target_type="INVOICE",
                    )
                )
                continue
            replacement, compensation = followup_eligibility(
                item,
                direct_by_id.get(direct_credit_sources[item.id])
                if item.id in direct_credit_sources
                else None,
                mode=None,
                final_exists=False,
                open_advance_draft_exists=False,
                existing_followup=item.id in direct_followed_credit_ids,
            )
            direct_actions.extend([
                _action("CREATE_REPLACEMENT", replacement.available, replacement.reason_code, target_id=item.id, target_type="INVOICE", followup_context=_followup_context(credit=item, source=direct_by_id.get(direct_credit_sources[item.id]) if item.id in direct_credit_sources else None, relation_type=InvoiceRelationType.REPLACEMENT_OF, mode=None, final_exists=False)),
                _action("CREATE_COMPENSATING_INVOICE", compensation.available, compensation.reason_code, target_id=item.id, target_type="INVOICE", followup_context=_followup_context(credit=item, source=direct_by_id.get(direct_credit_sources[item.id]) if item.id in direct_credit_sources else None, relation_type=InvoiceRelationType.COMPENSATES_CREDIT, mode=None, final_exists=False)),
            ])
        direct_relations = [
            DocumentChainRelationRead(
                relation_type="INVOICE_TO_PAYMENT", from_node_id=payment.invoice_id, to_node_id=payment.id
            )
            for payment in payments if payment.invoice_id is not None
        ] + [
            DocumentChainRelationRead(
                relation_type="CREDIT_NOTE_TO_REFUND", from_node_id=payment.credit_note_id, to_node_id=payment.id
            )
            for payment in payments if payment.credit_note_id is not None
        ] + [
            DocumentChainRelationRead(
                relation_type="INVOICE_TO_CREDIT_NOTE", from_node_id=correction.source_invoice_id, to_node_id=correction.credit_note_id
            )
            for correction in corrections
        ] + [
            DocumentChainRelationRead(
                relation_type=relation.relation_type.value, from_node_id=relation.related_credit_note_id, to_node_id=relation.invoice_id
            )
            for relation in related_positive_rows
        ]
        direct_events = [
            DocumentChainEventRead(
                id=event.id, event_type=event.event_type, occurred_at=event.occurred_at,
                event_order=event.event_order,
                quote_id=event.quote_id, invoice_id=event.invoice_id,
                actor_user_id=event.actor_user_id, metadata=event.metadata_json,
            )
            for event in events
        ]
        return DocumentChainRead(
            settlement_mode=QuoteSettlementMode.UNSET,
            nodes=nodes,
            relations=direct_relations,
            events=direct_events,
            timeline=_timeline(nodes, direct_events, direct_relations),
            totals=compute_document_chain_totals(direct_invoices, []),
            available_actions=direct_actions,
        )
    chain = await get_document_chain(session, company_id=company_id, quote_id=invoice.quote_id)
    if chain is None:
        return None
    return chain
