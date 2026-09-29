"""Persistent receipt inbox state, separate from accounting Expenses."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jai.db import Base


class ReceiptInbox(Base):
    """One pending receipt group or its retained confirmed history row."""

    __tablename__ = "receipt_inbox"
    __table_args__ = (
        UniqueConstraint("expense_id", name="uq_receipt_inbox_expense_id"),
        Index("ix_receipt_inbox_company_created", "company_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("expense.id", ondelete="SET NULL"), nullable=True
    )
    ai_suggestion: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    ai_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'NOT_RUN'"))
    ai_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extraction_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ai_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
