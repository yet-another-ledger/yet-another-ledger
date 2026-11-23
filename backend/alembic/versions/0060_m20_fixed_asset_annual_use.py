"""M20 Step 3 annual mixed-use estimates and audit history.

Revision ID: 0060
Revises: 0059
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be configured")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute("ALTER TABLE fixed_asset DROP CONSTRAINT fixed_asset_business_percentage_check")
    op.execute(
        "ALTER TABLE fixed_asset ADD CONSTRAINT ck_fixed_asset_business_percentage "
        "CHECK (business_percentage BETWEEN 0 AND 100)"
    )
    op.execute(
        "ALTER TABLE fixed_asset ADD COLUMN use_type TEXT NOT NULL DEFAULT 'FULL_BUSINESS' "
        "CHECK (use_type IN ('FULL_BUSINESS','MIXED_USE'))"
    )
    op.execute(
        "ALTER TABLE fixed_asset ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)"
    )
    # The M16 trigger excludes battery purchases from P&L. When a fixed asset
    # already replaces that source purchase, preserve its original P&L flag.
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_battery_asset_register_guard()
      RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NOT EXISTS (SELECT 1 FROM expense e WHERE e.id=NEW.acquisition_expense_id
          AND e.company_id=NEW.company_id AND e.kind='PURCHASE' AND NOT e.is_draft
          AND e.vat_treatment_code='NL_DOMESTIC_PURCH' AND e.vat_treatment_effect='APPLY_RATE'
          AND e.deductible AND e.base_vat_amount > 0 AND e.vat_rate_percent > 0
          AND NEW.in_service_date >= e.expense_date
          AND NEW.acquisition_expense_date=e.expense_date
          AND NEW.net_basis=e.base_net_amount AND NEW.purchase_vat=e.base_vat_amount
          AND NEW.purchase_vat_rate_percent=e.vat_rate_percent AND NEW.currency=e.currency) THEN
          RAISE EXCEPTION 'ineligible battery acquisition Expense';
        END IF;
        PERFORM set_config('jai.battery_asset_register', NEW.acquisition_expense_id::text, true);
        UPDATE expense SET include_in_pl=false
          WHERE id=NEW.acquisition_expense_id AND company_id=NEW.company_id;
        PERFORM set_config('jai.battery_asset_register', 'false', true);
        RETURN NEW;
      END $$
    """)
    op.execute("""
      CREATE TABLE fixed_asset_annual_use (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        asset_id UUID NOT NULL,
        year INTEGER NOT NULL CHECK (year BETWEEN 1 AND 9999),
        estimated_business_percentage NUMERIC(6,3) NOT NULL
          CHECK (estimated_business_percentage BETWEEN 0 AND 100),
        actual_business_percentage NUMERIC(6,3)
          CHECK (actual_business_percentage BETWEEN 0 AND 100),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        confirmed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_fixed_asset_annual_use_company_id UNIQUE(company_id,id),
        CONSTRAINT uq_fixed_asset_use_year UNIQUE(company_id,asset_id,year),
        CONSTRAINT fk_fixed_asset_use_asset FOREIGN KEY(company_id,asset_id)
          REFERENCES fixed_asset(company_id,id) ON DELETE CASCADE,
        CONSTRAINT ck_fixed_asset_actual_confirmation CHECK
          ((actual_business_percentage IS NULL) = (confirmed_at IS NULL))
      )
    """)
    op.execute("""
      CREATE TABLE fixed_asset_annual_revision (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        annual_use_id UUID NOT NULL,
        version INTEGER NOT NULL CHECK(version > 1),
        reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
        actor_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        before_snapshot JSONB NOT NULL,
        after_snapshot JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_fixed_asset_use_revision_version UNIQUE(company_id,annual_use_id,version),
        CONSTRAINT fk_fixed_asset_use_revision FOREIGN KEY(company_id,annual_use_id)
          REFERENCES fixed_asset_annual_use(company_id,id) ON DELETE CASCADE
      )
    """)
    role = _role()
    for table in ("fixed_asset_annual_use", "fixed_asset_annual_revision"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_company_isolation ON {table} USING "
            "(company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) "
            "WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON fixed_asset_annual_use TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON fixed_asset_annual_revision TO {role}")
    op.execute(f"GRANT UPDATE ON fixed_asset TO {role}")
    op.execute("""
      CREATE TABLE fixed_asset_revision (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        asset_id UUID NOT NULL,
        version INTEGER NOT NULL CHECK(version > 1),
        reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
        actor_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        before_snapshot JSONB NOT NULL,
        after_snapshot JSONB NOT NULL,
        affected_periods JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_fixed_asset_revision_version UNIQUE(company_id,asset_id,version),
        CONSTRAINT fk_fixed_asset_revision_asset FOREIGN KEY(company_id,asset_id)
          REFERENCES fixed_asset(company_id,id) ON DELETE CASCADE
      )
    """)
    op.execute("ALTER TABLE fixed_asset_revision ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE fixed_asset_revision FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY fixed_asset_revision_company_isolation ON fixed_asset_revision USING "
        "(company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) "
        "WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT ON fixed_asset_revision TO {role}")
    # M16 protects a linked purchase too. Permit changes to its protected
    # financial snapshot only when both linked audit records have been staged
    # in this transaction and their snapshots match this exact source change.
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_fixed_asset_expense_protect()
      RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE battery_revision UUID;
              fixed_revision UUID;
              allowed BOOLEAN := FALSE;
      BEGIN
        IF TG_OP = 'DELETE' THEN
          IF EXISTS (SELECT 1 FROM fixed_asset a WHERE a.company_id=OLD.company_id
                     AND a.acquisition_expense_id=OLD.id) THEN
            RAISE EXCEPTION 'fixed asset acquisition cannot be deleted';
          END IF;
          RETURN OLD;
        END IF;
        IF EXISTS (SELECT 1 FROM fixed_asset a WHERE a.company_id=OLD.company_id
                   AND a.acquisition_expense_id=OLD.id)
           AND (ROW(NEW.expense_date,NEW.net_amount,NEW.vat_amount,NEW.gross_amount,
                 NEW.base_net_amount,NEW.base_vat_amount,NEW.base_gross_amount,
                 NEW.vat_treatment_id,NEW.vat_treatment_code,NEW.vat_treatment_label,
                 NEW.vat_treatment_effect,NEW.vat_rate_id,NEW.vat_rate_percent,
                 NEW.vat_rate_label,NEW.deductible,NEW.currency,NEW.exchange_rate,
                 NEW.business_percentage,NEW.depreciation_years,
                 NEW.pl_cost_basis)
             IS DISTINCT FROM
             ROW(OLD.expense_date,OLD.net_amount,OLD.vat_amount,OLD.gross_amount,
                 OLD.base_net_amount,OLD.base_vat_amount,OLD.base_gross_amount,
                 OLD.vat_treatment_id,OLD.vat_treatment_code,OLD.vat_treatment_label,
                 OLD.vat_treatment_effect,OLD.vat_rate_id,OLD.vat_rate_percent,
                 OLD.vat_rate_label,OLD.deductible,OLD.currency,OLD.exchange_rate,
                 OLD.business_percentage,OLD.depreciation_years,OLD.pl_cost_basis)
             OR (NEW.include_in_pl IS DISTINCT FROM OLD.include_in_pl
                 AND NOT (OLD.include_in_pl IS TRUE AND NEW.include_in_pl IS FALSE
                   AND pg_trigger_depth() > 1
                   AND current_setting('jai.battery_asset_register', true) = OLD.id::text))) THEN
          battery_revision := NULLIF(
            current_setting('jai.battery_asset_revision_id', true), '')::uuid;
          fixed_revision := NULLIF(current_setting('jai.fixed_asset_revision_id', true), '')::uuid;
          SELECT EXISTS (
            SELECT 1 FROM battery_asset_revision br
            JOIN battery_asset ba ON ba.company_id=br.company_id AND ba.id=br.asset_id
            JOIN fixed_asset fa ON fa.company_id=br.company_id
              AND fa.acquisition_expense_id=ba.acquisition_expense_id
            JOIN fixed_asset_revision fr ON fr.company_id=fa.company_id AND fr.asset_id=fa.id
            WHERE br.id=battery_revision AND fr.id=fixed_revision
              AND ba.acquisition_expense_id=OLD.id AND fa.acquisition_expense_id=OLD.id
              AND br.version=ba.version AND fr.version=fa.version
              AND br.before_snapshot ? 'expense_before'
              AND br.after_snapshot ? 'expense_after'
              AND fr.before_snapshot ? 'acquisition_cost'
              AND fr.after_snapshot ? 'acquisition_cost'
              AND fr.before_snapshot->>'acquisition_date'=OLD.expense_date::text
              AND fr.after_snapshot->>'acquisition_date'=NEW.expense_date::text
              AND (fr.before_snapshot->>'acquisition_cost')::numeric >= 0
              AND (fr.after_snapshot->>'acquisition_cost')::numeric=fa.acquisition_cost
              AND (br.before_snapshot->'expense_before'->>'net_amount')::numeric=OLD.net_amount
              AND (br.before_snapshot->'expense_before'->>'vat_amount')::numeric=OLD.vat_amount
              AND (br.before_snapshot->'expense_before'->>'gross_amount')::numeric=OLD.gross_amount
              AND (br.before_snapshot->'expense_before'->>'base_net_amount')::numeric
                    = OLD.base_net_amount
              AND (br.before_snapshot->'expense_before'->>'base_vat_amount')::numeric
                    = OLD.base_vat_amount
              AND (br.before_snapshot->'expense_before'->>'base_gross_amount')::numeric
                    = OLD.base_gross_amount
              AND br.after_snapshot->'expense_after'->>'expense_date'=NEW.expense_date::text
              AND (br.after_snapshot->'expense_after'->>'net_amount')::numeric=NEW.net_amount
              AND (br.after_snapshot->'expense_after'->>'vat_amount')::numeric=NEW.vat_amount
              AND (br.after_snapshot->'expense_after'->>'gross_amount')::numeric=NEW.gross_amount
              AND (br.after_snapshot->'expense_after'->>'base_net_amount')::numeric
                    = NEW.base_net_amount
              AND (br.after_snapshot->'expense_after'->>'base_vat_amount')::numeric
                    = NEW.base_vat_amount
              AND (br.after_snapshot->'expense_after'->>'base_gross_amount')::numeric
                    = NEW.base_gross_amount
          ) INTO allowed;
          IF NOT allowed THEN
            RAISE EXCEPTION 'shared acquisition correction requires staged audit snapshots';
          END IF;
          IF ROW(NEW.vat_treatment_id,NEW.vat_treatment_code,NEW.vat_treatment_label,
                 NEW.vat_treatment_effect,NEW.deductible,NEW.currency,NEW.exchange_rate,
                 NEW.business_percentage,NEW.depreciation_years,NEW.include_in_pl,NEW.pl_cost_basis)
             IS DISTINCT FROM
             ROW(OLD.vat_treatment_id,OLD.vat_treatment_code,OLD.vat_treatment_label,
                 OLD.vat_treatment_effect,OLD.deductible,OLD.currency,OLD.exchange_rate,
                 OLD.business_percentage,OLD.depreciation_years,OLD.include_in_pl,OLD.pl_cost_basis)
          THEN
            RAISE EXCEPTION 'fixed asset acquisition non-correction fields are protected';
          END IF;
        END IF;
        RETURN NEW;
      END $$
    """)
    op.execute("""
      CREATE FUNCTION jai_fixed_asset_annual_revision_immutable()
      RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'fixed asset annual revisions are append-only'; END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_fixed_asset_annual_revision_immutable "
        "BEFORE UPDATE OR DELETE ON fixed_asset_annual_revision "
        "FOR EACH ROW EXECUTE FUNCTION jai_fixed_asset_annual_revision_immutable()"
    )
    op.execute(
        "CREATE TRIGGER trg_fixed_asset_revision_immutable "
        "BEFORE UPDATE OR DELETE ON fixed_asset_revision "
        "FOR EACH ROW EXECUTE FUNCTION jai_fixed_asset_annual_revision_immutable()"
    )
    op.execute("""
      CREATE FUNCTION jai_fixed_asset_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE revision_id UUID;
      BEGIN
        revision_id := NULLIF(current_setting('jai.fixed_asset_revision_id', true), '')::uuid;
        IF revision_id IS NULL OR NOT EXISTS (
          SELECT 1 FROM fixed_asset_revision r
          WHERE r.id=revision_id AND r.company_id=OLD.company_id AND r.asset_id=OLD.id
            AND r.version=NEW.version
            AND (r.after_snapshot->>'acquisition_cost')::numeric=NEW.acquisition_cost
            AND r.after_snapshot->>'in_service_date'=NEW.in_service_date::text
            AND r.after_snapshot->>'acquisition_date'=NEW.acquisition_date::text
        ) THEN
          RAISE EXCEPTION 'fixed asset corrections require matching revision provenance';
        END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_fixed_asset_revision_guard BEFORE UPDATE ON fixed_asset "
        "FOR EACH ROW EXECUTE FUNCTION jai_fixed_asset_revision_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_fixed_asset_revision_guard ON fixed_asset")
    op.execute("DROP FUNCTION IF EXISTS jai_fixed_asset_revision_guard()")
    # Restore the original 0059-only guard before the audit tables disappear.
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_fixed_asset_expense_protect()
      RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM fixed_asset a WHERE a.company_id=OLD.company_id
                   AND a.acquisition_expense_id=OLD.id) THEN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'fixed asset acquisition cannot be deleted';
          END IF;
          IF ROW(NEW.expense_date,NEW.net_amount,NEW.vat_amount,NEW.gross_amount,
                 NEW.base_net_amount,NEW.base_vat_amount,NEW.base_gross_amount,
                 NEW.vat_treatment_id,NEW.vat_treatment_code,NEW.vat_treatment_label,
                 NEW.vat_treatment_effect,NEW.vat_rate_id,NEW.vat_rate_percent,
                 NEW.vat_rate_label,NEW.deductible,NEW.currency,NEW.exchange_rate,
                 NEW.business_percentage,NEW.depreciation_years,NEW.include_in_pl,NEW.pl_cost_basis)
             IS DISTINCT FROM
             ROW(OLD.expense_date,OLD.net_amount,OLD.vat_amount,OLD.gross_amount,
                 OLD.base_net_amount,OLD.base_vat_amount,OLD.base_gross_amount,
                 OLD.vat_treatment_id,OLD.vat_treatment_code,OLD.vat_treatment_label,
                 OLD.vat_treatment_effect,OLD.vat_rate_id,OLD.vat_rate_percent,
                 OLD.vat_rate_label,OLD.deductible,OLD.currency,OLD.exchange_rate,
                 OLD.business_percentage,OLD.depreciation_years,OLD.include_in_pl,OLD.pl_cost_basis)
          THEN RAISE EXCEPTION 'fixed asset acquisition financial fields are protected'; END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$
    """)
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_battery_asset_register_guard()
      RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NOT EXISTS (SELECT 1 FROM expense e WHERE e.id=NEW.acquisition_expense_id
          AND e.company_id=NEW.company_id AND e.kind='PURCHASE' AND NOT e.is_draft
          AND e.vat_treatment_code='NL_DOMESTIC_PURCH' AND e.vat_treatment_effect='APPLY_RATE'
          AND e.deductible AND e.base_vat_amount > 0 AND e.vat_rate_percent > 0
          AND NEW.in_service_date >= e.expense_date
          AND NEW.acquisition_expense_date=e.expense_date
          AND NEW.net_basis=e.base_net_amount AND NEW.purchase_vat=e.base_vat_amount
          AND NEW.purchase_vat_rate_percent=e.vat_rate_percent AND NEW.currency=e.currency) THEN
          RAISE EXCEPTION 'ineligible battery acquisition Expense';
        END IF;
        UPDATE expense SET include_in_pl=false
          WHERE id=NEW.acquisition_expense_id AND company_id=NEW.company_id;
        RETURN NEW;
      END $$
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_fixed_asset_revision_immutable ON fixed_asset_revision")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_fixed_asset_annual_revision_immutable "
        "ON fixed_asset_annual_revision"
    )
    op.execute("DROP FUNCTION IF EXISTS jai_fixed_asset_annual_revision_immutable()")
    op.drop_table("fixed_asset_revision")
    op.drop_table("fixed_asset_annual_revision")
    op.drop_table("fixed_asset_annual_use")
    op.execute("ALTER TABLE fixed_asset DROP CONSTRAINT ck_fixed_asset_business_percentage")
    op.execute("ALTER TABLE fixed_asset DROP COLUMN use_type")
    op.execute("ALTER TABLE fixed_asset DROP COLUMN version")
    op.execute(
        "ALTER TABLE fixed_asset ADD CONSTRAINT fixed_asset_business_percentage_check "
        "CHECK (business_percentage = 100)"
    )
