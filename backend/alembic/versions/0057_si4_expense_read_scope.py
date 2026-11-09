"""Allow the read-only expense Agent capability.

Revision ID: 0057
Revises: 0056
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_token DROP CONSTRAINT ck_agent_token_scopes")
    op.execute(
        """ALTER TABLE agent_token ADD CONSTRAINT ck_agent_token_scopes CHECK (
            cardinality(scopes) > 0 AND scopes <@ ARRAY[
                'receipt:intake', 'customer:read', 'customer:create', 'catalog:read',
                'quote:read', 'quote:write_draft', 'invoice:read', 'invoice:write_draft',
                'expense:read'
            ]::text[]
        )"""
    )


def downgrade() -> None:
    # Installing the narrower CHECK scans every row independently of RLS.
    # A SELECT from the migration role could be tenant-filtered because this
    # table uses FORCE ROW LEVEL SECURITY; constraint validation is global.
    op.execute(
        """ALTER TABLE agent_token ADD CONSTRAINT ck_agent_token_scopes_old CHECK (
            cardinality(scopes) > 0 AND scopes <@ ARRAY[
                'receipt:intake', 'customer:read', 'customer:create', 'catalog:read',
                'quote:read', 'quote:write_draft', 'invoice:read', 'invoice:write_draft'
            ]::text[]
        )"""
    )
    op.execute("ALTER TABLE agent_token DROP CONSTRAINT ck_agent_token_scopes")
    op.execute(
        "ALTER TABLE agent_token RENAME CONSTRAINT "
        "ck_agent_token_scopes_old TO ck_agent_token_scopes"
    )
