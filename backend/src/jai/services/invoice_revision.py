"""Read-only SI2 history and eligibility projections."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jai.db import set_rls_company
from jai.models._enums import (
    EmailRelatedType,
    EmailStatus,
    InvoiceDocumentKind,
    InvoiceStatus,
    QuoteSettlementMode,
)
from jai.models.document import (
    FinalAdvanceApplication,
    InvoiceCorrection,
    InvoiceRelation,
    InvoiceRevision,
)
from jai.models.email_log import EmailLog
from jai.models.invoice import Invoice
from jai.models.payment import Payment
from jai.models.quote import Quote
from jai.schemas.invoice import InvoiceRevisionEligibilityRead
from jai.schemas.invoice_revision import InvoiceRevisionListRead, InvoiceRevisionRead
from jai.services.document_actions import invoice_revision_eligibility


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
