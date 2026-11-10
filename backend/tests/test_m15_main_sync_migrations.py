"""Regression coverage for the paused M15/main migration ID collision."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_migrations import (
    BACKEND_DIR,
    _alembic_url,
    _drop_database,
    _ensure_database,
    _expect_integrity_error,
    _query,
    _run_alembic,
    _script_head,
)


def test_m18_step2_follows_si4_with_one_unambiguous_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("0053").down_revision == "0052"
    assert scripts.get_revision("0054").down_revision == "0053"
    assert scripts.get_revision("0055").down_revision == "0054"
    assert scripts.get_revision("0056").down_revision == "0055"
    assert scripts.get_revision("0057").down_revision == "0056"
    assert scripts.get_revision("0058").down_revision == "0057"
    assert scripts.get_heads() == ["0058"]
    revisions = list(scripts.walk_revisions())
    assert len(revisions) == len({revision.revision for revision in revisions})


@pytest.mark.integration
def test_m18_scope_migration_preserves_old_tokens_and_guards_revoked_scope_downgrade() -> None:
    url = make_url(_alembic_url()).set(database=f"jai_test_m18_scope_{uuid.uuid4().hex[:12]}")
    isolated_url = url.render_as_string(hide_password=False)
    _ensure_database(isolated_url)
    company_ids = [uuid.uuid4(), uuid.uuid4()]
    owner_ids = [uuid.uuid4(), uuid.uuid4()]
    token_ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        result = _run_alembic("upgrade", "0057", url=isolated_url)
        assert result.returncode == 0, result.stderr
        for index, company_id in enumerate(company_ids):
            _query(
                isolated_url,
                "INSERT INTO company (id, name, base_currency) "
                "VALUES (:company_id, :name, 'EUR'); "
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:owner_id, :email, 'synthetic-test-only', :company_id)",
                {
                    "company_id": company_id,
                    "name": f"M18 company {index}",
                    "owner_id": owner_ids[index],
                    "email": f"m18-{index}-{company_id}@example.test",
                },
                company_id=company_id,
            )
            _query(
                isolated_url,
                "INSERT INTO agent_token "
                "(id, company_id, creator_id, name, selector, secret_digest, scopes) "
                "VALUES (:token_id, :company_id, :owner_id, 'synthetic token', "
                "gen_random_uuid(), decode(repeat('00', 32), 'hex'), :scopes)",
                {
                    "token_id": token_ids[index],
                    "company_id": company_id,
                    "owner_id": owner_ids[index],
                    "scopes": ["customer:read"] if index == 0 else ["expense:read"],
                },
                company_id=company_id,
            )

        result = _run_alembic("upgrade", "head", url=isolated_url)
        assert result.returncode == 0, result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0058"}
        ]
        original_scopes = [
            _query(
                isolated_url,
                "SELECT scopes, revoked_at IS NOT NULL AS revoked FROM agent_token "
                "WHERE id = :token_id",
                {"token_id": token_ids[index]},
                company_id=company_ids[index],
            )[0]
            for index in range(2)
        ]
        assert [tuple(row["scopes"]) for row in original_scopes] == [
            ("customer:read",),
            ("expense:read",),
        ]
        assert not any(row["revoked"] for row in original_scopes)
        _query(
            isolated_url,
            "UPDATE agent_token SET scopes = ARRAY['mileage:create']::text[], "
            "revoked_at = now() "
            "WHERE id = :token_id",
            {"token_id": token_ids[1]},
            company_id=company_ids[1],
        )
        _expect_integrity_error(
            isolated_url,
            "UPDATE agent_token SET scopes = ARRAY['future:scope']::text[] "
            "WHERE id = :token_id",
            {"token_id": token_ids[0]},
            company_id=company_ids[0],
        )

        result = _run_alembic("downgrade", "-1", url=isolated_url)
        assert result.returncode != 0
        assert "ck_agent_token_scopes_old" in result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0058"}
        ]
        assert _query(
            isolated_url,
            "SELECT scopes, revoked_at IS NOT NULL AS revoked FROM agent_token "
            "WHERE id = :token_id",
            {"token_id": token_ids[1]},
            company_id=company_ids[1],
        ) == [{"scopes": ["mileage:create"], "revoked": True}]

        _query(
            isolated_url,
            "UPDATE agent_token SET scopes = ARRAY['expense:read']::text[] "
            "WHERE id = :token_id",
            {"token_id": token_ids[1]},
            company_id=company_ids[1],
        )
        result = _run_alembic("downgrade", "-1", url=isolated_url)
        assert result.returncode == 0, result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0057"}
        ]
    finally:
        _drop_database(isolated_url)


@pytest.mark.integration
def test_si4_scope_migration_preserves_old_tokens_and_guards_cross_company_downgrade() -> None:
    url = make_url(_alembic_url()).set(database=f"jai_test_si4_scopes_{uuid.uuid4().hex[:12]}")
    isolated_url = url.render_as_string(hide_password=False)
    _ensure_database(isolated_url)
    company_ids = [uuid.uuid4(), uuid.uuid4()]
    owner_ids = [uuid.uuid4(), uuid.uuid4()]
    token_ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        result = _run_alembic("upgrade", "0056", url=isolated_url)
        assert result.returncode == 0, result.stderr
        for index, company_id in enumerate(company_ids):
            _query(
                isolated_url,
                "INSERT INTO company (id, name, base_currency) "
                "VALUES (:company_id, :name, 'EUR'); "
                'INSERT INTO "user" (id, email, hashed_password, company_id) '
                "VALUES (:owner_id, :email, 'synthetic-test-only', :company_id)",
                {
                    "company_id": company_id,
                    "name": f"SI4 company {index}",
                    "owner_id": owner_ids[index],
                    "email": f"si4-{index}-{company_id}@example.test",
                },
                company_id=company_id,
            )
            _query(
                isolated_url,
                "INSERT INTO agent_token "
                "(id, company_id, creator_id, name, selector, secret_digest, scopes) "
                "VALUES (:token_id, :company_id, :owner_id, 'synthetic token', "
                "gen_random_uuid(), decode(repeat('00', 32), 'hex'), :scopes)",
                {
                    "token_id": token_ids[index],
                    "company_id": company_id,
                    "owner_id": owner_ids[index],
                    "scopes": ["customer:read"],
                },
                company_id=company_id,
            )

        result = _run_alembic("upgrade", "head", url=isolated_url)
        assert result.returncode == 0, result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0058"}
        ]
        for index in range(2):
            assert _query(
                isolated_url,
                "SELECT id, scopes FROM agent_token WHERE id = :token_id",
                {"token_id": token_ids[index]},
                company_id=company_ids[index],
            ) == [{"id": token_ids[index], "scopes": ["customer:read"]}]

        _expect_integrity_error(
            isolated_url,
            "UPDATE agent_token SET scopes = ARRAY['future:scope']::text[] "
            "WHERE id = :token_id",
            {"token_id": token_ids[0]},
            company_id=company_ids[0],
        )
        for index in range(2):
            _query(
                isolated_url,
                "UPDATE agent_token SET scopes = ARRAY['expense:read']::text[] "
                "WHERE id = :token_id",
                {"token_id": token_ids[index]},
                company_id=company_ids[index],
            )

        result = _run_alembic("downgrade", "-1", url=isolated_url)
        assert result.returncode == 0, result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0057"}
        ]
        result = _run_alembic("downgrade", "-1", url=isolated_url)
        assert result.returncode != 0
        assert "ck_agent_token_scopes_old" in result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0057"}
        ]
        for index in range(2):
            assert _query(
                isolated_url,
                "SELECT id, scopes FROM agent_token WHERE id = :token_id",
                {"token_id": token_ids[index]},
                company_id=company_ids[index],
            ) == [{"id": token_ids[index], "scopes": ["expense:read"]}]
        assert _query(
            isolated_url,
            "SELECT count(*) AS count FROM pg_constraint "
            "WHERE conname = 'ck_agent_token_scopes'",
        ) == [{"count": 1}]

        for index in range(2):
            _query(
                isolated_url,
                "UPDATE agent_token SET scopes = ARRAY['customer:read']::text[] "
                "WHERE id = :token_id",
                {"token_id": token_ids[index]},
                company_id=company_ids[index],
            )
        result = _run_alembic("downgrade", "-1", url=isolated_url)
        assert result.returncode == 0, result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0056"}
        ]
        for index in range(2):
            assert _query(
                isolated_url,
                "SELECT id, scopes FROM agent_token WHERE id = :token_id",
                {"token_id": token_ids[index]},
                company_id=company_ids[index],
            ) == [{"id": token_ids[index], "scopes": ["customer:read"]}]
    finally:
        _drop_database(isolated_url)


@pytest.mark.integration
@pytest.mark.parametrize("main_head", ["0052", "0054"])
def test_released_main_upgrades_to_m15_without_losing_existing_company(main_head: str) -> None:
    url = make_url(_alembic_url()).set(database=f"jai_test_m15_sync_{uuid.uuid4().hex[:12]}")
    isolated_url = url.render_as_string(hide_password=False)
    _ensure_database(isolated_url)
    try:
        result = _run_alembic("upgrade", main_head, url=isolated_url)
        assert result.returncode == 0, result.stderr
        company_id = uuid.uuid4()
        _query(
            isolated_url,
            "INSERT INTO company (id, name, base_currency) VALUES (:id, 'Main sentinel', 'EUR')",
            {"id": company_id},
        )
        for _ in range(2):
            result = _run_alembic("upgrade", "head", url=isolated_url)
            assert result.returncode == 0, result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": _script_head()}
        ]
        assert _query(
            isolated_url, "SELECT name FROM company WHERE id = :id", {"id": company_id}
        ) == [{"name": "Main sentinel"}]
        assert _query(
            isolated_url,
            "SELECT to_regclass('agent_token') IS NOT NULL AS tokens, "
            "to_regclass('agent_operation') IS NOT NULL AS operations, "
            "to_regclass('invoice_revision') IS NOT NULL AS revisions, "
            "to_regclass('pdf_appendix_file') IS NOT NULL AS appendices",
        ) == [{"tokens": True, "operations": True, "revisions": True, "appendices": True}]
    finally:
        _drop_database(isolated_url)


def _install_legacy_m15_schema(url: str, legacy_head: str) -> None:
    """Apply Agent DDL to its historical base, then record its ambiguous ID."""

    def install(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            names = ["0055_m15_agent_tokens.py"]
            if legacy_head in {"0045", "0054"}:
                names.append("0056_m15_agent_operations.py")
            for name in names:
                spec = importlib.util.spec_from_file_location(
                    f"legacy_{name[:-3]}", BACKEND_DIR / "alembic" / "versions" / name
                )
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()
        connection.execute(
            text("UPDATE alembic_version SET version_num = :head"), {"head": legacy_head}
        )

    async def run() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(install)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
@pytest.mark.parametrize("legacy_head", ["0044", "0045", "0053", "0054"])
def test_legacy_m15_history_is_refused_before_main_migrations(legacy_head: str) -> None:
    url = make_url(_alembic_url()).set(database=f"jai_test_m15_legacy_{uuid.uuid4().hex[:12]}")
    isolated_url = url.render_as_string(hide_password=False)
    _ensure_database(isolated_url)
    try:
        main_base = "0043" if legacy_head in {"0044", "0045"} else "0052"
        result = _run_alembic("upgrade", main_base, url=isolated_url)
        assert result.returncode == 0, result.stderr
        _install_legacy_m15_schema(isolated_url, legacy_head)
        result = _run_alembic("upgrade", "head", url=isolated_url)
        assert result.returncode != 0
        assert "Legacy paused M15 migration history detected" in result.stderr
        assert _query(isolated_url, "SELECT version_num FROM alembic_version") == [
            {"version_num": legacy_head}
        ]
        assert _query(
            isolated_url,
            "SELECT to_regclass('agent_token') IS NOT NULL AS tokens, "
            "to_regclass('invoice_revision') IS NOT NULL AS revisions, "
            "to_regclass('pdf_appendix_file') IS NOT NULL AS appendices, "
            "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'email_log' "
            "AND column_name = 'sent_copy_status') AS sent_copy",
        ) == [{
            "tokens": True,
            "revisions": main_base == "0052",
            "appendices": False,
            "sent_copy": main_base == "0052",
        }]
    finally:
        _drop_database(isolated_url)
