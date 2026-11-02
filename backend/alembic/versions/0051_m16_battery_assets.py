"""M16 Step 6 battery assets and annual private-use confirmations.

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "battery_asset",
    "battery_annual_use",
    "battery_annual_evidence",
    "battery_annual_confirmation",
)


def _role() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be configured")
    return dialect().identifier_preparer.quote_identifier(role)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_company_isolation ON {table} "
        "USING (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) "
        "WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE expense ADD CONSTRAINT uq_expense_company_id UNIQUE (company_id,id)")
    op.execute("""
      CREATE TABLE battery_asset (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        acquisition_expense_id UUID NOT NULL,
        acquisition_expense_date DATE NOT NULL,
        in_service_date DATE NOT NULL,
        net_basis NUMERIC(18,3) NOT NULL CHECK (net_basis >= 0),
        purchase_vat NUMERIC(18,3) NOT NULL CHECK (purchase_vat >= 0),
        purchase_vat_rate_percent NUMERIC(6,3) NOT NULL CHECK (purchase_vat_rate_percent >= 0),
        currency TEXT NOT NULL,
        rule_version INTEGER NOT NULL DEFAULT 1 CHECK (rule_version > 0),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        ordinary_vat_full_deduction_confirmed BOOLEAN NOT NULL
          CHECK (ordinary_vat_full_deduction_confirmed),
        owner_confirmed_at TIMESTAMPTZ NOT NULL,
        registered_by UUID REFERENCES "user"(id) ON DELETE SET NULL,
        historical_acknowledged_periods JSONB NOT NULL DEFAULT '[]'::jsonb,
        historical_acknowledged_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_battery_asset_company_id UNIQUE (company_id,id),
        CONSTRAINT uq_battery_asset_acquisition UNIQUE (company_id,acquisition_expense_id),
        CONSTRAINT fk_battery_asset_expense_company FOREIGN KEY (company_id,acquisition_expense_id)
          REFERENCES expense(company_id,id) ON DELETE RESTRICT
      )
    """)
    op.execute("""
      CREATE TABLE battery_annual_use (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL,
        asset_id UUID NOT NULL,
        year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        private_use_percentage NUMERIC(6,3) CHECK (private_use_percentage BETWEEN 0 AND 100),
        basis_note TEXT,
        confirmed BOOLEAN NOT NULL DEFAULT false,
        confirmed_at TIMESTAMPTZ,
        calculation_base NUMERIC(18,3),
        calculation_vat NUMERIC(18,3),
        asset_version_used INTEGER,
        CONSTRAINT uq_battery_annual_use_company_id UNIQUE (company_id,id),
        CONSTRAINT uq_battery_asset_year UNIQUE (company_id,asset_id,year),
        CONSTRAINT fk_battery_annual_asset_company FOREIGN KEY (company_id,asset_id)
          REFERENCES battery_asset(company_id,id) ON DELETE CASCADE,
        CHECK ((confirmed = false AND confirmed_at IS NULL) OR
               (confirmed = true AND confirmed_at IS NOT NULL AND private_use_percentage IS NOT NULL
                AND basis_note IS NOT NULL AND calculation_base IS NOT NULL
                AND calculation_vat IS NOT NULL AND asset_version_used IS NOT NULL))
      )
    """)
    op.execute("""
      CREATE TABLE battery_annual_evidence (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL,
        annual_use_id UUID NOT NULL,
        storage_key TEXT NOT NULL UNIQUE,
        filename TEXT,
        media_type TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK (byte_size > 0),
        sha256 TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY (company_id,annual_use_id)
          REFERENCES battery_annual_use(company_id,id) ON DELETE CASCADE
      )
    """)
    op.execute("""
      CREATE TABLE battery_annual_confirmation (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL,
        annual_use_id UUID NOT NULL,
        version INTEGER NOT NULL,
        actor_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        private_use_percentage NUMERIC(6,3) NOT NULL
          CHECK (private_use_percentage BETWEEN 0 AND 100),
        calculation_base NUMERIC(18,3) NOT NULL,
        calculation_vat NUMERIC(18,3) NOT NULL,
        asset_version_used INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_battery_confirmation_version UNIQUE (company_id,annual_use_id,version),
        FOREIGN KEY (company_id,annual_use_id)
          REFERENCES battery_annual_use(company_id,id) ON DELETE RESTRICT
      )
    """)
    for table in _TABLES:
        _rls(table)
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_role()}")
    op.execute("""
      CREATE FUNCTION jai_battery_asset_register_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
    op.execute("""
      CREATE TRIGGER trg_battery_asset_register_guard BEFORE INSERT ON battery_asset
      FOR EACH ROW EXECUTE FUNCTION jai_battery_asset_register_guard()
    """)
    op.execute("""
      CREATE FUNCTION jai_battery_expense_protect() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM battery_asset a WHERE a.company_id=OLD.company_id
          AND a.acquisition_expense_id=OLD.id) THEN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'registered battery acquisition cannot be deleted';
          END IF;
          IF ROW(NEW.expense_date,NEW.net_amount,NEW.vat_amount,NEW.gross_amount,
                 NEW.base_net_amount,NEW.base_vat_amount,NEW.base_gross_amount,
                 NEW.vat_treatment_id,NEW.vat_treatment_code,NEW.vat_treatment_effect,
                 NEW.vat_rate_id,NEW.vat_rate_percent,NEW.deductible,NEW.currency,
                 NEW.exchange_rate)
             IS DISTINCT FROM
             ROW(OLD.expense_date,OLD.net_amount,OLD.vat_amount,OLD.gross_amount,
                 OLD.base_net_amount,OLD.base_vat_amount,OLD.base_gross_amount,
                 OLD.vat_treatment_id,OLD.vat_treatment_code,OLD.vat_treatment_effect,
                 OLD.vat_rate_id,OLD.vat_rate_percent,OLD.deductible,OLD.currency,
                 OLD.exchange_rate) THEN
            RAISE EXCEPTION 'registered battery acquisition fields are protected';
          END IF;
        END IF;
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
      END $$
    """)
    op.execute("""
      CREATE TRIGGER trg_battery_expense_protect BEFORE UPDATE OR DELETE ON expense
      FOR EACH ROW EXECUTE FUNCTION jai_battery_expense_protect()
    """)
    op.execute("""
      CREATE FUNCTION jai_battery_confirmation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'battery confirmations are append-only'; END $$
    """)
    op.execute("""
      CREATE TRIGGER trg_battery_confirmation_append_only
      BEFORE UPDATE OR DELETE ON battery_annual_confirmation
      FOR EACH ROW EXECUTE FUNCTION jai_battery_confirmation_append_only()
    """)


def downgrade() -> None:
    op.execute("""
      LOCK TABLE battery_asset, battery_annual_use, battery_annual_evidence,
        battery_annual_confirmation IN ACCESS EXCLUSIVE MODE
    """)
    for table in reversed(_TABLES):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM battery_asset) OR EXISTS (SELECT 1 FROM battery_annual_use)
          OR EXISTS (SELECT 1 FROM battery_annual_evidence)
          OR EXISTS (SELECT 1 FROM battery_annual_confirmation) THEN
          RAISE EXCEPTION 'Refusing to discard retained battery asset/annual confirmation data';
        END IF;
      END $$
    """)
    op.execute("DROP TRIGGER trg_battery_confirmation_append_only ON battery_annual_confirmation")
    op.execute("DROP FUNCTION jai_battery_confirmation_append_only()")
    op.execute("DROP TRIGGER trg_battery_asset_register_guard ON battery_asset")
    op.execute("DROP FUNCTION jai_battery_asset_register_guard()")
    op.execute("DROP TRIGGER trg_battery_expense_protect ON expense")
    op.execute("DROP FUNCTION jai_battery_expense_protect()")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE expense DROP CONSTRAINT uq_expense_company_id")
