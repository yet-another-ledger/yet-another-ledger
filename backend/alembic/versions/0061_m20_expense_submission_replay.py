"""Durable company-scoped idempotency keys for new expense submissions.

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    app_user = get_settings().postgres_app_user
    if not app_user:
        raise ValueError("POSTGRES_APP_USER must be configured")
    role = dialect().identifier_preparer.quote_identifier(app_user)
    op.execute("""
      CREATE TABLE expense_submission (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        client_submission_id UUID NOT NULL,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        expense_id UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_expense_submission_key UNIQUE(company_id,client_submission_id),
        CONSTRAINT fk_expense_submission_expense FOREIGN KEY(company_id,expense_id)
          REFERENCES expense(company_id,id) ON DELETE CASCADE
      )
    """)
    op.execute("ALTER TABLE expense_submission ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE expense_submission FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY expense_submission_company_isolation ON expense_submission USING "
        "(company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) "
        "WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT ON expense_submission TO {role}")


def downgrade() -> None:
    op.drop_table("expense_submission")
