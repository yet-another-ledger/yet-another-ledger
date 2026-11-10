"""Allow the owner-managed Agent mileage creation capability.

Revision ID: 0058
Revises: 0057
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_token DROP CONSTRAINT ck_agent_token_scopes")
    op.execute(
        """ALTER TABLE agent_token ADD CONSTRAINT ck_agent_token_scopes CHECK (
            cardinality(scopes) > 0 AND scopes <@ ARRAY[
                'receipt:intake', 'customer:read', 'customer:create', 'catalog:read',
                'quote:read', 'quote:write_draft', 'invoice:read', 'invoice:write_draft',
                'expense:read', 'mileage:create'
            ]::text[]
        )"""
    )


def downgrade() -> None:
    # CHECK creation scans every row, including revoked tokens, independent of RLS.
    op.execute(
        """ALTER TABLE agent_token ADD CONSTRAINT ck_agent_token_scopes_old CHECK (
            cardinality(scopes) > 0 AND scopes <@ ARRAY[
                'receipt:intake', 'customer:read', 'customer:create', 'catalog:read',
                'quote:read', 'quote:write_draft', 'invoice:read', 'invoice:write_draft',
                'expense:read'
            ]::text[]
        )"""
    )
    op.execute("ALTER TABLE agent_token DROP CONSTRAINT ck_agent_token_scopes")
    op.execute(
        "ALTER TABLE agent_token RENAME CONSTRAINT "
        "ck_agent_token_scopes_old TO ck_agent_token_scopes"
    )
