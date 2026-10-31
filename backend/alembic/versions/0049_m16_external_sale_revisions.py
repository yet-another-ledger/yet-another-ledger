"""M16 Step 4 external sale revisions, corrections and voids.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

# ruff: noqa: E501

revision: str = "0049"
down_revision: str | None = "0048"
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
        "ALTER TABLE external_sale ADD CONSTRAINT external_sale_status_check CHECK (status IN ('PENDING','POSTED','VOID'))"
    )
    op.execute(
        "ALTER TABLE external_sale ADD COLUMN document_kind TEXT NOT NULL DEFAULT 'SALE' CHECK (document_kind IN ('SALE','CREDIT','SETTLEMENT_ADJUSTMENT'))"
    )
    op.execute("ALTER TABLE external_sale ADD COLUMN related_external_sale_id UUID")
    op.execute("ALTER TABLE external_sale ADD COLUMN historical_original_number TEXT")
    op.execute("ALTER TABLE external_sale ADD COLUMN historical_original_explanation TEXT")
    op.execute(
        "ALTER TABLE external_sale ADD CONSTRAINT fk_external_sale_related_company FOREIGN KEY(company_id,related_external_sale_id) REFERENCES external_sale(company_id,id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE external_sale ADD CONSTRAINT external_sale_credit_reference_check CHECK ((document_kind = 'CREDIT' AND ((status = 'PENDING' AND (related_external_sale_id IS NULL OR (historical_original_number IS NULL AND historical_original_explanation IS NULL))) OR (related_external_sale_id IS NOT NULL AND historical_original_number IS NULL AND historical_original_explanation IS NULL) OR (related_external_sale_id IS NULL AND btrim(COALESCE(historical_original_number,'')) <> '' AND btrim(COALESCE(historical_original_explanation,'')) <> ''))) OR (document_kind <> 'CREDIT' AND related_external_sale_id IS NULL AND historical_original_number IS NULL AND historical_original_explanation IS NULL))"
    )
    op.execute(
        "ALTER TABLE external_sale ADD CONSTRAINT external_sale_no_self_credit CHECK (related_external_sale_id IS NULL OR related_external_sale_id <> id)"
    )
    op.execute("""
      CREATE TABLE external_sale_revision (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        external_sale_id UUID NOT NULL,
        version INTEGER NOT NULL CHECK(version > 1),
        action TEXT NOT NULL CHECK(action IN ('REVISION','VOID')),
        reason TEXT NOT NULL CHECK(length(btrim(reason)) > 0),
        before_snapshot JSONB NOT NULL,
        after_snapshot JSONB NOT NULL,
        actor_id UUID REFERENCES "user"(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT fk_external_sale_revision_company FOREIGN KEY(company_id,external_sale_id)
          REFERENCES external_sale(company_id,id) ON DELETE RESTRICT,
        CONSTRAINT uq_external_sale_revision_version UNIQUE(external_sale_id,version)
      )
    """)
    op.execute(
        "CREATE INDEX ix_external_sale_revision_sale ON external_sale_revision(company_id,external_sale_id,created_at)"
    )
    op.execute("ALTER TABLE external_sale_revision ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_sale_revision FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY external_sale_revision_company_isolation ON external_sale_revision USING (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )
    role = _role()
    op.execute(f"GRANT SELECT, INSERT ON external_sale_revision TO {role}")
    op.execute(f"REVOKE UPDATE, DELETE ON external_sale_revision FROM {role}")
    op.execute("""
      CREATE FUNCTION jai_external_sale_revision_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'external_sale_revision is append-only'; END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_revision_append_only BEFORE UPDATE OR DELETE ON external_sale_revision FOR EACH ROW EXECUTE FUNCTION jai_external_sale_revision_append_only()"
    )
    op.execute("DROP TRIGGER trg_external_sale_posted_immutable ON external_sale")
    op.execute("DROP FUNCTION jai_external_sale_posted_immutable()")
    op.execute("""
      CREATE FUNCTION jai_external_sale_posted_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE audit_id UUID;
      BEGIN
        IF TG_OP = 'DELETE' THEN
          IF OLD.status <> 'PENDING' THEN RAISE EXCEPTION 'posted external sale cannot be deleted'; END IF;
          RETURN OLD;
        END IF;
        IF OLD.status IN ('POSTED','VOID') THEN
          audit_id := NULLIF(current_setting('jai.external_sale_revision_id', true), '')::uuid;
          IF audit_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM external_sale_revision r
            WHERE r.id=audit_id AND r.company_id=OLD.company_id
              AND r.external_sale_id=OLD.id AND r.version=NEW.version
              AND ((r.action='REVISION' AND OLD.status='POSTED' AND NEW.status='POSTED')
                OR (r.action='VOID' AND OLD.status='POSTED' AND NEW.status='VOID'))
          ) THEN RAISE EXCEPTION 'posted external sale requires an audited command'; END IF;
          IF OLD.source_id IS DISTINCT FROM NEW.source_id
             OR OLD.original_number IS DISTINCT FROM NEW.original_number
             OR OLD.document_kind IS DISTINCT FROM NEW.document_kind
             OR OLD.related_external_sale_id IS DISTINCT FROM NEW.related_external_sale_id
             OR OLD.historical_original_number IS DISTINCT FROM NEW.historical_original_number
             OR OLD.historical_original_explanation IS DISTINCT FROM NEW.historical_original_explanation
          THEN RAISE EXCEPTION 'posted external sale identity is immutable'; END IF;
        END IF;
        IF OLD.status = 'VOID' THEN RAISE EXCEPTION 'voided external sale is immutable'; END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_posted_immutable BEFORE UPDATE OR DELETE ON external_sale FOR EACH ROW EXECUTE FUNCTION jai_external_sale_posted_immutable()"
    )
    op.execute("DROP TRIGGER trg_external_sale_posted_file_retained ON external_sale_file")
    op.execute("DROP FUNCTION jai_external_sale_posted_file_retained()")
    op.execute("""
      CREATE FUNCTION jai_external_sale_posted_file_retained() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM external_sale s WHERE s.id=OLD.external_sale_id AND s.company_id=OLD.company_id AND s.status IN ('POSTED','VOID'))
        THEN RAISE EXCEPTION 'posted external sale originals are immutable'; END IF;
        RETURN OLD;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_posted_file_retained BEFORE UPDATE OR DELETE ON external_sale_file FOR EACH ROW EXECUTE FUNCTION jai_external_sale_posted_file_retained()"
    )

    # Retained selections are frozen, including when their dictionary is later
    # edited/deactivated. New selections must still pass tenant/eligibility checks.
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_check_external_sale_tax_owner() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE retained_treatment BOOLEAN := false; retained_rate BOOLEAN := false;
      BEGIN
        IF TG_OP='UPDATE' AND NEW.company_id=OLD.company_id AND NEW.external_sale_id=OLD.external_sale_id THEN
          retained_treatment := NEW.treatment_id IS NOT DISTINCT FROM OLD.treatment_id;
          retained_rate := NEW.rate_id IS NOT DISTINCT FROM OLD.rate_id;
        END IF;
        IF NOT retained_treatment AND NEW.treatment_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM vat_treatment t WHERE t.id=NEW.treatment_id AND t.company_id=NEW.company_id
            AND t.side='SALES' AND t.effect='APPLY_RATE' AND NOT t.requires_icp
        ) THEN RAISE EXCEPTION 'external sales treatment unavailable'; END IF;
        IF NOT retained_rate AND NEW.rate_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM vat_rate r WHERE r.id=NEW.rate_id AND r.company_id=NEW.company_id AND r.active
        ) THEN RAISE EXCEPTION 'external sales rate unavailable'; END IF;
        RETURN NEW;
      END $$
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE external_sale_revision, external_sale_file, external_sale IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("ALTER TABLE external_sale_revision DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_sale DISABLE ROW LEVEL SECURITY")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM external_sale_revision) OR EXISTS (SELECT 1 FROM external_sale WHERE status='VOID' OR (status='POSTED' AND (document_kind <> 'SALE' OR related_external_sale_id IS NOT NULL OR historical_original_number IS NOT NULL OR historical_original_explanation IS NOT NULL))) THEN
          RAISE EXCEPTION 'Refusing to discard retained external sale revision/void history';
        END IF;
      END $$
    """)
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_check_external_sale_tax_owner() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.treatment_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM vat_treatment t WHERE t.id=NEW.treatment_id AND t.company_id=NEW.company_id
            AND t.side='SALES' AND t.effect='APPLY_RATE' AND NOT t.requires_icp
        ) THEN RAISE EXCEPTION 'external sales treatment unavailable'; END IF;
        IF NEW.rate_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM vat_rate r WHERE r.id=NEW.rate_id AND r.company_id=NEW.company_id AND r.active
        ) THEN RAISE EXCEPTION 'external sales rate unavailable'; END IF;
        RETURN NEW;
      END $$
    """)
    op.execute("DROP TRIGGER trg_external_sale_posted_file_retained ON external_sale_file")
    op.execute("DROP FUNCTION jai_external_sale_posted_file_retained()")
    op.execute("DROP TRIGGER trg_external_sale_posted_immutable ON external_sale")
    op.execute("DROP FUNCTION jai_external_sale_posted_immutable()")
    op.execute("DROP TRIGGER trg_external_sale_revision_append_only ON external_sale_revision")
    op.execute("DROP FUNCTION jai_external_sale_revision_append_only()")
    op.execute("DROP TABLE external_sale_revision")
    op.execute(
        "ALTER TABLE external_sale DROP CONSTRAINT external_sale_no_self_credit, DROP CONSTRAINT external_sale_credit_reference_check, DROP CONSTRAINT fk_external_sale_related_company"
    )
    op.execute(
        "ALTER TABLE external_sale DROP COLUMN historical_original_explanation, DROP COLUMN historical_original_number, DROP COLUMN related_external_sale_id, DROP COLUMN document_kind"
    )
    op.execute("ALTER TABLE external_sale DROP CONSTRAINT external_sale_status_check")
    op.execute(
        "ALTER TABLE external_sale ADD CONSTRAINT external_sale_status_check CHECK (status IN ('PENDING','POSTED'))"
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
    op.execute(
        "CREATE FUNCTION jai_external_sale_posted_file_retained() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM external_sale s WHERE s.id=OLD.external_sale_id AND s.company_id=OLD.company_id AND s.status='POSTED') THEN RAISE EXCEPTION 'posted external sale originals are retained'; END IF; RETURN OLD; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_external_sale_posted_file_retained BEFORE DELETE ON external_sale_file FOR EACH ROW EXECUTE FUNCTION jai_external_sale_posted_file_retained()"
    )
    op.execute("ALTER TABLE external_sale ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_sale FORCE ROW LEVEL SECURITY")
