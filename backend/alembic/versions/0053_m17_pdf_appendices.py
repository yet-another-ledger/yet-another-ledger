"""M17 Step 1 PDF appendix originals and membership."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import dialect

from alembic import op
from jai.config import get_settings

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE quote ADD CONSTRAINT uq_quote_company_id UNIQUE (company_id, id)")
    app_role = dialect().identifier_preparer.quote_identifier(get_settings().postgres_app_user)
    op.execute("""
      CREATE TABLE pdf_appendix_file (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        owner_quote_id UUID,
        title TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        pdf_bytes BYTEA NOT NULL,
        sha256 VARCHAR(64) NOT NULL CHECK (length(sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
        page_count INTEGER NOT NULL CHECK (page_count > 0),
        is_archived BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT fk_pdf_appendix_owner_company FOREIGN KEY (company_id, owner_quote_id)
          REFERENCES quote(company_id, id) ON DELETE CASCADE,
        CONSTRAINT ck_pdf_appendix_own_not_archived CHECK (
          owner_quote_id IS NULL OR is_archived = FALSE
        )
      )
    """)
    op.execute("""
      CREATE TABLE quote_appendix (
        id UUID PRIMARY KEY,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        quote_id UUID NOT NULL,
        file_id UUID NOT NULL REFERENCES pdf_appendix_file(id) ON DELETE NO ACTION
          DEFERRABLE INITIALLY DEFERRED,
        sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
        title_snapshot TEXT NOT NULL,
        CONSTRAINT fk_quote_appendix_quote_company FOREIGN KEY (company_id, quote_id)
          REFERENCES quote(company_id, id) ON DELETE CASCADE,
        CONSTRAINT uq_quote_appendix_file UNIQUE (quote_id, file_id),
        CONSTRAINT uq_quote_appendix_order UNIQUE (quote_id, sort_order)
      )
    """)
    op.execute(
        "CREATE INDEX ix_pdf_appendix_file_company_library "
        "ON pdf_appendix_file(company_id, owner_quote_id, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_quote_appendix_company_quote ON quote_appendix(company_id, quote_id)"
    )
    op.execute(
        """ALTER TABLE pdf_appendix_file
        ADD CONSTRAINT uq_pdf_appendix_file_company_id UNIQUE (company_id, id)"""
    )
    op.execute(
        """ALTER TABLE pdf_appendix_file
        ADD CONSTRAINT uq_pdf_appendix_file_owner UNIQUE (company_id, id, owner_quote_id)"""
    )
    op.execute("""
      CREATE FUNCTION jai_check_pdf_appendix_membership() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE f_company UUID; f_owner UUID;
      BEGIN
        SELECT company_id, owner_quote_id INTO f_company, f_owner
          FROM pdf_appendix_file WHERE id = NEW.file_id;
        IF f_company IS DISTINCT FROM NEW.company_id OR
           (f_owner IS NOT NULL AND f_owner IS DISTINCT FROM NEW.quote_id) THEN
          RAISE EXCEPTION 'Invalid PDF appendix ownership';
        END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        """CREATE TRIGGER trg_quote_appendix_ownership
        BEFORE INSERT OR UPDATE ON quote_appendix
        FOR EACH ROW EXECUTE FUNCTION jai_check_pdf_appendix_membership()"""
    )
    tenant = "company_id = NULLIF(current_setting('jai.company_id', true), '')::uuid"
    for table in ("pdf_appendix_file", "quote_appendix"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_company_isolation ON {table} "
            f"USING ({tenant}) WITH CHECK ({tenant})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {app_role}")
    op.execute("""
      CREATE FUNCTION jai_pdf_appendix_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.company_id IS DISTINCT FROM OLD.company_id
          OR NEW.owner_quote_id IS DISTINCT FROM OLD.owner_quote_id
          OR NEW.original_filename IS DISTINCT FROM OLD.original_filename
          OR NEW.pdf_bytes IS DISTINCT FROM OLD.pdf_bytes
          OR NEW.sha256 IS DISTINCT FROM OLD.sha256
          OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
          OR NEW.page_count IS DISTINCT FROM OLD.page_count THEN
          RAISE EXCEPTION 'PDF appendix original fields are immutable';
        END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_pdf_appendix_immutable BEFORE UPDATE ON pdf_appendix_file "
        "FOR EACH ROW EXECUTE FUNCTION jai_pdf_appendix_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_quote_appendix_ownership ON quote_appendix")
    op.execute("DROP FUNCTION IF EXISTS jai_check_pdf_appendix_membership()")
    op.execute("DROP TRIGGER IF EXISTS trg_pdf_appendix_immutable ON pdf_appendix_file")
    op.execute("DROP FUNCTION IF EXISTS jai_pdf_appendix_immutable()")
    for table in ("quote_appendix", "pdf_appendix_file"):
        op.execute(f"DROP POLICY IF EXISTS {table}_company_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table)
    op.execute("ALTER TABLE quote DROP CONSTRAINT IF EXISTS uq_quote_company_id")
