"""SI2 read-only revision history storage and append-only permissions.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

# ruff: noqa: E501

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be a non-empty PostgreSQL role identifier.")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute("ALTER TYPE documentchaineventtype ADD VALUE IF NOT EXISTS 'INVOICE_REVISED'")
    op.execute("CREATE SEQUENCE invoice_revision_order_seq AS BIGINT")
    op.execute(
        "CREATE TABLE invoice_revision ("
        "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), company_id UUID NOT NULL, "
        "invoice_id UUID NOT NULL, actor_user_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "revision_order BIGINT NOT NULL DEFAULT nextval('invoice_revision_order_seq'), "
        "reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0), "
        "undelivered_confirmed BOOLEAN NOT NULL, before_snapshot JSONB NOT NULL, after_snapshot JSONB NOT NULL, "
        "CONSTRAINT uq_invoice_revision_order UNIQUE (revision_order), "
        "CONSTRAINT fk_invoice_revision_company FOREIGN KEY (company_id) REFERENCES company(id) ON DELETE RESTRICT, "
        "CONSTRAINT fk_invoice_revision_invoice FOREIGN KEY (invoice_id) REFERENCES invoice(id) ON DELETE RESTRICT, "
        'CONSTRAINT fk_invoice_revision_actor FOREIGN KEY (actor_user_id) REFERENCES "user"(id) ON DELETE SET NULL, '
        "CONSTRAINT ck_invoice_revision_snapshots_object CHECK ("
        "jsonb_typeof(before_snapshot) = 'object' AND jsonb_typeof(after_snapshot) = 'object'))"
    )
    op.execute("ALTER SEQUENCE invoice_revision_order_seq OWNED BY invoice_revision.revision_order")
    op.execute("CREATE INDEX ix_invoice_revision_company_id ON invoice_revision(company_id)")
    op.execute("CREATE INDEX ix_invoice_revision_invoice_id ON invoice_revision(invoice_id)")
    op.execute(
        "CREATE INDEX ix_invoice_revision_history ON invoice_revision(company_id, invoice_id, revision_order DESC)"
    )
    op.execute("ALTER TABLE invoice_revision ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invoice_revision FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY invoice_revision_company_isolation ON invoice_revision "
        "USING (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) "
        "WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE FUNCTION jai_assert_invoice_revision_ownership() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'invoice_revision is append-only'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM invoice i WHERE i.id = NEW.invoice_id AND i.company_id = NEW.company_id) "
        "THEN RAISE EXCEPTION 'invoice_revision invoice ownership mismatch'; END IF; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_invoice_revision_ownership BEFORE INSERT OR UPDATE ON invoice_revision "
        "FOR EACH ROW EXECUTE FUNCTION jai_assert_invoice_revision_ownership()"
    )
    role = _role()
    op.execute(f"REVOKE UPDATE, DELETE ON invoice_revision FROM {role}")
    op.execute(f"GRANT SELECT, INSERT ON invoice_revision TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE invoice_revision_order_seq TO {role}")


def downgrade() -> None:
    op.execute("DROP TABLE invoice_revision")
    op.execute("DROP FUNCTION jai_assert_invoice_revision_ownership()")
    # PostgreSQL enum labels are intentionally retained, matching M12 practice.
