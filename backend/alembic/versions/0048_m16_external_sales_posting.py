"""M16 Step 3 atomic external sale posting and append-only history.

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

# ruff: noqa: E501

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be configured")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute("ALTER TABLE external_sale DROP CONSTRAINT external_sale_status_check")
    op.execute(
        "ALTER TABLE external_sale ADD CONSTRAINT external_sale_status_check CHECK (status IN ('PENDING','POSTED'))"
    )
    op.execute(
        "ALTER TABLE external_sale ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)"
    )
    op.execute("ALTER TABLE external_sale ADD COLUMN posted_at TIMESTAMPTZ")
    op.execute(
        'ALTER TABLE external_sale ADD COLUMN posted_by UUID REFERENCES "user"(id) ON DELETE RESTRICT'
    )
    op.execute("ALTER TABLE external_sale ADD COLUMN posted_source_name TEXT")
    op.execute("ALTER TABLE external_sale ADD COLUMN original_net_total NUMERIC(14,2)")
    op.execute("ALTER TABLE external_sale ADD COLUMN original_vat_total NUMERIC(14,2)")
    op.execute("ALTER TABLE external_sale ADD COLUMN mismatch_ack_reason TEXT")
    op.execute("""
      CREATE TABLE external_sale_posting (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        external_sale_id UUID NOT NULL,
        version INTEGER NOT NULL CHECK(version > 1),
        snapshot JSONB NOT NULL,
        actor_id UUID REFERENCES "user"(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT fk_external_sale_posting_company FOREIGN KEY(company_id,external_sale_id)
          REFERENCES external_sale(company_id,id) ON DELETE RESTRICT,
        CONSTRAINT uq_external_sale_posting_version UNIQUE(external_sale_id,version)
      )
    """)
    op.execute(
        "CREATE INDEX ix_external_sale_posting_sale ON external_sale_posting(company_id,external_sale_id,created_at)"
    )
    op.execute("ALTER TABLE external_sale_posting ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_sale_posting FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY external_sale_posting_company_isolation ON external_sale_posting USING (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )
    role = _role()
    op.execute(f"GRANT SELECT, INSERT ON external_sale_posting TO {role}")
    op.execute(f"REVOKE UPDATE, DELETE ON external_sale_posting FROM {role}")
    op.execute("""
      CREATE FUNCTION jai_external_sale_posting_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'external_sale_posting is append-only'; END IF; RETURN NEW; END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_posting_append_only BEFORE UPDATE OR DELETE ON external_sale_posting FOR EACH ROW EXECUTE FUNCTION jai_external_sale_posting_append_only()"
    )
    op.execute("""
      CREATE FUNCTION jai_external_sale_posted_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.status = 'POSTED' THEN RAISE EXCEPTION 'posted external sale is immutable'; END IF;
        IF TG_OP = 'DELETE' AND OLD.status <> 'PENDING' THEN RAISE EXCEPTION 'posted external sale cannot be deleted'; END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_posted_immutable BEFORE UPDATE OR DELETE ON external_sale FOR EACH ROW EXECUTE FUNCTION jai_external_sale_posted_immutable()"
    )
    op.execute("""
      CREATE FUNCTION jai_external_sale_posted_file_retained() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM external_sale s WHERE s.id=OLD.external_sale_id AND s.company_id=OLD.company_id AND s.status='POSTED')
        THEN RAISE EXCEPTION 'posted external sale originals are retained'; END IF;
        RETURN OLD;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_posted_file_retained BEFORE DELETE ON external_sale_file FOR EACH ROW EXECUTE FUNCTION jai_external_sale_posted_file_retained()"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE external_sale_posting, external_sale_file, external_sale IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("ALTER TABLE external_sale_posting DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_sale DISABLE ROW LEVEL SECURITY")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM external_sale_posting) OR EXISTS (SELECT 1 FROM external_sale WHERE status='POSTED') THEN
          RAISE EXCEPTION 'Refusing to discard retained external sale posting history';
        END IF;
      END $$
    """)
    op.execute("DROP TRIGGER trg_external_sale_posted_file_retained ON external_sale_file")
    op.execute("DROP FUNCTION jai_external_sale_posted_file_retained()")
    op.execute("DROP TRIGGER trg_external_sale_posted_immutable ON external_sale")
    op.execute("DROP FUNCTION jai_external_sale_posted_immutable()")
    op.execute("DROP TRIGGER trg_external_sale_posting_append_only ON external_sale_posting")
    op.execute("DROP FUNCTION jai_external_sale_posting_append_only()")
    op.execute("DROP TABLE external_sale_posting")
    op.execute(
        "ALTER TABLE external_sale DROP COLUMN mismatch_ack_reason, DROP COLUMN original_vat_total, DROP COLUMN original_net_total, DROP COLUMN posted_source_name, DROP COLUMN posted_by, DROP COLUMN posted_at, DROP COLUMN version"
    )
    op.execute("ALTER TABLE external_sale DROP CONSTRAINT external_sale_status_check")
    op.execute(
        "ALTER TABLE external_sale ADD CONSTRAINT external_sale_status_check CHECK (status='PENDING')"
    )
    op.execute("ALTER TABLE external_sale ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_sale FORCE ROW LEVEL SECURITY")
