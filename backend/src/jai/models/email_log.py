"""EmailLog ORM model – audit record for every document email-send attempt (M9 step 6).

Design decisions (from M9.md §数据模型 + D6/D7):
- Generic ``(related_type, related_id)`` pair references the document (red-line 6:
  no multiple nullable FKs).  ``related_id`` is a **soft reference** with no FK
  constraint so the audit log survives document deletion.
- ``company_id`` FK uses RESTRICT – the log row cannot outlive its company.
- ``creator_id`` FK uses SET NULL – the log row survives user deletion.
- ``error_message`` must NEVER contain SMTP credentials (D7 / D6 requirement).
- ``body_snapshot`` stores the rendered HTML body as sent – useful for auditing
  what the customer actually received.
- No storage paths, no passwords, no API keys (red-line 7).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from jai.db import Base
from jai.models._enums import EmailRelatedType, EmailStatus

# Enum column type declarations.  ``create_type=False`` prevents SQLAlchemy
# from emitting ``CREATE TYPE`` statements – the PG enum types are created by
# Alembic migration 0023 using raw SQL.
_RELATED_TYPE_COL = Enum(
    EmailRelatedType,
    name="emailrelatedtype",
    create_type=False,
)
_STATUS_COL = Enum(
    EmailStatus,
    name="emailstatus",
    create_type=False,
)


class EmailLog(Base):
    """Audit record for a single document email-send attempt."""

    __tablename__ = "email_log"

    # -- Primary key ----------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # -- Tenant scope (red-line 2): FK RESTRICT so log cannot outlive company --
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # -- Generic document reference (red-line 6: no multiple nullable FKs) ----
    related_type: Mapped[EmailRelatedType] = mapped_column(
        _RELATED_TYPE_COL,
        nullable=False,
    )
    related_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment=(
            "Soft reference – no FK constraint so the audit log survives "
            "document deletion.  Index with company_id + related_type for "
            "per-document log queries."
        ),
    )

    # -- Envelope fields ------------------------------------------------------
    to_email: Mapped[str] = mapped_column(Text, nullable=False)
    cc: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Comma-separated CC addresses; NULL when no CC was given.",
    )

    # -- Content snapshot (as actually sent) ----------------------------------
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Rendered HTML body that was sent to the recipient.",
    )
    attachment_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_artifact.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Exact retained PDF attached to a successful M12 formal send.",
    )
    locale: Mapped[str | None] = mapped_column(
        String(5),
        nullable=True,
        comment="Language used for rendering ('en' / 'zh'); NULL = unknown.",
    )

    # -- Send result ----------------------------------------------------------
    status: Mapped[EmailStatus] = mapped_column(
        _STATUS_COL,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Human-readable failure reason.  "
            "MUST NOT contain SMTP credentials or passwords (D6/D7)."
        ),
    )

    sent_copy_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="NOT_ATTEMPTED", server_default="NOT_ATTEMPTED"
    )
    sent_copy_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_copy_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- Audit timestamps -----------------------------------------------------
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the SMTP server accepted the message; NULL on failure.",
    )

    # -- Composite index for per-document log queries -------------------------
    __table_args__ = (
        CheckConstraint(
            "sent_copy_status IN ('NOT_ATTEMPTED', 'SAVED', 'FAILED')",
            name="ck_email_log_sent_copy_status",
        ),
        Index(
            "ix_email_log_company_related",
            "company_id",
            "related_type",
            "related_id",
        ),
    )
