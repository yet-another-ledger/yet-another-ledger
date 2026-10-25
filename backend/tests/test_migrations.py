"""Tests for Alembic migrations – upgrade / downgrade cycle.

Runs ``alembic`` in a subprocess (because ``env.py`` calls ``asyncio.run``,
which would clash with pytest-asyncio's event loop).

Marked ``@pytest.mark.integration`` — requires a running PostgreSQL instance.
Skipped by default; run with ``pytest -m integration``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from jai.config import get_settings
from jai.services.mileage import get_mileage_defaults

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_url() -> str:
    """Derive a dedicated migration-test database URL."""
    settings = get_settings()
    migration_url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.postgres_migration_user,
        password=settings.postgres_migration_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="jai_test_migrations",
    ).render_as_string(hide_password=False)
    return migration_url


def _run_alembic(*extra_args: str, url: str) -> subprocess.CompletedProcess[str]:
    migration_env = os.environ.copy()
    migration_env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *extra_args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        env=migration_env,
    )


def _ensure_database(url: str) -> None:
    """Create the test database if it doesn't exist (via maintenance DB)."""
    import asyncio
    from urllib.parse import urlparse, urlunparse

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/")
    maint_url = urlunparse(urlparse(get_settings().database_admin_url)._replace(path="/postgres"))

    async def _create() -> None:
        engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            await conn.execute(
                text(
                    f'ALTER DATABASE "{db_name}" OWNER TO '
                    f'"{get_settings().postgres_migration_user}"'
                )
            )
        await engine.dispose()

    asyncio.run(_create())


def _drop_database(url: str) -> None:
    """Drop the test database."""
    import asyncio
    from urllib.parse import urlparse, urlunparse

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/")
    maint_url = urlunparse(urlparse(get_settings().database_admin_url)._replace(path="/postgres"))

    async def _drop() -> None:
        engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await engine.dispose()

    asyncio.run(_drop())


def _query(
    url: str,
    statement: str,
    params: dict[str, object] | None = None,
    *,
    company_id: uuid.UUID | None = None,
) -> list[dict[str, object]]:
    """Run SQL in the isolated migration DB, optionally with tenant context."""

    async def _run() -> list[dict[str, object]]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                if company_id is not None:
                    await conn.execute(
                        text("SELECT set_config('jai.company_id', :company_id, true)"),
                        {"company_id": str(company_id)},
                    )
                rows: list[dict[str, object]] = []
                for command in (part.strip() for part in statement.split(";") if part.strip()):
                    result = await conn.execute(text(command), params or {})
                    if result.returns_rows:
                        rows = [dict(row) for row in result.mappings().all()]
                return rows
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _expect_integrity_error(
    url: str, statement: str, params: dict[str, object], *, company_id: uuid.UUID | None = None
) -> str:
    """Execute one invalid raw SQL write and return its database error text."""

    async def _run() -> str:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                transaction = await conn.begin()
                try:
                    if company_id is not None:
                        await conn.execute(
                            text("SELECT set_config('jai.company_id', :company, true)"),
                            {"company": str(company_id)},
                        )
                    await conn.execute(text(statement), params)
                except IntegrityError as exc:
                    await transaction.rollback()
                    return str(exc.orig)
                await transaction.rollback()
                raise AssertionError("Expected PostgreSQL integrity constraint to reject write.")
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _insert_0025_expense_sentinel(url: str) -> dict[str, uuid.UUID]:
    """Create an existing-company Expense with all M11 non-target snapshots."""
    company_id = uuid.uuid4()
    category_id = uuid.uuid4()
    rate_id = uuid.uuid4()
    treatment_id = uuid.uuid4()
    recurring_id = uuid.uuid4()
    expense_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    params: dict[str, object] = {
        "company_id": company_id,
        "category_id": category_id,
        "rate_id": rate_id,
        "treatment_id": treatment_id,
        "recurring_id": recurring_id,
        "expense_id": expense_id,
        "attachment_id": attachment_id,
    }
    _query(
        url,
        """
        INSERT INTO company (id, name, base_currency)
        VALUES (:company_id, 'Pre-M11 Co', 'EUR');
        INSERT INTO expense_category (id, company_id, name, default_deductible, active)
        VALUES (:category_id, :company_id, 'Travel', true, true);
        INSERT INTO vat_rate (id, company_id, label, percent, active)
        VALUES (:rate_id, :company_id, 'VAT 21%', 21.000, true);
        INSERT INTO vat_treatment
            (id, company_id, code, label, side, effect, report_box, requires_icp,
             deductible, active)
        VALUES
            (:treatment_id, :company_id, 'DOMESTIC', 'Domestic VAT', 'PURCHASE',
             'APPLY_RATE', '5b', false, true, true);
        INSERT INTO recurring_expense
            (id, company_id, name, category_id, category_name, vat_treatment_id,
             vat_treatment_code, vat_treatment_label, vat_treatment_effect,
             vat_rate_id, vat_rate_percent, vat_rate_label, net_amount, vat_amount,
             gross_amount, deductible, frequency, start_date, next_run_date,
             paid_by, business_percentage, depreciation_years)
        VALUES
            (:recurring_id, :company_id, 'Sentinel recurring', :category_id, 'Travel',
             :treatment_id, 'DOMESTIC', 'Domestic VAT', 'APPLY_RATE', :rate_id,
             21.000, 'VAT 21%', 10.100, 2.121, 12.221, true, 'MONTHLY',
             DATE '2025-01-01', DATE '2025-02-01', 'PRIVATE', 75.000, 3);
        INSERT INTO expense
            (id, company_id, expense_date, category_id, category_name, supplier_name,
             vat_treatment_id, vat_treatment_code, vat_treatment_label, vat_treatment_effect,
             vat_rate_id, vat_rate_percent, vat_rate_label, net_amount, vat_amount,
             gross_amount, deductible, currency, exchange_rate, base_net_amount,
             base_vat_amount, base_gross_amount, reference, note, recurring_expense_id,
             paid_by, business_percentage, depreciation_years)
        VALUES
            (:expense_id, :company_id, DATE '2025-01-15', :category_id, 'Travel',
             'Sentinel supplier', :treatment_id, 'DOMESTIC', 'Domestic VAT',
             'APPLY_RATE', :rate_id, 21.000, 'VAT 21%', 10.100, 2.121, 12.221,
             true, 'EUR', 1.23456789, 12.469, 2.619, 15.088, 'SENTINEL',
             'snapshot must survive', :recurring_id, 'PRIVATE', 75.000, 3);
        INSERT INTO expense_attachment
            (id, company_id, expense_id, storage_key, filename, mime_type, byte_size, sha256)
        VALUES (:attachment_id, :company_id, :expense_id, 'sentinel/key', 'sentinel.pdf',
                'application/pdf', 123, 'sentinel-sha');
        """,
        params,
    )
    return {key: value for key, value in params.items() if isinstance(value, uuid.UUID)}


def _insert_0028_downgrade_sentinel(
    url: str, *, quote_provenance: bool, payment_tax: bool
) -> dict[str, uuid.UUID]:
    """Insert one independently non-representable 0028 payment shape."""
    assert quote_provenance or payment_tax
    company_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    treatment_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    payment_id = uuid.uuid4()
    params: dict[str, object] = {
        "company_id": company_id,
        "customer_id": customer_id,
        "treatment_id": treatment_id,
        "invoice_id": invoice_id,
        "payment_id": payment_id,
    }
    _query(
        url,
        """
        INSERT INTO company (id, name, base_currency)
        VALUES (:company_id, 'Downgrade safety Co', 'EUR');
        INSERT INTO customer (id, company_id, name)
        VALUES (:customer_id, :company_id, 'Safety customer');
        INSERT INTO vat_treatment
            (id, company_id, code, label, side, effect, requires_icp, active)
        VALUES (:treatment_id, :company_id, 'NL_DOMESTIC', 'NL Domestic',
                'SALES', 'APPLY_RATE', false, true);
        INSERT INTO invoice
            (id, company_id, customer_id, invoice_number, sequence_number,
             invoice_date, status, paid_status, currency, exchange_rate,
             tax_mode, amounts_include_vat, vat_treatment_id,
             vat_treatment_code, vat_treatment_label, vat_treatment_effect,
             vat_treatment_requires_icp, discount_type, discount_value,
             document_discount_amount, subtotal_excl_vat, line_discount_total,
             taxable_amount, vat_total, total_incl_vat, due_amount,
             base_subtotal_excl_vat, base_line_discount_total,
             base_taxable_amount, base_vat_total, base_total_incl_vat,
             base_due_amount)
        VALUES (:invoice_id, :company_id, :customer_id, 'DOWN-INV-1', 1,
                DATE '2026-01-10', 'DRAFT', 'UNPAID', 'EUR', 1,
                'LINE', false, :treatment_id,
                'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE', false,
                'NONE', 0, 0, 100, 0, 100, 21, 121, 121,
                100, 0, 100, 21, 121, 121)
        """,
        params,
    )
    if quote_provenance:
        quote_id = uuid.uuid4()
        params["quote_id"] = quote_id
        _query(
            url,
            """
            INSERT INTO quote
                (id, company_id, customer_id, quote_number, sequence_number,
                 quote_date, status, currency, exchange_rate, tax_mode,
                 amounts_include_vat, vat_treatment_id, vat_treatment_code,
                 vat_treatment_label, vat_treatment_effect, vat_treatment_requires_icp,
                 discount_type, discount_value, document_discount_amount,
                 subtotal_excl_vat, line_discount_total, taxable_amount, vat_total,
                 total_incl_vat, base_subtotal_excl_vat, base_line_discount_total,
                 base_taxable_amount, base_vat_total, base_total_incl_vat)
            VALUES (:quote_id, :company_id, :customer_id, 'DOWN-QUOTE-1', 1,
                    DATE '2026-01-01', 'ACCEPTED', 'EUR', 1, 'LINE', false,
                    :treatment_id, 'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE', false,
                    'NONE', 0, 0, 100, 0, 100, 21, 121, 100, 0, 100, 21, 121);
            INSERT INTO payment
                (id, company_id, quote_id, payment_date, amount, base_amount,
                 currency, exchange_rate)
            VALUES (:payment_id, :company_id, :quote_id, DATE '2026-01-05',
                    60, 60, 'EUR', 1)
            """,
            params,
        )
    else:
        _query(
            url,
            """
            INSERT INTO payment
                (id, company_id, invoice_id, payment_date, amount, base_amount,
                 currency, exchange_rate)
            VALUES (:payment_id, :company_id, :invoice_id, DATE '2026-01-05',
                    60, 60, 'EUR', 1)
            """,
            params,
        )
    if payment_tax:
        tax_id = uuid.uuid4()
        params["tax_id"] = tax_id
        _query(
            url,
            """
            INSERT INTO payment_tax
                (id, payment_id, vat_rate_label, vat_rate_percent,
                 vat_treatment_code, vat_treatment_effect, vat_treatment_requires_icp,
                 taxable_amount, vat_amount, gross_amount,
                 base_taxable_amount, base_vat_amount, base_gross_amount,
                 bucket_key, sort_order)
            VALUES (:tax_id, :payment_id, 'NL standard (21%)', 21,
                    'NL_DOMESTIC', 'APPLY_RATE', false,
                    49.587, 10.413, 60, 49.587, 10.413, 60,
                    'NL_DOMESTIC|APPLY_RATE|0|21', 0)
            """,
            params,
        )
    return {key: value for key, value in params.items() if isinstance(value, uuid.UUID)}


@pytest.mark.integration
class TestMigrations:
    """Verify ``alembic upgrade head`` and ``alembic downgrade base``."""

    @classmethod
    def setup_class(cls) -> None:
        cls.url = _alembic_url()
        _ensure_database(cls.url)

    @classmethod
    def teardown_class(cls) -> None:
        _drop_database(cls.url)

    def test_upgrade_head(self) -> None:
        """``alembic upgrade head`` should succeed."""
        result = _run_alembic("upgrade", "head", url=self.url)
        assert result.returncode == 0, (
            f"upgrade head failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_0044_sent_copy_preserves_historical_logs(self) -> None:
        assert _run_alembic("upgrade", "head", url=self.url).returncode == 0
        assert _run_alembic("downgrade", "0043", url=self.url).returncode == 0
        company_id, log_id, related_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        params = {"company": company_id, "id": log_id, "related": related_id}
        _query(
            self.url,
            "INSERT INTO company (id,name,base_currency) "
            "VALUES (:company,'SI1 historical','EUR'); "
            "INSERT INTO email_log (id,company_id,related_type,related_id,to_email,"
            "subject,body_snapshot,status,sent_at) VALUES (:id,:company,'INVOICE',"
            ":related,'to@example.com','old subject','old body','SENT',now())",
            params,
            company_id=company_id,
        )
        before = _query(
            self.url,
            "SELECT status,sent_at,body_snapshot FROM email_log WHERE id=:id",
            params,
            company_id=company_id,
        )
        result = _run_alembic("upgrade", "head", url=self.url)
        assert result.returncode == 0, result.stderr
        assert (
            _query(
                self.url,
                "SELECT status,sent_at,body_snapshot FROM email_log WHERE id=:id",
                params,
                company_id=company_id,
            )
            == before
        )
        assert _query(
            self.url,
            "SELECT sent_copy_status,sent_copy_error_code,sent_copy_saved_at "
            "FROM email_log WHERE id=:id",
            params,
            company_id=company_id,
        ) == [
            {
                "sent_copy_status": "NOT_ATTEMPTED",
                "sent_copy_error_code": None,
                "sent_copy_saved_at": None,
            }
        ]
        error = _expect_integrity_error(
            self.url,
            "UPDATE email_log SET sent_copy_status='INVALID' WHERE id=:id",
            params,
            company_id=company_id,
        )
        assert "ck_email_log_sent_copy_status" in error
        assert _run_alembic("downgrade", "0043", url=self.url).returncode == 0
        assert (
            _query(
                self.url,
                "SELECT status,sent_at,body_snapshot FROM email_log WHERE id=:id",
                params,
                company_id=company_id,
            )
            == before
        )
        assert _run_alembic("upgrade", "head", url=self.url).returncode == 0

    def test_0042_upload_enum_upgrade_and_non_destructive_downgrade_posture(self) -> None:
        """UPLOAD is additive, and downgrade deliberately keeps the enum label."""
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0041", url=self.url).returncode == 0
        before = _query(
            self.url,
            "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'documentartifactreason'::regtype "
            "ORDER BY enumsortorder",
        )
        assert before == [{"enumlabel": "DOWNLOAD"}, {"enumlabel": "SEND"}]

        assert _run_alembic("upgrade", "0042", url=self.url).returncode == 0
        upgraded = _query(
            self.url,
            "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'documentartifactreason'::regtype "
            "ORDER BY enumsortorder",
        )
        assert upgraded == [
            {"enumlabel": "DOWNLOAD"},
            {"enumlabel": "SEND"},
            {"enumlabel": "UPLOAD"},
        ]

        assert _run_alembic("downgrade", "0041", url=self.url).returncode == 0
        retained = _query(
            self.url,
            "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'documentartifactreason'::regtype "
            "ORDER BY enumsortorder",
        )
        assert retained == upgraded
        assert _run_alembic("upgrade", "head", url=self.url).returncode == 0

    def test_0043_preserves_legacy_attachments_and_guards_parent_ownership(self) -> None:
        """A production-shaped 0042 upgrade keeps Expense history and enforces XOR ownership."""
        # Establish a revision on a fresh test database as well as in the full
        # suite, where earlier migration tests may already have reached head.
        initial_upgrade = _run_alembic("upgrade", "head", url=self.url)
        assert initial_upgrade.returncode == 0, initial_upgrade.stderr
        assert _run_alembic("downgrade", "0025", url=self.url).returncode == 0
        ids = _insert_0025_expense_sentinel(self.url)
        assert _run_alembic("upgrade", "0042", url=self.url).returncode == 0
        before = _query(
            self.url,
            "SELECT net_amount, vat_amount, gross_amount FROM expense WHERE id=:expense_id",
            {"expense_id": ids["expense_id"]},
        )
        old_file = _query(
            self.url,
            "SELECT company_id, expense_id, storage_key, filename, byte_size "
            "FROM expense_attachment WHERE id=:attachment_id",
            {"attachment_id": ids["attachment_id"]},
        )

        upgraded = _run_alembic("upgrade", "head", url=self.url)
        assert upgraded.returncode == 0, upgraded.stderr
        assert (
            _query(
                self.url,
                "SELECT net_amount, vat_amount, gross_amount FROM expense WHERE id=:expense_id",
                {"expense_id": ids["expense_id"]},
            )
            == before
        )
        assert _query(
            self.url,
            "SELECT company_id, expense_id, storage_key, filename, byte_size, receipt_inbox_id "
            "FROM expense_attachment WHERE id=:attachment_id",
            {"attachment_id": ids["attachment_id"]},
        ) == [{**old_file[0], "receipt_inbox_id": None}]

        inbox_id = uuid.uuid4()
        _query(
            self.url,
            "INSERT INTO receipt_inbox (id, company_id) VALUES (:id, :company_id)",
            {"id": inbox_id, "company_id": ids["company_id"]},
            company_id=ids["company_id"],
        )
        bad_zero = _expect_integrity_error(
            self.url,
            "INSERT INTO expense_attachment "
            "(id, company_id, storage_key, mime_type, byte_size) "
            "VALUES (:id, :company_id, 'zero', 'application/pdf', 1)",
            {"id": uuid.uuid4(), "company_id": ids["company_id"]},
        )
        assert "ck_expense_attachment_exactly_one_parent" in str(bad_zero)
        bad_two = _expect_integrity_error(
            self.url,
            "INSERT INTO expense_attachment "
            "(id, company_id, expense_id, receipt_inbox_id, storage_key, mime_type, byte_size) "
            "VALUES (:id, :company_id, :expense_id, :inbox_id, 'two', 'application/pdf', 1)",
            {
                "id": uuid.uuid4(),
                "company_id": ids["company_id"],
                "expense_id": ids["expense_id"],
                "inbox_id": inbox_id,
            },
        )
        assert "ck_expense_attachment_exactly_one_parent" in str(bad_two)

        inbox_file_id = uuid.uuid4()
        _query(
            self.url,
            "INSERT INTO expense_attachment "
            "(id, company_id, receipt_inbox_id, storage_key, mime_type, byte_size) "
            "VALUES (:id, :company_id, :inbox_id, 'inbox/key', 'application/pdf', 1)",
            {
                "id": inbox_file_id,
                "company_id": ids["company_id"],
                "inbox_id": inbox_id,
            },
        )
        assert _query(
            self.url,
            "SELECT id FROM expense_attachment WHERE id=:id",
            {"id": inbox_file_id},
        ) == [{"id": inbox_file_id}]

        downgrade = _run_alembic("downgrade", "0042", url=self.url)
        assert downgrade.returncode != 0
        assert "cannot downgrade receipt inbox while inbox rows exist" in downgrade.stderr

        _query(
            self.url,
            "DELETE FROM receipt_inbox WHERE id=:id",
            {"id": inbox_id},
            company_id=ids["company_id"],
        )
        assert (
            _query(
                self.url,
                "SELECT id FROM expense_attachment WHERE id=:id",
                {"id": inbox_file_id},
            )
            == []
        )

        # A confirmed item has already transferred its file to Expense. Its
        # attachment no longer prevents SET NOT NULL, so the explicit guard
        # must still reject a downgrade and preserve processed history.
        confirmed_id = uuid.uuid4()
        transferred_file_id = uuid.uuid4()
        _query(
            self.url,
            "INSERT INTO receipt_inbox (id, company_id) VALUES (:id, :company_id)",
            {"id": confirmed_id, "company_id": ids["company_id"]},
            company_id=ids["company_id"],
        )
        _query(
            self.url,
            "INSERT INTO expense_attachment "
            "(id, company_id, receipt_inbox_id, storage_key, mime_type, byte_size) "
            "VALUES (:id, :company_id, :inbox_id, 'transferred/key', 'application/pdf', 1); "
            "UPDATE expense_attachment SET expense_id=:expense_id, receipt_inbox_id=NULL "
            "WHERE id=:id",
            {
                "id": transferred_file_id,
                "company_id": ids["company_id"],
                "inbox_id": confirmed_id,
                "expense_id": ids["expense_id"],
            },
        )
        _query(
            self.url,
            "UPDATE receipt_inbox SET status='CONFIRMED', expense_id=:expense_id, "
            "confirmed_at=now() WHERE id=:id",
            {"id": confirmed_id, "expense_id": ids["expense_id"]},
            company_id=ids["company_id"],
        )
        downgrade = _run_alembic("downgrade", "0042", url=self.url)
        assert downgrade.returncode != 0
        assert "cannot downgrade receipt inbox while inbox rows exist" in downgrade.stderr
        assert (
            _query(
                self.url,
                "SELECT id FROM receipt_inbox WHERE id=:id",
                {"id": confirmed_id},
            )
            == []
        )
        assert _query(
            self.url,
            "SELECT id FROM receipt_inbox WHERE id=:id",
            {"id": confirmed_id},
            company_id=ids["company_id"],
        ) == [{"id": confirmed_id}]
        _query(
            self.url,
            "DELETE FROM receipt_inbox WHERE id=:id",
            {"id": confirmed_id},
            company_id=ids["company_id"],
        )
        assert _run_alembic("downgrade", "0042", url=self.url).returncode == 0
        assert _query(
            self.url,
            "SELECT expense_id, storage_key FROM expense_attachment WHERE id=:id",
            {"id": ids["attachment_id"]},
        ) == [{"expense_id": ids["expense_id"], "storage_key": "sentinel/key"}]
        assert _query(
            self.url,
            "SELECT expense_id, storage_key FROM expense_attachment WHERE id=:id",
            {"id": transferred_file_id},
        ) == [{"expense_id": ids["expense_id"], "storage_key": "transferred/key"}]

    def test_0040_downgrade_round_trip_without_refund_tombstones(self) -> None:
        """0040 remains reversible when no deleted Refund owner exists."""
        assert _run_alembic("upgrade", "0040", url=self.url).returncode == 0
        down = _run_alembic("downgrade", "0039", url=self.url)
        assert down.returncode == 0, down.stderr
        up = _run_alembic("upgrade", "0040", url=self.url)
        assert up.returncode == 0, up.stderr

    def test_0025_to_0026_backfills_existing_expenses_and_seeds_live_defaults(self) -> None:
        """The production upgrade preserves all pre-M11 Expense snapshots exactly."""
        assert _run_alembic("downgrade", "0025", url=self.url).returncode == 0
        ids = _insert_0025_expense_sentinel(self.url)

        result = _run_alembic("upgrade", "0026", url=self.url)
        assert result.returncode == 0, result.stderr

        expense_rows = _query(
            self.url,
            """
            SELECT kind, category_name, supplier_name, vat_treatment_code, vat_rate_percent,
                   net_amount, vat_amount, gross_amount, exchange_rate, base_gross_amount,
                   reference, note, recurring_expense_id, paid_by, business_percentage,
                   depreciation_years
            FROM expense WHERE id = :expense_id
            """,
            {"expense_id": ids["expense_id"]},
        )
        assert expense_rows == [
            {
                "kind": "PURCHASE",
                "category_name": "Travel",
                "supplier_name": "Sentinel supplier",
                "vat_treatment_code": "DOMESTIC",
                "vat_rate_percent": Decimal("21.000"),
                "net_amount": Decimal("10.100"),
                "vat_amount": Decimal("2.121"),
                "gross_amount": Decimal("12.221"),
                "exchange_rate": Decimal("1.23456789"),
                "base_gross_amount": Decimal("15.088"),
                "reference": "SENTINEL",
                "note": "snapshot must survive",
                "recurring_expense_id": ids["recurring_id"],
                "paid_by": "PRIVATE",
                "business_percentage": Decimal("75.000"),
                "depreciation_years": 3,
            }
        ]
        attachment_rows = _query(
            self.url,
            "SELECT storage_key, filename, mime_type, byte_size, sha256 "
            "FROM expense_attachment WHERE id = :attachment_id",
            {"attachment_id": ids["attachment_id"]},
        )
        assert attachment_rows == [
            {
                "storage_key": "sentinel/key",
                "filename": "sentinel.pdf",
                "mime_type": "application/pdf",
                "byte_size": 123,
                "sha256": "sentinel-sha",
            }
        ]
        seed_rows = _query(
            self.url,
            """
            SELECT
                (SELECT active FROM expense_category
                 WHERE company_id = :company_id AND name = 'Mileage') AS category_active,
                (SELECT default_deductible FROM expense_category
                 WHERE company_id = :company_id AND name = 'Mileage') AS category_deductible,
                (SELECT side::text || ':' || effect::text || ':' ||
                        COALESCE(report_box, '') || ':' ||
                        deductible::text || ':' || active::text
                 FROM vat_treatment WHERE company_id = :company_id
                   AND code = 'NL_PRIVATE_TRANSPORT_MILEAGE') AS treatment,
                (SELECT value FROM setting WHERE level = 'COMPANY' AND scope_id = :company_id
                   AND key = 'expense.mileage.defaults') AS defaults
            """,
            {"company_id": ids["company_id"]},
        )[0]
        assert _query(
            self.url,
            "SELECT name FROM mileage_transport_type WHERE company_id = :company_id ORDER BY name",
            {"company_id": ids["company_id"]},
        ) == [
            {"name": "Bicycle"},
            {"name": "Car"},
            {"name": "Motorcycle"},
            {"name": "Other"},
        ]
        assert _query(
            self.url,
            """
            SELECT effective_from, rate_per_km
            FROM mileage_rate
            WHERE company_id = :company_id AND transport_type_id IS NULL
            ORDER BY effective_from
            """,
            {"company_id": ids["company_id"]},
        ) == [
            {"effective_from": date(2024, 1, 1), "rate_per_km": Decimal("0.230")},
            {"effective_from": date(2026, 1, 1), "rate_per_km": Decimal("0.250")},
        ]
        assert seed_rows["category_active"] is True
        assert seed_rows["category_deductible"] is False
        assert seed_rows["treatment"] == "PURCHASE:EXEMPT::false:true"
        assert seed_rows["defaults"] == {
            "expense_category_id": str(
                _query(
                    self.url,
                    "SELECT id FROM expense_category WHERE company_id = :company_id "
                    "AND name = 'Mileage'",
                    {"company_id": ids["company_id"]},
                )[0]["id"]
            ),
            "default_transport_type_id": str(
                _query(
                    self.url,
                    "SELECT id FROM mileage_transport_type WHERE company_id = :company_id "
                    "AND name = 'Car'",
                    {"company_id": ids["company_id"]},
                )[0]["id"]
            ),
        }

    def test_0026_downgrade_then_reupgrade_reseeds_live_typed_defaults(self) -> None:
        """Downgrading removes M11 defaults before its referenced UUIDs vanish."""
        assert _run_alembic("downgrade", "0025", url=self.url).returncode == 0
        ids = _insert_0025_expense_sentinel(self.url)
        assert _run_alembic("upgrade", "0026", url=self.url).returncode == 0
        assert _run_alembic("downgrade", "0025", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0026", url=self.url).returncode == 0

        seeded_rows = _query(
            self.url,
            """
            SELECT
                ec.company_id = c.id AS category_is_company_local,
                ec.active AS category_active,
                tt.company_id = c.id AS type_is_company_local,
                tt.active AS type_active,
                tt.name AS type_name
            FROM company c
            JOIN setting s ON s.level = 'COMPANY' AND s.scope_id = c.id
                AND s.key = 'expense.mileage.defaults'
            JOIN expense_category ec ON ec.id = (s.value ->> 'expense_category_id')::uuid
            JOIN mileage_transport_type tt
                ON tt.id = (s.value ->> 'default_transport_type_id')::uuid
            WHERE c.id = :company_id
            """,
            {"company_id": ids["company_id"]},
        )
        assert seeded_rows == [
            {
                "category_is_company_local": True,
                "category_active": True,
                "type_is_company_local": True,
                "type_active": True,
                "type_name": "Car",
            }
        ]

        async def _assert_typed_defaults() -> None:
            engine = create_async_engine(self.url)
            try:
                async with AsyncSession(engine) as session:
                    defaults = await get_mileage_defaults(session, ids["company_id"])
                assert defaults.expense_category_id
                assert defaults.default_transport_type_id
            finally:
                await engine.dispose()

        asyncio.run(_assert_typed_defaults())

    def test_0027_backfills_selected_live_rate_scope_without_a_live_fk(self) -> None:
        """The one-time backfill makes pre-snapshot trips independent of later edits."""
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0026", url=self.url).returncode == 0
        company_id = uuid.uuid4()
        transport_type_id = uuid.uuid4()
        rate_id = uuid.uuid4()
        trip_id = uuid.uuid4()
        _query(
            self.url,
            """
            INSERT INTO company (id, name, base_currency)
            VALUES (:company_id, 'Rate scope migration Co', 'EUR');
            INSERT INTO mileage_transport_type (id, company_id, name, active)
            VALUES (:transport_type_id, :company_id, 'Migration Scope', true);
            INSERT INTO mileage_rate
                (id, company_id, transport_type_id, effective_from, rate_per_km)
            VALUES (:rate_id, :company_id, :transport_type_id, DATE '2026-01-01', 0.300);
            INSERT INTO mileage_trip
                (id, company_id, transport_type_id, transport_type_name, rate_rule_id,
                 rate_effective_from, rate_per_km, trip_date, one_way_distance_km,
                 total_distance_km, round_trip, calculated_amount)
            VALUES (:trip_id, :company_id, :transport_type_id, 'Migration Scope', :rate_id,
                    DATE '2026-01-01', 0.300, DATE '2026-06-15', 10.000, 10.000,
                    false, 3.000)
            """,
            {
                "company_id": company_id,
                "transport_type_id": transport_type_id,
                "rate_id": rate_id,
                "trip_id": trip_id,
            },
        )
        result = _run_alembic("upgrade", "0027", url=self.url)
        assert result.returncode == 0, result.stderr
        assert _query(
            self.url,
            """
            SELECT rate_transport_type_id, rate_transport_type_name
            FROM mileage_trip WHERE id = :trip_id
            """,
            {"trip_id": trip_id},
        ) == [
            {
                "rate_transport_type_id": transport_type_id,
                "rate_transport_type_name": "Migration Scope",
            }
        ]

    def test_0028_preserves_legacy_payments_and_enforces_document_link(self) -> None:
        """M11.5 migration is additive and keeps invoice-only history unchanged."""
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0027", url=self.url).returncode == 0
        company_id = uuid.uuid4()
        customer_id = uuid.uuid4()
        treatment_id = uuid.uuid4()
        rate_id = uuid.uuid4()
        invoice_id = uuid.uuid4()
        line_id = uuid.uuid4()
        line_tax_id = uuid.uuid4()
        document_invoice_id = uuid.uuid4()
        document_line_one_id = uuid.uuid4()
        document_line_two_id = uuid.uuid4()
        document_tax_id = uuid.uuid4()
        tail_document_ids = [uuid.uuid4() for _ in range(3)]
        tail_line_ids = [[uuid.uuid4() for _ in range(3)] for _ in range(3)]
        tail_tax_ids = [uuid.uuid4() for _ in range(3)]
        payment_id = uuid.uuid4()
        _query(
            self.url,
            """
            INSERT INTO company (id, name, base_currency)
            VALUES (:company_id, 'Pre-M11.5 Co', 'EUR');
            INSERT INTO customer (id, company_id, name)
            VALUES (:customer_id, :company_id, 'Legacy customer');
            INSERT INTO vat_treatment
                (id, company_id, code, label, side, effect, requires_icp, active)
            VALUES
                (:treatment_id, :company_id, 'NL_DOMESTIC', 'NL Domestic',
                 'SALES', 'APPLY_RATE', false, true);
            INSERT INTO vat_rate (id, company_id, label, percent, active)
            VALUES (:rate_id, :company_id, 'NL 21%', 21, true);
            INSERT INTO invoice
                (id, company_id, customer_id, invoice_number, sequence_number,
                 invoice_date, status, paid_status, currency, exchange_rate,
                 tax_mode, amounts_include_vat, vat_treatment_id,
                 vat_treatment_code, vat_treatment_label, vat_treatment_effect,
                 vat_treatment_requires_icp, discount_type, discount_value,
                 document_discount_amount, subtotal_excl_vat, line_discount_total,
                 taxable_amount, vat_total, total_incl_vat, due_amount,
                 base_subtotal_excl_vat, base_line_discount_total,
                 base_taxable_amount, base_vat_total, base_total_incl_vat,
                 base_due_amount)
            VALUES
                (:invoice_id, :company_id, :customer_id, 'LEGACY-1', 1,
                 DATE '2026-01-10', 'SENT', 'PARTIALLY_PAID', 'EUR', 1,
                 'LINE', false, :treatment_id,
                 'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE', false,
                 'NONE', 0, 0, 100.001, 0, 100.001, 21.005, 121.006, 61.006,
                 100.001, 0, 100.001, 21.005, 121.006, 61.006);
            INSERT INTO payment
                (id, company_id, invoice_id, payment_date, amount, base_amount,
                 currency, exchange_rate, reference)
            VALUES
                (:payment_id, :company_id, :invoice_id, DATE '2026-01-15',
                 60, 60, 'EUR', 1, 'LEGACY-PAYMENT')
            ;
            INSERT INTO invoice_line
                (id, invoice_id, sort_order, name, quantity, unit_price, vat_rate_id,
                 vat_rate_label, vat_rate_percent, subtotal_excl_vat, subtotal_incl_vat,
                 line_discount_amount, document_discount_share, taxable_amount, vat_total,
                 total_incl_vat)
            VALUES (:line_id, :invoice_id, 0, 'Legacy line', 1, 100.001, :rate_id,
                    'NL 21%', 21, 100.001, 121.006, 0, 0, 100.001, 21.005, 121.006);
            INSERT INTO invoice_line_tax
                (id, invoice_line_id, vat_rate_id, vat_rate_label, vat_rate_percent,
                 effective_vat_percent, taxable_amount, tax_amount)
            VALUES (:line_tax_id, :line_id, :rate_id, 'NL 21%', 21, 21, 100.001, 21.005)
            ;
            INSERT INTO invoice
                (id, company_id, customer_id, invoice_number, sequence_number,
                 invoice_date, status, paid_status, currency, exchange_rate, tax_mode,
                 amounts_include_vat, vat_treatment_id, document_vat_rate_id,
                 vat_treatment_code, vat_treatment_label, vat_treatment_effect,
                 vat_treatment_requires_icp, discount_type, discount_value,
                 document_discount_amount, subtotal_excl_vat, line_discount_total,
                 taxable_amount, vat_total, total_incl_vat, due_amount,
                 base_subtotal_excl_vat, base_line_discount_total, base_taxable_amount,
                 base_vat_total, base_total_incl_vat, base_due_amount)
            VALUES (:document_invoice_id, :company_id, :customer_id, 'LEGACY-DOC', 2,
                    DATE '2026-01-11', 'SENT', 'UNPAID', 'EUR', 1.234, 'DOCUMENT', false,
                    :treatment_id, :rate_id, 'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE',
                    false, 'NONE', 0, 0, 290, 0, 290, 60.905, 350.905, 350.905,
                    357.860, 0, 357.860, 75.167, 433.027, 433.027);
            INSERT INTO invoice_line
                (id, invoice_id, sort_order, name, quantity, unit_price, subtotal_excl_vat,
                 subtotal_incl_vat, line_discount_amount, document_discount_share,
                 taxable_amount, vat_total, total_incl_vat)
            VALUES (:document_line_one_id, :document_invoice_id, 0, 'Document A', 1, 100,
                    100, 100, 0, 0, 100, 0, 100),
                   (:document_line_two_id, :document_invoice_id, 1, 'Document B', 1, 190,
                    190, 190, 0, 0, 190, 0, 190);
            INSERT INTO invoice_tax
                (id, invoice_id, vat_rate_id, vat_rate_label, vat_rate_percent,
                 effective_vat_percent, taxable_amount, tax_amount)
            VALUES (:document_tax_id, :document_invoice_id, :rate_id, 'NL 21%', 21, 21, 290, 60.905)
            """,
            {
                "company_id": company_id,
                "customer_id": customer_id,
                "treatment_id": treatment_id,
                "rate_id": rate_id,
                "invoice_id": invoice_id,
                "line_id": line_id,
                "line_tax_id": line_tax_id,
                "document_invoice_id": document_invoice_id,
                "document_line_one_id": document_line_one_id,
                "document_line_two_id": document_line_two_id,
                "document_tax_id": document_tax_id,
                "payment_id": payment_id,
            },
        )
        # Historical NUMERIC(18,3) snapshots predate M7.5's customer-facing
        # minor-unit rule.  Exercise all tail values plus a mixed/zero line
        # shape before 0029 derives immutable credit basis rows.
        tail_cases = (
            (
                "LEGACY-DOC-001",
                Decimal("60.001"),
                (Decimal("100.001"), Decimal("0"), Decimal("189.998")),
                Decimal("74.041"),
            ),
            (
                "LEGACY-DOC-005",
                Decimal("60.005"),
                (Decimal("100"), Decimal("0"), Decimal("189.999")),
                Decimal("74.046"),
            ),
            (
                "LEGACY-DOC-999",
                Decimal("60.999"),
                (Decimal("100.001"), Decimal("0"), Decimal("189.999")),
                Decimal("75.273"),
            ),
        )
        for index, (number, vat, line_amounts, base_vat) in enumerate(tail_cases):
            invoice_tail_id = tail_document_ids[index]
            tax_tail_id = tail_tax_ids[index]
            lines = tail_line_ids[index]
            net = sum(line_amounts)
            _query(
                self.url,
                """
                INSERT INTO invoice
                    (id, company_id, customer_id, invoice_number, sequence_number,
                     invoice_date, status, paid_status, currency, exchange_rate, tax_mode,
                     amounts_include_vat, vat_treatment_id, document_vat_rate_id,
                     vat_treatment_code, vat_treatment_label, vat_treatment_effect,
                     vat_treatment_requires_icp, discount_type, discount_value,
                     document_discount_amount, subtotal_excl_vat, line_discount_total,
                     taxable_amount, vat_total, total_incl_vat, due_amount,
                     base_subtotal_excl_vat, base_line_discount_total, base_taxable_amount,
                     base_vat_total, base_total_incl_vat, base_due_amount)
                VALUES
                    (:invoice_id, :company_id, :customer_id, :number, :sequence,
                     DATE '2026-01-12', 'SENT', 'UNPAID', 'EUR', 1.234, 'DOCUMENT', false,
                     :treatment_id, :rate_id, 'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE',
                     false, 'NONE', 0, 0, :net, 0, :net, :vat, :gross, :gross,
                     357.860, 0, 357.860, :base_vat, :base_gross, :base_gross);
                INSERT INTO invoice_line
                    (id, invoice_id, sort_order, name, quantity, unit_price, subtotal_excl_vat,
                     subtotal_incl_vat, line_discount_amount, document_discount_share,
                     taxable_amount, vat_total, total_incl_vat)
                VALUES
                    (:line_one, :invoice_id, 0, 'Tail A', 1, :one, :one, :one, 0, 0, :one, 0, :one),
                    (:line_zero, :invoice_id, 1, 'Tail Zero', 1, 0, 0, 0, 0, 0, 0, 0, 0),
                    (:line_two, :invoice_id, 2, 'Tail B', 1, :two, :two, :two, 0, 0, :two, 0, :two);
                INSERT INTO invoice_tax
                    (id, invoice_id, vat_rate_id, vat_rate_label, vat_rate_percent,
                     effective_vat_percent, taxable_amount, tax_amount)
                VALUES (:tax_id, :invoice_id, :rate_id, 'NL 21%', 21, 21, :net, :vat)
                """,
                {
                    "invoice_id": invoice_tail_id,
                    "company_id": company_id,
                    "customer_id": customer_id,
                    "number": number,
                    "sequence": index + 3,
                    "treatment_id": treatment_id,
                    "rate_id": rate_id,
                    "net": net,
                    "vat": vat,
                    "gross": net + vat,
                    "base_vat": base_vat,
                    "base_gross": Decimal("357.860") + base_vat,
                    "line_one": lines[0],
                    "line_zero": lines[1],
                    "line_two": lines[2],
                    "one": line_amounts[0],
                    "two": line_amounts[2],
                    "tax_id": tax_tail_id,
                },
            )

        result = _run_alembic("upgrade", "0028", url=self.url)
        assert result.returncode == 0, result.stderr
        assert _query(
            self.url,
            """
            SELECT invoice_id, quote_id, amount, payment_date, reference
            FROM payment WHERE id = :payment_id
            """,
            {"payment_id": payment_id},
        ) == [
            {
                "invoice_id": invoice_id,
                "quote_id": None,
                "amount": Decimal("60.000"),
                "payment_date": date(2026, 1, 15),
                "reference": "LEGACY-PAYMENT",
            }
        ]
        assert _query(
            self.url,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'payment' AND column_name = 'invoice_id'
            """,
        ) == [{"is_nullable": "YES"}]
        assert _query(
            self.url,
            """
            SELECT delete_rule FROM information_schema.referential_constraints
            WHERE constraint_name = 'fk_payment_invoice'
            """,
        ) == [{"delete_rule": "SET NULL"}]
        assert _query(
            self.url,
            """
            SELECT to_regclass('public.payment_tax')::text AS payment_tax_table
            """,
        ) == [{"payment_tax_table": "payment_tax"}]

        async def _assert_check_constraint() -> None:
            engine = create_async_engine(self.url)
            try:
                async with engine.begin() as conn:
                    with pytest.raises(IntegrityError):
                        async with conn.begin_nested():
                            await conn.execute(
                                text(
                                    "INSERT INTO payment "
                                    "(company_id, payment_date, amount, base_amount, "
                                    "currency, exchange_rate) VALUES "
                                    "(:company_id, DATE '2026-01-16', 1, 1, 'EUR', 1)"
                                ),
                                {"company_id": company_id},
                            )
            finally:
                await engine.dispose()

        asyncio.run(_assert_check_constraint())
        assert _run_alembic("downgrade", "0027", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "head", url=self.url).returncode == 0
        assert _query(
            self.url,
            """
            SELECT set_config('jai.company_id', CAST(:company_id AS text), true);
            SELECT i.invoice_number, sum(b.net_amount) AS net, sum(b.vat_amount) AS vat,
                   sum(b.gross_amount) AS gross, sum(b.base_net_amount) AS base_net,
                   sum(b.base_vat_amount) AS base_vat, sum(b.base_gross_amount) AS base_gross
            FROM invoice i JOIN invoice_credit_basis_line b ON b.invoice_id = i.id
            WHERE i.id = ANY(:invoice_ids) GROUP BY i.invoice_number ORDER BY i.invoice_number
            """,
            {"company_id": str(company_id), "invoice_ids": tail_document_ids},
        ) == [
            {
                "invoice_number": number,
                "net": sum(line_amounts),
                "vat": vat,
                "gross": sum(line_amounts) + vat,
                "base_net": Decimal("357.860"),
                "base_vat": base_vat,
                "base_gross": Decimal("357.860") + base_vat,
            }
            for number, vat, line_amounts, base_vat in tail_cases
        ]
        # M12 foundation remains additive: existing financial snapshots and
        # lifecycle values do not move, while its new compatibility metadata
        # is filled from persisted rows only.
        assert _query(
            self.url,
            """
            SELECT document_kind::text, payable_before_payments, due_amount,
                   incoming_payment_total, settlement_status::text,
                   issued_at, issued_by_user_id
            FROM invoice WHERE id = :invoice_id
            """,
            {"invoice_id": invoice_id},
        ) == [
            {
                "document_kind": "STANDARD",
                "payable_before_payments": Decimal("121.006"),
                "due_amount": Decimal("61.006"),
                "incoming_payment_total": Decimal("60.000"),
                "settlement_status": "PARTIALLY_SETTLED",
                "issued_at": None,
                "issued_by_user_id": None,
            }
        ]
        assert _query(
            self.url,
            "SELECT set_config('jai.company_id', CAST(:company_id AS text), true); "
            "SELECT provenance::text FROM invoice_party_snapshot WHERE invoice_id = :invoice_id",
            {"company_id": str(company_id), "invoice_id": invoice_id},
        ) == [{"provenance": "MIGRATED_CURRENT_STATE"}]
        assert _query(
            self.url,
            "SELECT direction::text, credit_note_id FROM payment WHERE id = :payment_id",
            {"payment_id": payment_id},
        ) == [{"direction": "INCOMING", "credit_note_id": None}]
        assert _query(
            self.url,
            "SELECT set_config('jai.company_id', CAST(:company_id AS text), true); "
            "SELECT net_amount, vat_amount, gross_amount, base_net_amount, base_vat_amount, "
            "base_gross_amount, effective_vat_percent FROM invoice_credit_basis_line "
            "WHERE invoice_line_id = :line_id",
            {"company_id": str(company_id), "line_id": line_id},
        ) == [
            {
                "net_amount": Decimal("100.001"),
                "vat_amount": Decimal("21.005"),
                "gross_amount": Decimal("121.006"),
                "base_net_amount": Decimal("100.001"),
                "base_vat_amount": Decimal("21.005"),
                "base_gross_amount": Decimal("121.006"),
                "effective_vat_percent": Decimal("21.000"),
            }
        ]
        # The production-shaped fixture deliberately contains both legacy tax
        # layouts.  0029 may add compatibility state, but it must preserve
        # every persisted reporting input and derive basis only from those
        # snapshots: LINE reads its line-tax amount; DOCUMENT apportions the
        # persisted document tax across its unchanged line bases.
        assert _query(
            self.url,
            """
            SELECT i.invoice_number, i.tax_mode::text, i.status::text, i.paid_status::text,
                   i.subtotal_excl_vat, i.taxable_amount, i.vat_total,
                   i.total_incl_vat, i.due_amount, i.base_total_incl_vat,
                   COALESCE(lt.tax_amount, it.tax_amount) AS persisted_tax_input
            FROM invoice i
            LEFT JOIN invoice_line l ON l.invoice_id = i.id AND l.sort_order = 0
            LEFT JOIN invoice_line_tax lt ON lt.invoice_line_id = l.id
            LEFT JOIN invoice_tax it ON it.invoice_id = i.id
            WHERE i.id IN (:invoice_id, :document_invoice_id)
            ORDER BY i.invoice_number
            """,
            {"invoice_id": invoice_id, "document_invoice_id": document_invoice_id},
        ) == [
            {
                "invoice_number": "LEGACY-1",
                "tax_mode": "LINE",
                "status": "SENT",
                "paid_status": "PARTIALLY_PAID",
                "subtotal_excl_vat": Decimal("100.001"),
                "taxable_amount": Decimal("100.001"),
                "vat_total": Decimal("21.005"),
                "total_incl_vat": Decimal("121.006"),
                "due_amount": Decimal("61.006"),
                "base_total_incl_vat": Decimal("121.006"),
                "persisted_tax_input": Decimal("21.005"),
            },
            {
                "invoice_number": "LEGACY-DOC",
                "tax_mode": "DOCUMENT",
                "status": "SENT",
                "paid_status": "UNPAID",
                "subtotal_excl_vat": Decimal("290.000"),
                "taxable_amount": Decimal("290.000"),
                "vat_total": Decimal("60.905"),
                "total_incl_vat": Decimal("350.905"),
                "due_amount": Decimal("350.905"),
                "base_total_incl_vat": Decimal("433.027"),
                "persisted_tax_input": Decimal("60.905"),
            },
        ]
        assert _query(
            self.url,
            "SELECT set_config('jai.company_id', CAST(:company_id AS text), true); "
            "SELECT invoice_line_id, sort_order, net_amount, vat_amount, gross_amount, "
            "base_net_amount, base_vat_amount, base_gross_amount, effective_vat_percent "
            "FROM invoice_credit_basis_line WHERE invoice_id = :invoice_id ORDER BY sort_order",
            {"company_id": str(company_id), "invoice_id": document_invoice_id},
        ) == [
            {
                "invoice_line_id": document_line_one_id,
                "sort_order": 0,
                "net_amount": Decimal("100.000"),
                "vat_amount": Decimal("21.002"),
                "gross_amount": Decimal("121.002"),
                "base_net_amount": Decimal("123.400"),
                "base_vat_amount": Decimal("25.920"),
                "base_gross_amount": Decimal("149.320"),
                "effective_vat_percent": Decimal("21.000"),
            },
            {
                "invoice_line_id": document_line_two_id,
                "sort_order": 1,
                "net_amount": Decimal("190.000"),
                "vat_amount": Decimal("39.903"),
                "gross_amount": Decimal("229.903"),
                "base_net_amount": Decimal("234.460"),
                "base_vat_amount": Decimal("49.247"),
                "base_gross_amount": Decimal("283.707"),
                "effective_vat_percent": Decimal("21.000"),
            },
        ]

    def test_0028_downgrade_refuses_quote_provenance_and_tax_snapshots(self) -> None:
        """Downgrade must fail before DDL instead of destroying M11.5 history."""
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0028", url=self.url).returncode == 0
        company_id = uuid.uuid4()
        customer_id = uuid.uuid4()
        treatment_id = uuid.uuid4()
        invoice_id = uuid.uuid4()
        quote_id = uuid.uuid4()
        payment_id = uuid.uuid4()
        tax_id = uuid.uuid4()
        _query(
            self.url,
            """
            INSERT INTO company (id, name, base_currency)
            VALUES (:company_id, 'Downgrade safety Co', 'EUR');
            INSERT INTO customer (id, company_id, name)
            VALUES (:customer_id, :company_id, 'Safety customer');
            INSERT INTO vat_treatment
                (id, company_id, code, label, side, effect, requires_icp, active)
            VALUES (:treatment_id, :company_id, 'NL_DOMESTIC', 'NL Domestic',
                    'SALES', 'APPLY_RATE', false, true);
            INSERT INTO invoice
                (id, company_id, customer_id, invoice_number, sequence_number,
                 invoice_date, status, paid_status, currency, exchange_rate,
                 tax_mode, amounts_include_vat, vat_treatment_id,
                 vat_treatment_code, vat_treatment_label, vat_treatment_effect,
                 vat_treatment_requires_icp, discount_type, discount_value,
                 document_discount_amount, subtotal_excl_vat, line_discount_total,
                 taxable_amount, vat_total, total_incl_vat, due_amount,
                 base_subtotal_excl_vat, base_line_discount_total,
                 base_taxable_amount, base_vat_total, base_total_incl_vat,
                 base_due_amount)
            VALUES (:invoice_id, :company_id, :customer_id, 'DOWN-INV-1', 1,
                    DATE '2026-01-10', 'DRAFT', 'UNPAID', 'EUR', 1,
                    'LINE', false, :treatment_id,
                    'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE', false,
                    'NONE', 0, 0, 100, 0, 100, 21, 121, 121,
                    100, 0, 100, 21, 121, 121);
            INSERT INTO quote
                (id, company_id, customer_id, quote_number, sequence_number,
                 quote_date, status, converted_invoice_id, currency, exchange_rate,
                 tax_mode, amounts_include_vat, vat_treatment_id,
                 vat_treatment_code, vat_treatment_label, vat_treatment_effect,
                 vat_treatment_requires_icp, discount_type, discount_value,
                 document_discount_amount, subtotal_excl_vat, line_discount_total,
                 taxable_amount, vat_total, total_incl_vat,
                 base_subtotal_excl_vat, base_line_discount_total,
                 base_taxable_amount, base_vat_total, base_total_incl_vat)
            VALUES (:quote_id, :company_id, :customer_id, 'DOWN-QUOTE-1', 1,
                    DATE '2026-01-01', 'ACCEPTED', :invoice_id, 'EUR', 1,
                    'LINE', false, :treatment_id,
                    'NL_DOMESTIC', 'NL Domestic', 'APPLY_RATE', false,
                    'NONE', 0, 0, 100, 0, 100, 21, 121,
                    100, 0, 100, 21, 121);
            INSERT INTO payment
                (id, company_id, invoice_id, quote_id, payment_date, amount, base_amount,
                 currency, exchange_rate)
            VALUES (:payment_id, :company_id, :invoice_id, :quote_id,
                    DATE '2026-01-05', 60, 60, 'EUR', 1);
            INSERT INTO payment_tax
                (id, payment_id, vat_rate_label, vat_rate_percent,
                 vat_treatment_code, vat_treatment_effect, vat_treatment_requires_icp,
                 taxable_amount, vat_amount, gross_amount,
                 base_taxable_amount, base_vat_amount, base_gross_amount,
                 bucket_key, sort_order)
            VALUES (:tax_id, :payment_id, 'NL standard (21%)', 21,
                    'NL_DOMESTIC', 'APPLY_RATE', false,
                    49.587, 10.413, 60, 49.587, 10.413, 60,
                    'NL_DOMESTIC|APPLY_RATE|0|21', 0)
            """,
            {
                "company_id": company_id,
                "customer_id": customer_id,
                "treatment_id": treatment_id,
                "invoice_id": invoice_id,
                "quote_id": quote_id,
                "payment_id": payment_id,
                "tax_id": tax_id,
            },
        )

        result = _run_alembic("downgrade", "0027", url=self.url)
        assert result.returncode != 0
        assert "Cannot downgrade 0028" in result.stderr
        # The guard runs before any DDL, so both provenance and VAT data survive.
        assert _query(
            self.url,
            "SELECT invoice_id, quote_id FROM payment WHERE id = :payment_id",
            {"payment_id": payment_id},
        ) == [{"invoice_id": invoice_id, "quote_id": quote_id}]
        assert _query(
            self.url,
            "SELECT id FROM payment_tax WHERE id = :tax_id",
            {"tax_id": tax_id},
        ) == [{"id": tax_id}]
        assert _query(
            self.url,
            "SELECT version_num FROM alembic_version",
        ) == [{"version_num": "0028"}]
        # Explicitly remove the test-only non-representable rows, then prove
        # a deliberate cleanup permits the normal downgrade path again.
        _query(self.url, "DELETE FROM payment_tax; DELETE FROM payment")
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0

    def test_0028_downgrade_refuses_quote_provenance_without_payment_tax(self) -> None:
        """A quote-only deposit independently blocks downgrade before any DDL."""
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0028", url=self.url).returncode == 0
        ids = _insert_0028_downgrade_sentinel(self.url, quote_provenance=True, payment_tax=False)

        result = _run_alembic("downgrade", "0027", url=self.url)
        assert result.returncode != 0
        assert "Cannot downgrade 0028" in result.stderr
        assert _query(
            self.url,
            "SELECT invoice_id, quote_id FROM payment WHERE id = :payment_id",
            {"payment_id": ids["payment_id"]},
        ) == [{"invoice_id": None, "quote_id": ids["quote_id"]}]
        assert _query(self.url, "SELECT count(*) AS count FROM payment_tax") == [{"count": 0}]
        assert _query(
            self.url,
            "SELECT to_regclass('public.payment_tax')::text AS payment_tax_table, "
            "(SELECT version_num FROM alembic_version) AS version_num",
        ) == [{"payment_tax_table": "payment_tax", "version_num": "0028"}]
        assert _query(
            self.url,
            "SELECT delete_rule FROM information_schema.referential_constraints "
            "WHERE constraint_name = 'fk_payment_quote'",
        ) == [{"delete_rule": "RESTRICT"}]
        _query(self.url, "DELETE FROM payment")
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0

    def test_0028_downgrade_refuses_payment_tax_without_quote_provenance(self) -> None:
        """A VAT snapshot independently blocks downgrade before any DDL."""
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0028", url=self.url).returncode == 0
        ids = _insert_0028_downgrade_sentinel(self.url, quote_provenance=False, payment_tax=True)

        result = _run_alembic("downgrade", "0027", url=self.url)
        assert result.returncode != 0
        assert "Cannot downgrade 0028" in result.stderr
        assert _query(
            self.url,
            "SELECT invoice_id, quote_id FROM payment WHERE id = :payment_id",
            {"payment_id": ids["payment_id"]},
        ) == [{"invoice_id": ids["invoice_id"], "quote_id": None}]
        assert _query(
            self.url,
            "SELECT payment_id, vat_rate_percent, gross_amount FROM payment_tax WHERE id = :tax_id",
            {"tax_id": ids["tax_id"]},
        ) == [
            {
                "payment_id": ids["payment_id"],
                "vat_rate_percent": Decimal("21.000"),
                "gross_amount": Decimal("60.000"),
            }
        ]
        assert _query(
            self.url,
            "SELECT to_regclass('public.payment_tax')::text AS payment_tax_table, "
            "(SELECT version_num FROM alembic_version) AS version_num",
        ) == [{"payment_tax_table": "payment_tax", "version_num": "0028"}]
        assert _query(
            self.url,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'payment' AND column_name = 'invoice_id'",
        ) == [{"is_nullable": "YES"}]
        _query(self.url, "DELETE FROM payment_tax; DELETE FROM payment")
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0

    @pytest.mark.parametrize(
        ("setup_sql", "expected_error"),
        [
            (
                "INSERT INTO expense_category (company_id, name, default_deductible, active) "
                "VALUES (:company_id, 'Mileage', false, false)",
                "existing Mileage category",
            ),
            (
                """INSERT INTO vat_treatment
                    (company_id, code, label, side, effect, report_box, requires_icp,
                     deductible, active)
                    VALUES (:company_id, 'NL_PRIVATE_TRANSPORT_MILEAGE', 'Wrong', 'SALES',
                            'APPLY_RATE', '1a', false, true, true)""",
                "existing NL_PRIVATE_TRANSPORT_MILEAGE VAT treatment",
            ),
        ],
    )
    def test_0025_to_0026_refuses_unsafe_named_seed_conflicts(
        self, setup_sql: str, expected_error: str
    ) -> None:
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0025", url=self.url).returncode == 0
        _query(
            self.url,
            "INSERT INTO company (id, name, base_currency) "
            "VALUES (:company_id, 'Conflict Co', 'EUR')",
            {"company_id": uuid.uuid4()},
        )
        company_id = _query(self.url, "SELECT id FROM company WHERE name = 'Conflict Co'")[0]["id"]
        _query(self.url, setup_sql, {"company_id": company_id})
        result = _run_alembic("upgrade", "0026", url=self.url)
        assert result.returncode != 0
        assert expected_error in result.stderr

    def test_0026_partial_rate_indexes_reject_general_and_type_specific_duplicates(self) -> None:
        assert _run_alembic("downgrade", "base", url=self.url).returncode == 0
        assert _run_alembic("upgrade", "0026", url=self.url).returncode == 0
        company_id = _query(self.url, "SELECT id FROM company LIMIT 1")
        if not company_id:
            _query(self.url, "INSERT INTO company (name, base_currency) VALUES ('Rate Co', 'EUR')")
            company_id = _query(self.url, "SELECT id FROM company WHERE name = 'Rate Co'")
        company = company_id[0]["id"]
        _query(
            self.url,
            """
            INSERT INTO mileage_transport_type (company_id, name, active)
            VALUES (:company_id, 'Car', true);
            INSERT INTO mileage_rate (company_id, effective_from, rate_per_km)
            VALUES (:company_id, DATE '2024-01-01', 0.230)
            """,
            {"company_id": company},
        )
        type_id = _query(
            self.url,
            "SELECT id FROM mileage_transport_type WHERE company_id = :company_id AND name = 'Car'",
            {"company_id": company},
        )[0]["id"]

        async def _assert_conflicts() -> None:
            engine = create_async_engine(self.url)
            try:
                async with engine.connect() as conn:
                    async with conn.begin():
                        with pytest.raises(IntegrityError):
                            async with conn.begin_nested():
                                await conn.execute(
                                    text(
                                        "INSERT INTO mileage_rate "
                                        "(company_id, effective_from, rate_per_km) "
                                        "VALUES (:company_id, DATE '2024-01-01', 0.999)"
                                    ),
                                    {"company_id": company},
                                )
                        await conn.execute(
                            text(
                                "INSERT INTO mileage_rate "
                                "(company_id, transport_type_id, effective_from, rate_per_km) "
                                "VALUES (:company_id, :type_id, DATE '2024-01-01', 0.999)"
                            ),
                            {"company_id": company, "type_id": type_id},
                        )
                        with pytest.raises(IntegrityError):
                            async with conn.begin_nested():
                                await conn.execute(
                                    text(
                                        "INSERT INTO mileage_rate "
                                        "(company_id, transport_type_id, "
                                        "effective_from, rate_per_km) "
                                        "VALUES (:company_id, :type_id, "
                                        "DATE '2024-01-01', 0.998)"
                                    ),
                                    {"company_id": company, "type_id": type_id},
                                )
            finally:
                await engine.dispose()

        asyncio.run(_assert_conflicts())

    def test_downgrade_base(self) -> None:
        """``alembic downgrade base`` should succeed after upgrade."""
        result = _run_alembic("upgrade", "head", url=self.url)
        assert result.returncode == 0
        result = _run_alembic("downgrade", "base", url=self.url)
        assert result.returncode == 0, (
            f"downgrade base failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_upgrade_then_upgrade_is_idempotent(self) -> None:
        """Running ``upgrade head`` twice should be a no-op (not error)."""
        result = _run_alembic("upgrade", "head", url=self.url)
        assert result.returncode == 0
        result = _run_alembic("upgrade", "head", url=self.url)
        assert result.returncode == 0

    def test_downgrade_at_base_is_safe(self) -> None:
        """``downgrade base`` when already at base should not error."""
        result = _run_alembic("downgrade", "base", url=self.url)
        assert result.returncode == 0
        result = _run_alembic("downgrade", "base", url=self.url)
        assert result.returncode == 0
