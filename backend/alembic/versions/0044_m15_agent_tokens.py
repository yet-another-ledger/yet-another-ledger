"""Add RLS-protected Agent tokens.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role_identifier() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be a non-empty PostgreSQL role identifier.")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute(
        """CREATE TABLE agent_token (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
            creator_id UUID NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            selector UUID NOT NULL,
            secret_digest BYTEA NOT NULL,
            scopes TEXT[] NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT pk_agent_token PRIMARY KEY (id),
            CONSTRAINT uq_agent_token_selector UNIQUE (selector),
            CONSTRAINT ck_agent_token_name CHECK (length(btrim(name)) BETWEEN 1 AND 100),
            CONSTRAINT ck_agent_token_digest_length CHECK (octet_length(secret_digest) = 32),
            CONSTRAINT ck_agent_token_scopes CHECK (
                cardinality(scopes) > 0 AND scopes <@ ARRAY[
                    'receipt:intake', 'customer:read', 'customer:create', 'catalog:read',
                    'quote:read', 'quote:write_draft', 'invoice:read', 'invoice:write_draft'
                ]::text[]
            )
        )"""
    )
    op.execute("CREATE INDEX ix_agent_token_company_id ON agent_token(company_id)")
    op.execute("CREATE INDEX ix_agent_token_company_created ON agent_token(company_id, created_at)")
    tenant = "company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid"
    op.execute("ALTER TABLE agent_token ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_token FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agent_token_company_isolation ON agent_token "
        f"USING ({tenant}) WITH CHECK ({tenant})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON agent_token TO {_role_identifier()}")


def downgrade() -> None:
    op.execute("DROP TABLE agent_token")
