"""Company-owned external sales sources, pending entries and originals."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jai.db import Base


class ExternalSalesSource(Base):
    __tablename__ = "external_sales_source"
    __table_args__ = (Index("ix_external_sales_source_company", "company_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    default_include_in_pl: Mapped[bool | None] = mapped_column(Boolean)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ExternalSale(Base):
    __tablename__ = "external_sale"
    __table_args__ = (
        Index("ix_external_sale_company_created", "company_id", "created_at", "id"),
        Index("ix_external_sale_source_number", "company_id", "source_id", "original_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_sales_source.id", ondelete="RESTRICT")
    )
    original_number: Mapped[str | None] = mapped_column(Text)
    document_date: Mapped[date | None] = mapped_column(Date)
    covered_from: Mapped[date | None] = mapped_column(Date)
    covered_to: Mapped[date | None] = mapped_column(Date)
    counterparty: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    include_in_pl: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT")
    )
    posted_source_name: Mapped[str | None] = mapped_column(Text)
    original_net_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    original_vat_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    mismatch_ack_reason: Mapped[str | None] = mapped_column(Text)
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    source: Mapped[ExternalSalesSource | None] = relationship()
    tax_summaries: Mapped[list[ExternalSaleTax]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ExternalSaleTax.id"
    )
    files: Mapped[list[ExternalSaleFile]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="ExternalSaleFile.created_at"
    )
    postings: Mapped[list[ExternalSalePosting]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExternalSalePosting.created_at",
    )


class ExternalSalePosting(Base):
    __tablename__ = "external_sale_posting"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    external_sale_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_sale.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ExternalSaleTax(Base):
    __tablename__ = "external_sale_tax"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    external_sale_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_sale.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    treatment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vat_treatment.id", ondelete="RESTRICT")
    )
    treatment_code: Mapped[str | None] = mapped_column(Text)
    treatment_label: Mapped[str | None] = mapped_column(Text)
    rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vat_rate.id", ondelete="RESTRICT")
    )
    rate_label: Mapped[str | None] = mapped_column(Text)
    rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    vat_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))


class ExternalSaleFile(Base):
    __tablename__ = "external_sale_file"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    external_sale_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_sale.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
