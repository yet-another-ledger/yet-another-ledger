"""M16 Step 5 durable external sales AI prefill state.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE external_sale ADD COLUMN ai_suggestion JSONB")
    op.execute(
        "ALTER TABLE external_sale ADD COLUMN ai_state TEXT NOT NULL DEFAULT 'NOT_RUN' "
        "CHECK (ai_state IN ('NOT_RUN','RUNNING','READY','UNAVAILABLE','FAILED'))"
    )
    op.execute(
        "ALTER TABLE external_sale ADD COLUMN ai_error_code TEXT CHECK "
        "(ai_error_code IS NULL OR ai_error_code IN "
        "('AI_DISABLED','AI_NOT_CONFIGURED','AI_TIMEOUT','AI_PROVIDER_ERROR','AI_INVALID_RESPONSE'))"
    )
    op.execute(
        "ALTER TABLE external_sale ADD COLUMN file_revision INTEGER NOT NULL DEFAULT 0 "
        "CHECK (file_revision >= 0)"
    )
    op.execute(
        "ALTER TABLE external_sale ADD COLUMN extraction_generation INTEGER NOT NULL DEFAULT 0 "
        "CHECK (extraction_generation >= 0)"
    )
    op.execute("ALTER TABLE external_sale ADD COLUMN ai_started_at TIMESTAMPTZ")
    op.execute("ALTER TABLE external_sale ADD COLUMN ai_completed_at TIMESTAMPTZ")
    op.execute("""
      CREATE FUNCTION jai_external_sale_ai_pending_only() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF (NEW.ai_suggestion IS DISTINCT FROM OLD.ai_suggestion
            OR NEW.ai_state IS DISTINCT FROM OLD.ai_state
            OR NEW.ai_error_code IS DISTINCT FROM OLD.ai_error_code
            OR NEW.file_revision IS DISTINCT FROM OLD.file_revision
            OR NEW.extraction_generation IS DISTINCT FROM OLD.extraction_generation
            OR NEW.ai_started_at IS DISTINCT FROM OLD.ai_started_at
            OR NEW.ai_completed_at IS DISTINCT FROM OLD.ai_completed_at)
           AND NEW.status <> 'PENDING'
           AND NOT (OLD.status = 'PENDING' AND NEW.status = 'POSTED') THEN
          RAISE EXCEPTION 'external sale AI extraction requires pending state';
        END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_external_sale_ai_pending_only BEFORE UPDATE ON external_sale "
        "FOR EACH ROW EXECUTE FUNCTION jai_external_sale_ai_pending_only()"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE external_sale IN ACCESS EXCLUSIVE MODE")
    op.execute("ALTER TABLE external_sale DISABLE ROW LEVEL SECURITY")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM external_sale WHERE ai_suggestion IS NOT NULL
          OR ai_state <> 'NOT_RUN' OR ai_error_code IS NOT NULL
          OR file_revision <> 0 OR extraction_generation <> 0
          OR ai_started_at IS NOT NULL OR ai_completed_at IS NOT NULL) THEN
          RAISE EXCEPTION 'Refusing to discard retained external sale AI extraction state';
        END IF;
      END $$
    """)
    op.execute("ALTER TABLE external_sale ENABLE ROW LEVEL SECURITY")
    op.execute("DROP TRIGGER trg_external_sale_ai_pending_only ON external_sale")
    op.execute("DROP FUNCTION jai_external_sale_ai_pending_only()")
    op.execute("""
      ALTER TABLE external_sale DROP COLUMN ai_completed_at, DROP COLUMN ai_started_at,
        DROP COLUMN extraction_generation, DROP COLUMN file_revision,
        DROP COLUMN ai_error_code, DROP COLUMN ai_state, DROP COLUMN ai_suggestion
    """)
