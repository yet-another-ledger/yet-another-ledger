"""M20 Step 2 company fixed assets and protected purchase association.

Revision ID: 0059
Revises: 0058
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be configured")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute("""
      CREATE TABLE fixed_asset (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        acquisition_expense_id UUID NOT NULL,
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        acquisition_date DATE NOT NULL,
        in_service_date DATE,
        acquisition_cost NUMERIC(18,3) NOT NULL CHECK (acquisition_cost >= 0),
        residual_value NUMERIC(18,3) NOT NULL CHECK (
          residual_value >= 0 AND residual_value <= acquisition_cost
        ),
        useful_life_years INTEGER NOT NULL CHECK (useful_life_years BETWEEN 1 AND 100),
        business_percentage NUMERIC(6,3) NOT NULL DEFAULT 100 CHECK (business_percentage = 100),
        created_by UUID REFERENCES "user"(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_fixed_asset_company_id UNIQUE(company_id,id),
        CONSTRAINT uq_fixed_asset_expense UNIQUE(company_id,acquisition_expense_id),
        CONSTRAINT fk_fixed_asset_expense FOREIGN KEY(company_id,acquisition_expense_id)
          REFERENCES expense(company_id,id) ON DELETE RESTRICT
      )
    """)
    op.execute("ALTER TABLE fixed_asset ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE fixed_asset FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY fixed_asset_company_isolation ON fixed_asset USING "
        "(company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid) "
        "WITH CHECK (company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT ON fixed_asset TO {_role()}")
    op.execute("""
      CREATE FUNCTION jai_fixed_asset_expense_protect() RETURNS trigger LANGUAGE plpgsql AS $$
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
                 NEW.business_percentage,NEW.depreciation_years,NEW.include_in_pl,
                 NEW.pl_cost_basis)
             IS DISTINCT FROM
             ROW(OLD.expense_date,OLD.net_amount,OLD.vat_amount,OLD.gross_amount,
                 OLD.base_net_amount,OLD.base_vat_amount,OLD.base_gross_amount,
                 OLD.vat_treatment_id,OLD.vat_treatment_code,OLD.vat_treatment_label,
                 OLD.vat_treatment_effect,OLD.vat_rate_id,OLD.vat_rate_percent,
                 OLD.vat_rate_label,OLD.deductible,OLD.currency,OLD.exchange_rate,
                 OLD.business_percentage,OLD.depreciation_years,OLD.include_in_pl,
                 OLD.pl_cost_basis) THEN
            RAISE EXCEPTION 'fixed asset acquisition financial fields are protected';
          END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_fixed_asset_expense_protect BEFORE UPDATE OR DELETE ON expense "
        "FOR EACH ROW EXECUTE FUNCTION jai_fixed_asset_expense_protect()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_fixed_asset_expense_protect ON expense")
    op.execute("DROP FUNCTION IF EXISTS jai_fixed_asset_expense_protect()")
    op.drop_table("fixed_asset")
