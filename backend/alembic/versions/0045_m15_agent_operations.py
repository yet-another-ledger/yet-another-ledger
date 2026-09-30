"""Add atomic Agent operation idempotency and trace records.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role_identifier() -> str:
    role = get_settings().postgres_app_user
    if not role:
        raise ValueError("POSTGRES_APP_USER must be a non-empty PostgreSQL role identifier.")
    return dialect().identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.execute(
        """CREATE TABLE agent_operation (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
            token_id UUID NOT NULL REFERENCES agent_token(id) ON DELETE RESTRICT,
            request_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            arguments_digest BYTEA NOT NULL,
            source_platform TEXT,
            source_chat_id TEXT,
            source_message_id TEXT,
            target_type TEXT,
            target_id UUID,
            result_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            outcome TEXT NOT NULL,
            error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT pk_agent_operation PRIMARY KEY (id),
            CONSTRAINT uq_agent_operation_company_token_request
                UNIQUE (company_id, token_id, request_id),
            CONSTRAINT ck_agent_operation_request_id
                CHECK (length(btrim(request_id)) BETWEEN 1 AND 200),
            CONSTRAINT ck_agent_operation_name
                CHECK (length(btrim(operation)) BETWEEN 1 AND 100),
            CONSTRAINT ck_agent_operation_digest_length
                CHECK (octet_length(arguments_digest) = 32),
            CONSTRAINT ck_agent_operation_outcome
                CHECK (outcome IN ('succeeded', 'failed')),
            CONSTRAINT ck_agent_operation_error
                CHECK ((outcome = 'failed' AND error_code IS NOT NULL)
                    OR (outcome = 'succeeded' AND error_code IS NULL))
        )"""
    )
    op.execute(
        "CREATE INDEX ix_agent_operation_company_created "
        "ON agent_operation(company_id, created_at)"
    )
    tenant = "company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid"
    op.execute("ALTER TABLE agent_operation ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_operation FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agent_operation_company_isolation ON agent_operation "
        f"USING ({tenant}) WITH CHECK ({tenant})"
    )
    # A row is inserted provisionally before the business callback runs. A
    # deferred constraint trigger checks its final state at transaction end,
    # allowing that reservation while rejecting any committed incomplete row.
    op.execute(
        """CREATE FUNCTION enforce_agent_operation_completed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            saved_completed_at TIMESTAMPTZ;
            saved_outcome TEXT;
            saved_target_type TEXT;
            saved_target_id UUID;
        BEGIN
            -- Deferred row triggers retain the event's NEW image. Read the
            -- current row so an insert reservation followed by completion in
            -- the same transaction is accepted.
            SELECT completed_at, outcome, target_type, target_id
            INTO saved_completed_at, saved_outcome, saved_target_type, saved_target_id
            FROM agent_operation WHERE id = NEW.id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'agent_operation reservation was deleted before commit'
                    USING ERRCODE = '23514';
            END IF;
            IF saved_completed_at IS NULL THEN
                RAISE EXCEPTION 'agent_operation must be completed before commit'
                    USING ERRCODE = '23514';
            END IF;
            IF saved_outcome = 'succeeded'
                AND (saved_target_type IS NULL OR saved_target_id IS NULL) THEN
                RAISE EXCEPTION 'successful agent_operation requires a target'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_agent_operation_completed
        AFTER INSERT OR UPDATE ON agent_operation
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_agent_operation_completed()"""
    )
    # Runtime UPDATE is needed only for the in-transaction reservation ->
    # completion transition. Once completed, every column is immutable. The
    # provisional transition may change result/outcome/target fields, but it
    # cannot rewrite the key or any source/audit identity fields.
    op.execute(
        """CREATE FUNCTION protect_agent_operation_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.completed_at IS NOT NULL THEN
                RAISE EXCEPTION 'completed agent_operation rows are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                NEW.id, NEW.company_id, NEW.token_id, NEW.request_id,
                NEW.operation, NEW.arguments_digest, NEW.source_platform,
                NEW.source_chat_id, NEW.source_message_id, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.id, OLD.company_id, OLD.token_id, OLD.request_id,
                OLD.operation, OLD.arguments_digest, OLD.source_platform,
                OLD.source_chat_id, OLD.source_message_id, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'agent_operation reservation identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.completed_at IS NULL THEN
                RAISE EXCEPTION 'agent_operation may only be updated to completion'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER protect_agent_operation_update
        BEFORE UPDATE ON agent_operation
        FOR EACH ROW EXECUTE FUNCTION protect_agent_operation_update()"""
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON agent_operation TO {_role_identifier()}"
    )
    op.execute(f"REVOKE DELETE ON agent_operation FROM {_role_identifier()}")


def downgrade() -> None:
    op.execute("DROP TRIGGER protect_agent_operation_update ON agent_operation")
    op.execute("DROP TABLE agent_operation")
    op.execute("DROP FUNCTION protect_agent_operation_update()")
    op.execute("DROP FUNCTION enforce_agent_operation_completed()")
