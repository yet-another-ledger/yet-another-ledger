"""ExpenseAttachment ORM model (M8 step 2).

Design
------
- Rooted on ``company_id`` (red-line 2, FK RESTRICT).
- ``expense_id`` FK ON DELETE CASCADE: when an expense is deleted the
  attachment metadata rows are automatically removed (red-line 3).
  The *disk files* are removed by the service layer (D12).
- ``storage_key`` is an opaque key understood by ``services/storage``;
  it is NEVER returned in any API response (schema layer omits it).
- ``filename`` is the client-supplied original filename kept for display
  only; it is never used in storage path construction (red-line 7).
- ``sha256`` column is reserved for future deduplication; nullable for now.
- ``creator_id`` FK SET NULL: preserves attachment metadata if user is deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from jai.db import Base


class ExpenseAttachment(Base):
    """Metadata for a receipt file attached to an expense."""

    __tablename__ = "expense_attachment"
    __table_args__ = (
        CheckConstraint(
            "(expense_id IS NOT NULL AND receipt_inbox_id IS NULL) OR "
            "(expense_id IS NULL AND receipt_inbox_id IS NOT NULL)",
            name="ck_expense_attachment_exactly_one_parent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # -- Tenant scoping (red-line 2) ------------------------------------------
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # -- Parent expense (CASCADE: delete expense → delete metadata rows, D12) --
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expense.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    receipt_inbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("receipt_inbox.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # -- Storage (opaque key; NEVER exposed in API responses) -----------------
    storage_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Opaque storage key understood by services/storage. NOT returned in API.",
    )

    # -- Display metadata -----------------------------------------------------
    filename: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Client-supplied original filename; display only, never used in storage path.",
    )
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    # -- Reserved for future dedup (sha256 of content) ------------------------
    sha256: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="SHA-256 hex digest; reserved for future deduplication.",
    )

    # -- Creator (SET NULL: preserve attachment if user is deleted) -----------
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- Timestamps -----------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
