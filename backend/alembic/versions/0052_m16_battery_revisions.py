"""M16 Step 7 audited battery and annual-use revisions.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("battery_asset_revision", "battery_annual_revision")


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
    op.execute("""
      CREATE TABLE battery_asset_revision (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        asset_id UUID NOT NULL,
        version INTEGER NOT NULL CHECK (version > 1),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        actor_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        affected_periods JSONB NOT NULL CHECK (jsonb_typeof(affected_periods) = 'array'),
        before_snapshot JSONB NOT NULL CHECK (jsonb_typeof(before_snapshot) = 'object'),
        after_snapshot JSONB NOT NULL CHECK (jsonb_typeof(after_snapshot) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_battery_asset_revision_version UNIQUE (company_id,asset_id,version),
        FOREIGN KEY (company_id,asset_id)
          REFERENCES battery_asset(company_id,id) ON DELETE RESTRICT
      )
    """)
    op.execute("""
      CREATE TABLE battery_annual_revision (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        annual_use_id UUID NOT NULL,
        version INTEGER NOT NULL CHECK (version > 1),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        actor_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        before_snapshot JSONB NOT NULL CHECK (jsonb_typeof(before_snapshot) = 'object'),
        after_snapshot JSONB NOT NULL CHECK (jsonb_typeof(after_snapshot) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_battery_annual_revision_version UNIQUE (company_id,annual_use_id,version),
        FOREIGN KEY (company_id,annual_use_id)
          REFERENCES battery_annual_use(company_id,id) ON DELETE RESTRICT
      )
    """)
    for table in _TABLES:
        _rls(table)
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {_role()}")

    op.execute("""
      CREATE FUNCTION jai_battery_revision_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'battery revisions are append-only'; END $$
    """)
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION jai_battery_revision_append_only()"
        )

    # A financial Expense correction is permitted only in the same transaction
    # as an append-only battery revision whose after-snapshot matches the values.
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_battery_expense_protect() RETURNS trigger
      LANGUAGE plpgsql AS $$
      DECLARE audit_id UUID;
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
            audit_id := NULLIF(current_setting('jai.battery_asset_revision_id', true), '')::uuid;
            IF ROW(NEW.vat_treatment_id,NEW.vat_treatment_code,NEW.vat_treatment_effect,
                   NEW.deductible,NEW.currency,NEW.exchange_rate,NEW.include_in_pl)
               IS DISTINCT FROM
               ROW(OLD.vat_treatment_id,OLD.vat_treatment_code,OLD.vat_treatment_effect,
                   OLD.deductible,OLD.currency,OLD.exchange_rate,OLD.include_in_pl) THEN
              RAISE EXCEPTION 'registered battery acquisition fields are protected';
            END IF;
            IF audit_id IS NULL OR NOT EXISTS (
              SELECT 1 FROM battery_asset_revision r
              WHERE r.id=audit_id AND r.company_id=OLD.company_id
                AND r.asset_id=(SELECT a.id FROM battery_asset a
                  WHERE a.company_id=OLD.company_id AND a.acquisition_expense_id=OLD.id)
                AND r.after_snapshot->'expense_after'->>'expense_date' = NEW.expense_date::text
                AND (r.after_snapshot->'expense_after'->>'net_amount')::numeric = NEW.net_amount
                AND (r.after_snapshot->'expense_after'->>'vat_amount')::numeric = NEW.vat_amount
                AND (r.after_snapshot->'expense_after'->>'gross_amount')::numeric = NEW.gross_amount
                AND (r.after_snapshot->'expense_after'->>'base_net_amount')::numeric
                  = NEW.base_net_amount
                AND (r.after_snapshot->'expense_after'->>'base_vat_amount')::numeric
                  = NEW.base_vat_amount
                AND (r.after_snapshot->'expense_after'->>'base_gross_amount')::numeric
                  = NEW.base_gross_amount
                AND r.after_snapshot->'expense_after'->>'vat_rate_id'
                  = COALESCE(NEW.vat_rate_id::text, '')
                AND (r.after_snapshot->'expense_after'->>'vat_rate_percent')::numeric
                  = NEW.vat_rate_percent
                AND r.after_snapshot->'expense_after'->>'vat_rate_label' = NEW.vat_rate_label
                AND r.after_snapshot->'expense_after'->>'currency' = NEW.currency
                AND (r.after_snapshot->'expense_after'->>'exchange_rate')::numeric
                  = NEW.exchange_rate
            ) THEN
              RAISE EXCEPTION 'registered battery acquisition fields are protected';
            END IF;
          END IF;
        END IF;
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
      END $$
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE battery_asset_revision, battery_annual_revision IN ACCESS EXCLUSIVE MODE"
    )
    for table in reversed(_TABLES):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM battery_asset_revision)
          OR EXISTS (SELECT 1 FROM battery_annual_revision) THEN
          RAISE EXCEPTION 'Refusing to discard retained battery revision history';
        END IF;
      END $$
    """)
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION jai_battery_revision_append_only()")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE {table}")
    # Restore the Step 6 deletion/update guard after empty audit tables are gone.
    op.execute("""
      CREATE OR REPLACE FUNCTION jai_battery_expense_protect() RETURNS trigger
      LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM battery_asset a WHERE a.company_id=OLD.company_id
          AND a.acquisition_expense_id=OLD.id) THEN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'registered battery acquisition cannot be deleted';
          END IF;
          IF ROW(NEW.expense_date,NEW.net_amount,NEW.vat_amount,NEW.gross_amount,
                 NEW.base_net_amount,NEW.base_vat_amount,NEW.base_gross_amount,
                 NEW.vat_treatment_id,NEW.vat_treatment_code,NEW.vat_treatment_effect,
                 NEW.vat_rate_id,NEW.vat_rate_percent,NEW.deductible,NEW.currency,NEW.exchange_rate)
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
