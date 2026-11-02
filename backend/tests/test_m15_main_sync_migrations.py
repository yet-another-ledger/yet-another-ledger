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
    _query,
    _run_alembic,
    _script_head,
)


def test_m15_follows_released_main_with_one_unambiguous_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("0053").down_revision == "0052"
    assert scripts.get_revision("0054").down_revision == "0053"
    revisions = list(scripts.walk_revisions())
    assert len(revisions) == len({revision.revision for revision in revisions})


@pytest.mark.integration
def test_main_0052_upgrades_to_m15_without_losing_existing_company() -> None:
    url = make_url(_alembic_url()).set(database=f"jai_test_m15_sync_{uuid.uuid4().hex[:12]}")
    isolated_url = url.render_as_string(hide_password=False)
    _ensure_database(isolated_url)
    try:
        result = _run_alembic("upgrade", "0052", url=isolated_url)
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
            "to_regclass('invoice_revision') IS NOT NULL AS revisions",
        ) == [{"tokens": True, "operations": True, "revisions": True}]
    finally:
        _drop_database(isolated_url)


def _install_legacy_m15_schema(url: str, legacy_head: str) -> None:
    """Apply the original Agent DDL at 0043, then record its historical ID."""

    def install(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            names = ["0053_m15_agent_tokens.py"]
            if legacy_head == "0045":
                names.append("0054_m15_agent_operations.py")
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
@pytest.mark.parametrize("legacy_head", ["0044", "0045"])
def test_legacy_m15_history_is_refused_before_main_migrations(legacy_head: str) -> None:
    url = make_url(_alembic_url()).set(database=f"jai_test_m15_legacy_{uuid.uuid4().hex[:12]}")
    isolated_url = url.render_as_string(hide_password=False)
    _ensure_database(isolated_url)
    try:
        result = _run_alembic("upgrade", "0043", url=isolated_url)
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
            "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'email_log' "
            "AND column_name = 'sent_copy_status') AS sent_copy",
        ) == [{"tokens": True, "revisions": False, "sent_copy": False}]
    finally:
        _drop_database(isolated_url)
