"""Immutable PDF originals and Quote appendix membership."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from jai.db import Base

if TYPE_CHECKING:
    from jai.models.quote import Quote


class PdfAppendixFile(Base):
    __tablename__ = "pdf_appendix_file"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_pdf_appendix_file_size_positive"),
        CheckConstraint("page_count > 0", name="ck_pdf_appendix_file_pages_positive"),
        CheckConstraint("length(sha256) = 64", name="ck_pdf_appendix_file_sha256"),
        UniqueConstraint("company_id", "id", name="uq_pdf_appendix_file_company_id"),
        UniqueConstraint("company_id", "id", "owner_quote_id", name="uq_pdf_appendix_file_owner"),
        Index("ix_pdf_appendix_file_company_library", "company_id", "owner_quote_id", "created_at"),
        ForeignKeyConstraint(
            ["company_id", "owner_quote_id"],
            ["quote.company_id", "quote.id"],
            name="fk_pdf_appendix_owner_company",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    owner_quote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_bytes: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    owner_quote: Mapped[Quote | None] = relationship(
        "Quote", back_populates="own_appendix_files", foreign_keys=[owner_quote_id]
    )


class QuoteAppendix(Base):
    __tablename__ = "quote_appendix"
    __table_args__ = (
        UniqueConstraint("quote_id", "file_id", name="uq_quote_appendix_file"),
        UniqueConstraint("quote_id", "sort_order", name="uq_quote_appendix_order"),
        CheckConstraint("sort_order >= 0", name="ck_quote_appendix_order_nonnegative"),
        Index("ix_quote_appendix_company_quote", "company_id", "quote_id"),
        ForeignKeyConstraint(
            ["company_id", "quote_id"],
            ["quote.company_id", "quote.id"],
            name="fk_quote_appendix_quote_company",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "pdf_appendix_file.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"
        ),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[Quote] = relationship("Quote", back_populates="appendix_memberships")
    file: Mapped[PdfAppendixFile] = relationship("PdfAppendixFile", foreign_keys=[file_id])
