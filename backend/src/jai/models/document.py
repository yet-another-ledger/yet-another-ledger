"""M12 issue-time document snapshots and immutable credit basis rows."""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jai.db import Base
from jai.models._enums import (
    DocumentChainEventType,
    InvoiceRelationType,
    PartySnapshotProvenance,
)

_MONEY = Numeric(18, 3)
_RATE = Numeric(6, 3)


class FinalAdvanceApplication(Base):
    """Immutable amount of one issued Advance applied to a Final."""

    __tablename__ = "final_advance_application"
    __table_args__ = (
        UniqueConstraint(
            "final_invoice_id", "advance_invoice_id", name="uq_final_advance_application"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    final_invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advance_invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    advance_invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    advance_invoice_number: Mapped[str] = mapped_column(Text, nullable=False)
    taxable_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_taxable_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    taxes: Mapped[list[FinalAdvanceApplicationTax]] = relationship(
        "FinalAdvanceApplicationTax",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="FinalAdvanceApplicationTax.sort_order",
    )


class FinalAdvanceApplicationTax(Base):
    """Immutable per-VAT-bucket snapshot for a Final Advance application."""

    __tablename__ = "final_advance_application_tax"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "source_vat_rate_id", name="uq_final_application_tax_bucket"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("final_advance_application.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_vat_rate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_vat_rate_label: Mapped[str] = mapped_column(Text, nullable=False)
    source_vat_rate_percent: Mapped[Decimal] = mapped_column(_RATE, nullable=False)
    vat_treatment_code: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_effect: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_requires_icp: Mapped[bool] = mapped_column(Boolean, nullable=False)
    taxable_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_taxable_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)


class InvoicePartySnapshot(Base):
    """One immutable seller/buyer identity snapshot for an issued invoice."""

    __tablename__ = "invoice_party_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    provenance: Mapped[PartySnapshotProvenance] = mapped_column(nullable=False)
    seller_name: Mapped[str] = mapped_column(Text, nullable=False)
    seller_legal_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_vat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_coc_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_address: Mapped[dict[str, str | None]] = mapped_column(JSONB, nullable=False)
    buyer_name: Mapped[str] = mapped_column(Text, nullable=False)
    buyer_company_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_contact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_vat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_address: Mapped[dict[str, str | None]] = mapped_column(JSONB, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    logo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("binary_asset.id", ondelete="RESTRICT"),
        nullable=True,
        comment="Retained issue-time logo; logo maintenance keeps referenced assets.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class InvoiceCreditBasisLine(Base):
    """Immutable issued charge basis used by later source-bound Credit Notes."""

    __tablename__ = "invoice_credit_basis_line"
    __table_args__ = (UniqueConstraint("invoice_line_id", name="uq_credit_basis_invoice_line"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_line.id", ondelete="CASCADE"),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    unit_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    vat_rate_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    vat_rate_percent: Mapped[Decimal | None] = mapped_column(_RATE, nullable=True)
    # The nominal rate is not necessarily the applied rate (for example a
    # reverse-charge invoice has a 21% nominal rate but a frozen 0% effect).
    # Credit Notes must replay this source snapshot rather than infer it.
    effective_vat_percent: Mapped[Decimal | None] = mapped_column(_RATE, nullable=True)
    vat_treatment_code: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_effect: Mapped[str] = mapped_column(Text, nullable=False)
    vat_treatment_requires_icp: Mapped[bool] = mapped_column(Boolean, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_net_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)


class InvoiceCorrection(Base):
    """One source-bound Credit Note correction.

    Selection rows are deliberately persisted while the Credit Note is a
    DRAFT, but the aggregate and ``affects_revenue`` values are populated only
    at issue.  This makes a draft reviewable without accidentally reserving
    coverage or creating a reporting event.
    """

    __tablename__ = "invoice_correction"
    __table_args__ = (UniqueConstraint("credit_note_id", name="uq_invoice_correction_credit"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Preserve whether the author selected the semantic "full remaining" mode
    # rather than merely selecting every currently visible basis row.  Those
    # two requests can produce the same lines today but must round-trip with
    # their distinct future replay semantics.
    full_remaining: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # ``MIGRATED_AMBIGUOUS`` denotes a pre-0041 DRAFT whose old materialised
    # rows cannot tell full coverage from explicit-all selection.  New service
    # writes are always ``NATIVE`` and carry an explicit boolean above.
    intent_provenance: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="NATIVE"
    )
    issued_net_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    issued_vat_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    issued_gross_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    issued_base_net_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    issued_base_vat_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    issued_base_gross_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    affects_revenue: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    lines: Mapped[list[InvoiceCorrectionLine]] = relationship(
        "InvoiceCorrectionLine", cascade="all, delete-orphan", lazy="selectin", order_by="InvoiceCorrectionLine.sort_order"
    )


class InvoiceCorrectionLine(Base):
    """A frozen Credit selection derived exclusively from a source basis row."""

    __tablename__ = "invoice_correction_line"
    __table_args__ = (UniqueConstraint("correction_id", "source_basis_line_id", name="uq_correction_basis"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    correction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_correction.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_basis_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_credit_basis_line.id", ondelete="RESTRICT"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    input_mode: Mapped[str] = mapped_column(Text, nullable=False)
    input_quantity: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    input_gross_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_net_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_vat_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    base_gross_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)


class InvoiceRelation(Base):
    """Immutable provenance from one positive DRAFT to an issued Credit."""

    __tablename__ = "invoice_relation"
    __table_args__ = (
        UniqueConstraint("invoice_id", name="uq_invoice_relation_invoice"),
        UniqueConstraint("related_credit_note_id", name="uq_invoice_relation_credit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    related_credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    relation_type: Mapped[InvoiceRelationType] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DocumentChainEvent(Base):
    """An immutable, safe projection fact written with a chain mutation.

    Metadata deliberately stores only stable IDs, enum codes and money/date
    snapshots.  It must never be used as a general request/audit payload.
    """

    __tablename__ = "document_chain_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quote.id", ondelete="CASCADE"), nullable=True, index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[DocumentChainEventType] = mapped_column(nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
    # Stable sequence order avoids transaction-timestamp/UUID tie ambiguity.
    event_order: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        unique=True,
        server_default=text("nextval('document_chain_event_order_seq')"),
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)


class InvoiceRevision(Base):
    """Append-only, typed business snapshots for an actual Invoice revision."""

    __tablename__ = "invoice_revision"
    __table_args__ = (UniqueConstraint("revision_order", name="uq_invoice_revision_order"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revision_order: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("nextval('invoice_revision_order_seq')")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    undelivered_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    before_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    after_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
