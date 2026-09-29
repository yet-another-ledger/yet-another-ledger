"""Add receipt inbox persistence and attachment parent ownership.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role_identifier() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be a non-empty PostgreSQL role identifier.")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute(
        """CREATE TABLE receipt_inbox (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            company_id UUID NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            expense_id UUID,
            ai_suggestion JSONB,
            ai_state TEXT NOT NULL DEFAULT 'NOT_RUN',
            ai_error_code TEXT,
            file_revision INTEGER NOT NULL DEFAULT 0,
            extraction_generation INTEGER NOT NULL DEFAULT 0,
            ai_started_at TIMESTAMPTZ,
            ai_completed_at TIMESTAMPTZ,
            creator_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            confirmed_at TIMESTAMPTZ,
            CONSTRAINT pk_receipt_inbox PRIMARY KEY (id),
            CONSTRAINT fk_receipt_inbox_company FOREIGN KEY (company_id)
                REFERENCES company(id) ON DELETE RESTRICT,
            CONSTRAINT fk_receipt_inbox_expense FOREIGN KEY (expense_id)
                REFERENCES expense(id) ON DELETE SET NULL,
            CONSTRAINT fk_receipt_inbox_creator FOREIGN KEY (creator_id)
                REFERENCES "user"(id) ON DELETE SET NULL,
            CONSTRAINT ck_receipt_inbox_status CHECK (status IN ('PENDING', 'CONFIRMED')),
            CONSTRAINT ck_receipt_inbox_ai_state CHECK
                (ai_state IN ('NOT_RUN', 'RUNNING', 'READY', 'UNAVAILABLE', 'FAILED')),
            CONSTRAINT ck_receipt_inbox_counters CHECK
                (file_revision >= 0 AND extraction_generation >= 0)
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_receipt_inbox_expense_id ON receipt_inbox(expense_id)")
    op.execute("CREATE INDEX ix_receipt_inbox_company_id ON receipt_inbox(company_id)")
    op.execute(
        "CREATE INDEX ix_receipt_inbox_company_created ON receipt_inbox(company_id, created_at, id)"
    )

    # Existing attachments remain Expense-owned. Nullable first, then validate
    # the new FK and XOR check against the historical table.
    op.execute("ALTER TABLE expense_attachment ALTER COLUMN expense_id DROP NOT NULL")
    op.execute("ALTER TABLE expense_attachment ADD COLUMN receipt_inbox_id UUID")
    op.execute(
        "ALTER TABLE expense_attachment ADD CONSTRAINT fk_expense_attachment_receipt_inbox "
        "FOREIGN KEY (receipt_inbox_id) REFERENCES receipt_inbox(id) ON DELETE CASCADE NOT VALID"
    )
    op.execute(
        "CREATE INDEX ix_expense_attachment_receipt_inbox_id "
        "ON expense_attachment(receipt_inbox_id)"
    )
    op.execute(
        "ALTER TABLE expense_attachment ADD CONSTRAINT ck_expense_attachment_exactly_one_parent "
        "CHECK ((expense_id IS NOT NULL AND receipt_inbox_id IS NULL) OR "
        "(expense_id IS NULL AND receipt_inbox_id IS NOT NULL)) NOT VALID"
    )
    op.execute(
        "ALTER TABLE expense_attachment VALIDATE CONSTRAINT fk_expense_attachment_receipt_inbox"
    )
    op.execute(
        "ALTER TABLE expense_attachment VALIDATE CONSTRAINT "
        "ck_expense_attachment_exactly_one_parent"
    )

    tenant = "company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid"
    op.execute("ALTER TABLE receipt_inbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE receipt_inbox FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY receipt_inbox_company_isolation ON receipt_inbox "
        f"USING ({tenant}) WITH CHECK ({tenant})"
    )
    app_role = _role_identifier()
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON receipt_inbox TO {app_role}")


def downgrade() -> None:
    # Never make a live inbox silently disappear during a code rollback.
    # The migration role owns this table but is NOBYPASSRLS, so FORCE RLS
    # hides every tenant's rows without a company context. ALTER TABLE takes
    # an exclusive lock and is transactional: a rejected downgrade restores
    # RLS, while an empty table is dropped in this same transaction.
    op.execute("ALTER TABLE receipt_inbox DISABLE ROW LEVEL SECURITY")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM receipt_inbox) THEN "
        "RAISE EXCEPTION 'cannot downgrade receipt inbox while inbox rows exist'; "
        "END IF; END $$"
    )
    op.execute(
        "ALTER TABLE expense_attachment DROP CONSTRAINT ck_expense_attachment_exactly_one_parent"
    )
    op.execute(
        "ALTER TABLE expense_attachment DROP CONSTRAINT fk_expense_attachment_receipt_inbox"
    )
    op.execute("DROP INDEX ix_expense_attachment_receipt_inbox_id")
    op.execute("ALTER TABLE expense_attachment DROP COLUMN receipt_inbox_id")
    op.execute("ALTER TABLE expense_attachment ALTER COLUMN expense_id SET NOT NULL")
    op.execute("DROP TABLE receipt_inbox")
