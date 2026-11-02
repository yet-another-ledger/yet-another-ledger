"""M16 Step 2 external sources, pending sales drafts and originals.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

# ruff: noqa: E501

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be configured")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE external_sales_source (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
          name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
          kind TEXT NOT NULL CHECK (kind IN ('PLATFORM','ENERGY','OTHER')),
          default_include_in_pl BOOLEAN,
          active BOOLEAN NOT NULL DEFAULT true,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_external_sales_source_company_id UNIQUE (company_id,id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_external_sales_source_company ON external_sales_source(company_id,name)"
    )
    op.execute("""
        CREATE TABLE external_sale (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
          source_id UUID,
          original_number TEXT,
          document_date DATE, covered_from DATE, covered_to DATE,
          counterparty TEXT, description TEXT, include_in_pl BOOLEAN,
          status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status = 'PENDING'),
          creator_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_external_sale_source_company FOREIGN KEY (company_id,source_id)
            REFERENCES external_sales_source(company_id,id) ON DELETE RESTRICT,
          CONSTRAINT uq_external_sale_company_id UNIQUE (company_id,id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_external_sale_company_created ON external_sale(company_id,created_at,id)"
    )
    op.execute("""
        CREATE UNIQUE INDEX uq_external_sale_source_trimmed_number
        ON external_sale(company_id,source_id,btrim(original_number))
        WHERE source_id IS NOT NULL AND original_number IS NOT NULL AND btrim(original_number) <> ''
    """)
    op.execute("""
        CREATE TABLE external_sale_tax (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
          external_sale_id UUID NOT NULL,
          treatment_id UUID REFERENCES vat_treatment(id) ON DELETE RESTRICT,
          treatment_code TEXT, treatment_label TEXT,
          rate_id UUID REFERENCES vat_rate(id) ON DELETE RESTRICT,
          rate_label TEXT, rate_percent NUMERIC(6,3), net_amount NUMERIC(14,3), vat_amount NUMERIC(14,3),
          CONSTRAINT fk_external_sale_tax_sale_company FOREIGN KEY(company_id,external_sale_id)
            REFERENCES external_sale(company_id,id) ON DELETE CASCADE,
          CONSTRAINT ck_external_sale_tax_rate CHECK(rate_percent IS NULL OR rate_percent BETWEEN 0 AND 100)
        )
    """)
    op.execute(
        "CREATE INDEX ix_external_sale_tax_external_sale_id ON external_sale_tax(external_sale_id)"
    )
    op.execute("""
        CREATE FUNCTION jai_check_external_sale_tax_owner() RETURNS trigger LANGUAGE plpgsql AS $$
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
    op.execute(
        "CREATE TRIGGER trg_external_sale_tax_owner BEFORE INSERT OR UPDATE ON external_sale_tax FOR EACH ROW EXECUTE FUNCTION jai_check_external_sale_tax_owner()"
    )
    op.execute("""
        CREATE TABLE external_sale_file (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
          external_sale_id UUID NOT NULL,
          storage_key TEXT NOT NULL, filename TEXT, mime_type TEXT NOT NULL,
          byte_size INTEGER NOT NULL CHECK(byte_size > 0), sha256 TEXT NOT NULL,
          duplicate_warning BOOLEAN NOT NULL DEFAULT false,
          creator_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_external_sale_file_sale_company FOREIGN KEY(company_id,external_sale_id)
            REFERENCES external_sale(company_id,id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX ix_external_sale_file_external_sale_id ON external_sale_file(external_sale_id)"
    )
    op.execute("""
        CREATE FUNCTION jai_check_external_sale_source_owner() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='UPDATE' AND NEW.source_id IS NOT DISTINCT FROM OLD.source_id THEN RETURN NEW; END IF;
          IF NEW.source_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM external_sales_source s WHERE s.id=NEW.source_id AND s.company_id=NEW.company_id AND s.active
          ) THEN RAISE EXCEPTION 'external sales source unavailable'; END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_source_owner BEFORE INSERT OR UPDATE OF source_id ON external_sale FOR EACH ROW EXECUTE FUNCTION jai_check_external_sale_source_owner()"
    )
    for table in (
        "external_sales_source",
        "external_sale",
        "external_sale_tax",
        "external_sale_file",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_company_isolation ON {table} USING (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_role()}")


def downgrade() -> None:
    # FORCE RLS hides every tenant's rows from the NOBYPASSRLS migration role
    # without a company context. Lock and disable RLS transactionally so the
    # retained-data guard sees all rows; a rejected downgrade rolls this back.
    op.execute(
        "LOCK TABLE external_sale_file, external_sale_tax, external_sale, "
        "external_sales_source IN ACCESS EXCLUSIVE MODE"
    )
    for table in (
        "external_sale_file",
        "external_sale_tax",
        "external_sale",
        "external_sales_source",
    ):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM external_sale_file) OR EXISTS (SELECT 1 FROM external_sale_tax)
             OR EXISTS (SELECT 1 FROM external_sale) OR EXISTS (SELECT 1 FROM external_sales_source) THEN
            RAISE EXCEPTION 'Refusing to discard external sales drafts or original metadata';
          END IF;
        END $$
    """)
    op.execute("DROP TRIGGER trg_external_sale_source_owner ON external_sale")
    op.execute("DROP FUNCTION jai_check_external_sale_source_owner()")
    op.execute("DROP TRIGGER trg_external_sale_tax_owner ON external_sale_tax")
    op.execute("DROP FUNCTION jai_check_external_sale_tax_owner()")
    op.execute("DROP TABLE external_sale_file")
    op.execute("DROP TABLE external_sale_tax")
    op.execute("DROP TABLE external_sale")
    op.execute("DROP TABLE external_sales_source")
