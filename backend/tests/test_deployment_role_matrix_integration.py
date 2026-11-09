"""Isolated deployment-role matrix against disposable PostgreSQL 18 servers.

This is deliberately an integration test rather than a Compose smoke script:
every server, network, volume, database URL and HTTP port is generated per
test.  It must consequently never discover, connect to, stop, or reuse a
developer's ``jai-*`` deployment.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
PROVISION_SCRIPT = REPO_ROOT / "docker/postgres-init/provision-application-roles.sh"


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run one command and keep its output in pytest's failure report."""
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed: {' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def _docker_is_available() -> bool:
    try:
        return (
            subprocess.run(
                ("docker", "info"), text=True, capture_output=True, check=False, timeout=5
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_postgres(name: str, user: str) -> None:
    """Wait for a named disposable server before a host process connects."""
    for _ in range(60):
        result = subprocess.run(
            ("docker", "exec", name, "pg_isready", "-U", user),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    pytest.fail(f"temporary PostgreSQL did not become ready: {name}")


def _test_env(**values: str) -> dict[str, str]:
    """Return a child environment that cannot inherit a deployment URL."""
    env = os.environ.copy()
    for key in (
        "DATABASE_URL",
        "DATABASE_MIGRATION_URL",
        "DATABASE_PROVISION_URL",
        "DATABASE_LEGACY_PROVISION_URL",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_ADMIN_USER",
        "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_LEGACY_USER",
        "POSTGRES_LEGACY_PASSWORD",
        "POSTGRES_MIGRATION_USER",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_APP_USER",
        "POSTGRES_APP_PASSWORD",
    ):
        env.pop(key, None)
    env.update(values)
    return env


class _Postgres18:
    """A single disposable PG18 container with only UUID-derived resources."""

    def __init__(self, label: str, *, user: str, password: str, database: str) -> None:
        suffix = uuid.uuid4().hex
        self.name = f"jai-matrix-{label}-{suffix}"
        self.network = f"jai-matrix-network-{suffix}"
        self.volume = f"jai-matrix-volume-{suffix}"
        self.user = user
        self.password = password
        self.database = database
        self.port = 0

    def start(self) -> None:
        _run("docker", "network", "create", self.network)
        _run("docker", "volume", "create", self.volume)
        _run(
            "docker",
            "run",
            "-d",
            "--name",
            self.name,
            "--network",
            self.network,
            "--volume",
            # PostgreSQL 18 moved its versioned data directory under this
            # parent mount; mounting the pre-18 ``.../data`` path causes the
            # image's initdb layout to fail before it can accept connections.
            f"{self.volume}:/var/lib/postgresql",
            "-e",
            f"POSTGRES_USER={self.user}",
            "-e",
            f"POSTGRES_PASSWORD={self.password}",
            "-e",
            f"POSTGRES_DB={self.database}",
            "-p",
            "127.0.0.1::5432",
            "postgres:18",
        )
        _wait_for_postgres(self.name, self.user)
        mapped = _run("docker", "port", self.name, "5432/tcp").strip()
        self.port = int(mapped.rsplit(":", maxsplit=1)[1])

    def psql(self, sql: str, *, user: str | None = None, password: str | None = None) -> str:
        return _run(
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={password or self.password}",
            "-i",
            self.name,
            "psql",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            user or self.user,
            "--dbname",
            self.database,
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        )

    def stop(self) -> None:
        subprocess.run(("docker", "rm", "-f", self.name), check=False, capture_output=True)
        subprocess.run(
            ("docker", "volume", "rm", "-f", self.volume), check=False, capture_output=True
        )
        subprocess.run(("docker", "network", "rm", self.network), check=False, capture_output=True)


def _run_provision(
    server: _Postgres18,
    *,
    admin_user: str,
    admin_password: str,
    legacy_user: str,
    legacy_password: str,
    migration_user: str,
    migration_password: str,
    app_user: str,
    app_password: str,
    admin_database_url: str | None = None,
) -> None:
    """Execute the shipped provisioning script through a second PG18 client."""
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"{server.name}-provision-{uuid.uuid4().hex}",
        "--network",
        server.network,
        "--volume",
        f"{PROVISION_SCRIPT}:/provision/provision-application-roles.sh:ro",
        "-e",
        f"POSTGRES_HOST={server.name}",
        "-e",
        "POSTGRES_PORT=5432",
        "-e",
        f"POSTGRES_DB={server.database}",
        "-e",
        f"POSTGRES_ADMIN_USER={admin_user}",
        "-e",
        f"POSTGRES_ADMIN_PASSWORD={admin_password}",
        "-e",
        f"POSTGRES_LEGACY_USER={legacy_user}",
        "-e",
        f"POSTGRES_LEGACY_PASSWORD={legacy_password}",
        "-e",
        f"POSTGRES_MIGRATION_USER={migration_user}",
        "-e",
        f"POSTGRES_MIGRATION_PASSWORD={migration_password}",
        "-e",
        f"POSTGRES_APP_USER={app_user}",
        "-e",
        f"POSTGRES_APP_PASSWORD={app_password}",
        "postgres:18",
        "sh",
        "/provision/provision-application-roles.sh",
    ]
    if admin_database_url is not None:
        command[11:11] = ["-e", f"DATABASE_URL={admin_database_url}"]
    _run(*command)


def _upgrade_host(url: str, env: dict[str, str], revision: str = "head") -> None:
    """Run explicitly host-side Alembic for legacy/dev-only test paths."""
    migration_env = env.copy()
    migration_env["DATABASE_URL"] = url
    _run(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        revision,
        cwd=BACKEND_ROOT,
        env=migration_env,
    )


def _upgrade_from_service_environment(env: dict[str, str], revision: str = "head") -> None:
    """Run Alembic using only a rendered Compose service environment."""
    assert env.get("DATABASE_URL")
    _run(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        revision,
        cwd=BACKEND_ROOT,
        env=env,
    )


def _render_compose(env: dict[str, str]) -> dict[str, Any]:
    """Render production Compose with an isolated, explicitly supplied env."""
    return json.loads(
        _run(
            "docker",
            "compose",
            "-f",
            str(REPO_ROOT / "docker-compose.yml"),
            "config",
            "--format",
            "json",
            cwd=REPO_ROOT,
            env=env,
        )
    )


def _service_environment(compose: dict[str, Any], service: str) -> dict[str, str]:
    """Return a rendered service environment suitable for a host subprocess."""
    raw = compose["services"][service]["environment"]
    assert isinstance(raw, dict)
    return {str(key): str(value) for key, value in raw.items()}


def _settings_database_url(env: dict[str, str]) -> str:
    """Instantiate Settings in a clean child process, exactly as a service does."""
    return _run(
        sys.executable,
        "-c",
        "from jai.config import Settings; print(Settings().database_url)",
        cwd=BACKEND_ROOT,
        env=env,
    ).strip()


def _settings_migration_database_url(env: dict[str, str]) -> str:
    """Resolve the Alembic URL exactly as its Settings-based env.py does."""
    return _run(
        sys.executable,
        "-c",
        "from jai.config import Settings; print(Settings().migration_database_url)",
        cwd=BACKEND_ROOT,
        env=env,
    ).strip()


def _role_flags(url: str) -> dict[str, Any]:
    async def query() -> dict[str, Any]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT current_user AS current_user, r.rolsuper, r.rolbypassrls, "
                                "has_schema_privilege(current_user, 'public', 'CREATE') AS can_ddl "
                                "FROM pg_roles r WHERE r.rolname = current_user"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return dict(row)
        finally:
            await engine.dispose()

    return asyncio.run(query())


def _runtime_privilege_flags(url: str) -> dict[str, Any]:
    """Check current runtime access to existing and post-0031 objects."""

    async def query() -> dict[str, Any]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT "
                                "has_table_privilege("
                                "current_user, 'invoice', 'SELECT') AS invoice_read, "
                                "has_table_privilege("
                                "current_user, 'runtime_role_grant_probe', "
                                "'SELECT, INSERT, UPDATE, DELETE') AS default_table_grant, "
                                "has_sequence_privilege("
                                "current_user, 'runtime_role_grant_probe_seq', "
                                "'USAGE') AS default_sequence_usage, "
                                "has_sequence_privilege("
                                "current_user, 'runtime_role_grant_probe_seq', "
                                "'SELECT') AS default_sequence_select, "
                                "has_sequence_privilege("
                                "current_user, 'runtime_role_grant_probe_seq', "
                                "'UPDATE') AS default_sequence_update, "
                                "(SELECT bool_and(has_sequence_privilege("
                                "current_user, c.oid, 'USAGE')) FROM pg_class c "
                                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                                "WHERE c.relkind = 'S' AND n.nspname = 'public') "
                                "AS existing_sequence_usage, "
                                "(SELECT bool_and(has_sequence_privilege("
                                "current_user, c.oid, 'SELECT')) FROM pg_class c "
                                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                                "WHERE c.relkind = 'S' AND n.nspname = 'public') "
                                "AS existing_sequence_select, "
                                "(SELECT bool_and(NOT has_sequence_privilege("
                                "current_user, c.oid, 'UPDATE')) FROM pg_class c "
                                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                                "WHERE c.relkind = 'S' AND n.nspname = 'public') "
                                "AS existing_sequence_update_revoked"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return dict(row)
        finally:
            await engine.dispose()

    return asyncio.run(query())


def _agent_operation_privilege_flags(url: str) -> dict[str, bool]:
    """Check runtime access to the append-only Agent operation log."""

    async def query() -> dict[str, bool]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT "
                            "has_table_privilege(current_user, 'public.agent_operation', "
                            "'SELECT') AS can_select, "
                            "has_table_privilege(current_user, 'public.agent_operation', "
                            "'INSERT') AS can_insert, "
                            "has_table_privilege(current_user, 'public.agent_operation', "
                            "'UPDATE') AS can_update, "
                            "has_table_privilege(current_user, 'public.agent_operation', "
                            "'DELETE') AS can_delete"
                        )
                    )
                ).mappings().one()
                return {key: bool(value) for key, value in row.items()}
        finally:
            await engine.dispose()

    return asyncio.run(query())


def _create_default_privilege_probe(url: str) -> None:
    """Create objects as the migration owner after 0031 grants defaults."""

    async def create() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE runtime_role_grant_probe "
                        "(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
                    )
                )
                await conn.execute(text("CREATE SEQUENCE runtime_role_grant_probe_seq"))
        finally:
            await engine.dispose()

    asyncio.run(create())


def _assert_runtime_sequence_behavior(url: str) -> None:
    """The runtime role may consume sequences but cannot move their counters."""

    async def assert_behavior() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT nextval('runtime_role_grant_probe_seq')"))
                await conn.execute(text("INSERT INTO runtime_role_grant_probe DEFAULT VALUES"))
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text("SELECT setval('runtime_role_grant_probe_seq', 42, true)")
                        )
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text("UPDATE runtime_role_grant_probe_seq SET last_value = 42")
                        )
        finally:
            await engine.dispose()

    asyncio.run(assert_behavior())


def _expected_runtime_privilege_flags() -> dict[str, bool]:
    return {
        "invoice_read": True,
        "default_table_grant": True,
        "default_sequence_usage": True,
        "default_sequence_select": True,
        "default_sequence_update": False,
        "existing_sequence_usage": True,
        "existing_sequence_select": True,
        "existing_sequence_update_revoked": True,
    }


def _connection_identity(url: str) -> dict[str, Any]:
    """Return the authenticated role and actual PostgreSQL target instance."""

    async def query() -> dict[str, Any]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT current_user, current_database() AS database, "
                                "inet_server_addr()::text AS server_address, "
                                "inet_server_port() AS server_port"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return dict(row)
        finally:
            await engine.dispose()

    return asyncio.run(query())


def _wait_for_health(process: subprocess.Popen[str], port: int) -> None:
    url = f"http://127.0.0.1:{port}/api/health"
    for _ in range(60):
        if process.poll() is not None:
            output = process.communicate(timeout=2)[0]
            pytest.fail(f"runtime server exited before health check:\n{output}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                body = json.loads(response.read())
                assert body["status"] == "ok"
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.5)
    pytest.fail("runtime server did not answer /api/health")


def _wait_for_http_health(port: int) -> None:
    """Wait for an isolated Compose app without handling any named deployment."""
    url = f"http://127.0.0.1:{port}/api/health"
    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                assert json.loads(response.read())["status"] == "ok"
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.5)
    pytest.fail("isolated Compose runtime did not answer /api/health")


def _runtime_smoke(env: dict[str, str]) -> None:
    """Start the real ASGI process using only the supplied runtime role."""
    port = _free_port()
    process = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "uvicorn",
            "jai.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(process, port)
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/users/me")
        with pytest.raises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request, timeout=3)  # noqa: S310
        assert response.value.code == 401
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)


def _database_urls(
    port: int,
    database: str,
    *,
    admin_user: str,
    admin_password: str,
    migration_user: str,
    migration_password: str,
    app_user: str,
    app_password: str,
) -> tuple[str, str, str]:
    def build(user: str, password: str) -> str:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=user,
            password=password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)

    return (
        build(admin_user, admin_password),
        build(migration_user, migration_password),
        build(app_user, app_password),
    )


def _provision_url(host: str, database: str, user: str, password: str) -> str:
    """Return a libpq URL for the optional provisioning override path."""
    return URL.create(
        drivername="postgresql",
        username=user,
        password=password,
        host=host,
        port=5432,
        database=database,
    ).render_as_string(hide_password=False)


def _only_migration_owner(owners: str, migration_user: str) -> bool:
    """Accept PostgreSQL's array rendering only when every owner is migrator."""
    return (
        owners.replace(migration_user, "")
        .replace("{", "")
        .replace("}", "")
        .replace(",", "")
        .strip()
        == ""
    )


@pytest.fixture
def disposable_servers() -> Iterator[list[_Postgres18]]:
    """Always remove only the resources that this test named itself."""
    servers: list[_Postgres18] = []
    try:
        yield servers
    finally:
        for server in reversed(servers):
            server.stop()


def test_deployment_role_matrix_uses_external_urls_legacy_volumes_and_dev_isolation(
    disposable_servers: list[_Postgres18], tmp_path: Path
) -> None:
    """Exercise the deploy path without ever touching a default JAI deployment."""
    if not _docker_is_available():
        pytest.skip("requires a Docker daemon; CI integration runners must provide one")

    # Fresh external PostgreSQL: full URL compatibility accepts reserved URI
    # characters, while internal credential paths below receive these exact,
    # unencoded passwords as independent environment fields.
    admin_user, admin_password = "matrix_admin", "admin@secret:/#"
    migration_user, migration_password = "matrix_migrator", "migration@secret:/#"
    # A hyphen is legal for a PostgreSQL role but requires quoted identifier
    # syntax.  Provisioning and migration must agree on this exact name.
    app_user, app_password = "runtime-role", "runtime@secret:/#"
    fresh = _Postgres18("external", user=admin_user, password=admin_password, database="matrix_jai")
    disposable_servers.append(fresh)
    fresh.start()
    admin_url, migration_url, runtime_url = _database_urls(
        fresh.port,
        fresh.database,
        admin_user=admin_user,
        admin_password=admin_password,
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
    )
    provision_url = _provision_url(fresh.name, fresh.database, admin_user, admin_password)
    _run_provision(
        fresh,
        admin_user=admin_user,
        admin_password=admin_password,
        legacy_user="missing_legacy_role",
        legacy_password="unused",
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
        admin_database_url=provision_url,
    )
    external_compose = _render_compose(
        _test_env(
            DATABASE_PROVISION_URL=provision_url,
            DATABASE_MIGRATION_URL=migration_url,
            DATABASE_URL=runtime_url,
            POSTGRES_ADMIN_USER=admin_user,
            POSTGRES_ADMIN_PASSWORD=admin_password,
            POSTGRES_MIGRATION_USER=migration_user,
            POSTGRES_MIGRATION_PASSWORD=migration_password,
            POSTGRES_APP_USER=app_user,
            POSTGRES_APP_PASSWORD=app_password,
            POSTGRES_DB=fresh.database,
        )
    )
    migration_environment = _test_env(**_service_environment(external_compose, "db-migration"))
    app_environment = _test_env(**_service_environment(external_compose, "app"))
    assert migration_environment["DATABASE_URL"] == migration_url
    assert migration_environment["DATABASE_MIGRATION_URL"] == migration_url
    assert app_environment["DATABASE_URL"] == runtime_url
    assert migration_environment["POSTGRES_APP_USER"] == app_user
    # The rendered service environment must drive both Settings and Alembic;
    # do not bypass Compose with an Alembic -x URL argument.
    assert _settings_migration_database_url(migration_environment) == migration_url
    assert _settings_database_url(app_environment) == runtime_url
    _upgrade_from_service_environment(migration_environment)
    assert _role_flags(admin_url)["current_user"] == admin_user
    assert _role_flags(migration_url) == {
        "current_user": migration_user,
        "rolsuper": False,
        "rolbypassrls": False,
        "can_ddl": True,
    }
    assert _role_flags(runtime_url) == {
        "current_user": app_user,
        "rolsuper": False,
        "rolbypassrls": False,
        "can_ddl": False,
    }
    _create_default_privilege_probe(migration_url)
    assert _runtime_privilege_flags(runtime_url) == _expected_runtime_privilege_flags()
    _assert_runtime_sequence_behavior(runtime_url)
    migration_identity = _connection_identity(migration_url)
    runtime_identity = _connection_identity(runtime_url)
    assert migration_identity["current_user"] == migration_user
    assert runtime_identity["current_user"] == app_user
    assert migration_identity["database"] == fresh.database
    assert runtime_identity["database"] == fresh.database
    assert migration_identity["server_address"] == runtime_identity["server_address"]
    assert migration_identity["server_port"] == runtime_identity["server_port"] == 5432
    app_environment.update(COOKIE_SECURE="false", SCHEDULER_ENABLED="false")
    _runtime_smoke(app_environment)

    # An actual old 0028 volume still has the initdb ``jai`` owner.  Add the
    # object shapes that have historically made REASSIGN OWNED insufficient,
    # then run the shipped script twice before upgrading and serving it.
    legacy = _Postgres18("legacy", user="jai", password="legacy-secret", database="jai")
    disposable_servers.append(legacy)
    legacy.start()
    legacy_url = f"postgresql+asyncpg://jai:legacy-secret@127.0.0.1:{legacy.port}/jai"
    _upgrade_host(legacy_url, _test_env(), "0028")
    legacy.psql(
        """
        CREATE SCHEMA legacy_extra;
        CREATE TYPE legacy_extra.state AS ENUM ('old', 'new');
        CREATE DOMAIN legacy_extra.positive_amount AS integer CHECK (VALUE > 0);
        CREATE TYPE legacy_extra.intspan AS RANGE (
            subtype = integer, multirange_type_name = intspan_multi
        );
        CREATE TABLE legacy_extra.serial_identity (
            serial_id serial PRIMARY KEY,
            identity_id bigint GENERATED ALWAYS AS IDENTITY,
            state legacy_extra.state NOT NULL DEFAULT 'old',
            amount legacy_extra.positive_amount NOT NULL DEFAULT 1,
            span legacy_extra.intspan,
            -- PostgreSQL resolves an explicitly named generated multirange
            -- through the current search_path (public here), while its
            -- pg_range link still makes it the real companion of intspan.
            spans public.intspan_multi
        );
        CREATE SEQUENCE legacy_extra.standalone_sequence;
        CREATE FUNCTION legacy_extra.answer() RETURNS integer LANGUAGE sql AS 'SELECT 42';
        ALTER DEFAULT PRIVILEGES IN SCHEMA legacy_extra GRANT SELECT ON TABLES TO PUBLIC;
        """
    )
    for _ in range(2):
        _run_provision(
            legacy,
            admin_user="missing_admin_role",
            admin_password="unused",
            legacy_user="jai",
            legacy_password="legacy-secret",
            migration_user=migration_user,
            migration_password="migration-secret",
            app_user=app_user,
            app_password="runtime-secret",
        )
    ownership = legacy.psql(
        """
        SELECT array_agg(owner_name ORDER BY owner_name)::text
        FROM (
          SELECT n.nspowner::regrole::text AS owner_name FROM pg_namespace n
            WHERE n.nspname = 'legacy_extra'
          UNION ALL SELECT c.relowner::regrole::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'legacy_extra' AND c.relkind IN ('r', 'S')
          UNION ALL SELECT p.proowner::regrole::text FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'legacy_extra'
          UNION ALL SELECT t.typowner::regrole::text FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'legacy_extra'
              AND t.typname IN ('state', 'positive_amount', 'intspan')
        ) owners
        """
    ).strip()
    assert _only_migration_owner(ownership, migration_user)
    companions = legacy.psql(
        """
        SELECT array_agg(t.typowner::regrole::text ORDER BY t.typname)::text
        FROM pg_type t
        WHERE t.oid IN (
          SELECT rngmultitypid FROM pg_range WHERE rngtypid = 'legacy_extra.intspan'::regtype
        )
        """
    ).strip()
    assert _only_migration_owner(companions, migration_user)
    legacy_migration = (
        f"postgresql+asyncpg://{migration_user}:migration-secret@127.0.0.1:{legacy.port}/jai"
    )
    legacy_runtime = f"postgresql+asyncpg://{app_user}:runtime-secret@127.0.0.1:{legacy.port}/jai"
    # Recreate the old provisioner's sequence UPDATE ACL after the legacy
    # ownership transfer.  0030's additive GRANT leaves it in place; 0031
    # must revoke it for both this existing sequence and future defaults.
    _upgrade_host(
        legacy_migration,
        _test_env(
            POSTGRES_APP_USER=app_user,
            POSTGRES_APP_PASSWORD="runtime-secret",
            POSTGRES_MIGRATION_USER=migration_user,
            POSTGRES_MIGRATION_PASSWORD="migration-secret",
        ),
        "0029",
    )
    legacy.psql(
        """
        GRANT UPDATE ON ALL SEQUENCES IN SCHEMA public TO "runtime-role";
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT UPDATE ON SEQUENCES TO "runtime-role";
        CREATE SEQUENCE runtime_role_pre_0031_sequence_probe;
        """,
        user=migration_user,
        password="migration-secret",
    )
    _upgrade_host(
        legacy_migration,
        _test_env(
            POSTGRES_APP_USER=app_user,
            POSTGRES_APP_PASSWORD="runtime-secret",
            POSTGRES_MIGRATION_USER=migration_user,
            POSTGRES_MIGRATION_PASSWORD="migration-secret",
        ),
        "0030",
    )
    assert (
        legacy.psql(
            "SELECT has_sequence_privilege(current_user, "
            "'runtime_role_pre_0031_sequence_probe', 'UPDATE')",
            user=app_user,
            password="runtime-secret",
        ).strip()
        == "t"
    )
    _upgrade_host(
        legacy_migration,
        _test_env(
            POSTGRES_APP_USER=app_user,
            POSTGRES_APP_PASSWORD="runtime-secret",
            POSTGRES_MIGRATION_USER=migration_user,
            POSTGRES_MIGRATION_PASSWORD="migration-secret",
        ),
        "0031",
    )
    _create_default_privilege_probe(legacy_migration)
    assert _runtime_privilege_flags(legacy_runtime) == _expected_runtime_privilege_flags()
    _assert_runtime_sequence_behavior(legacy_runtime)
    assert _role_flags(legacy_runtime)["can_ddl"] is False
    # The runtime smoke must exercise the current app/schema contract.  The
    # preceding 0031 assertions intentionally pin an older schema only long
    # enough to verify its ACL migration; M12's later heads correctly refuse
    # to boot against it.
    _upgrade_host(
        legacy_migration,
        _test_env(
            POSTGRES_APP_USER=app_user,
            POSTGRES_APP_PASSWORD="runtime-secret",
            POSTGRES_MIGRATION_USER=migration_user,
            POSTGRES_MIGRATION_PASSWORD="migration-secret",
        ),
    )
    _runtime_smoke(
        _test_env(
            POSTGRES_HOST="127.0.0.1",
            POSTGRES_PORT=str(legacy.port),
            POSTGRES_DB="jai",
            POSTGRES_APP_USER=app_user,
            POSTGRES_APP_PASSWORD="runtime-secret",
            POSTGRES_MIGRATION_USER=migration_user,
            POSTGRES_MIGRATION_PASSWORD="migration-secret",
            COOKIE_SECURE="false",
            SCHEDULER_ENABLED="false",
        )
    )

    # Parse the checked-in pair first: its production names must be replaced
    # by the dev suffixes and its port is independent.  The live postgres-only
    # exercise below adds a UUID project/container/volume/network/host port.
    rendered = _run(
        "docker",
        "compose",
        "-f",
        str(REPO_ROOT / "docker-compose.yml"),
        "-f",
        str(REPO_ROOT / "docker-compose.dev.yml"),
        "config",
        "--format",
        "json",
        cwd=REPO_ROOT,
    )
    compose = json.loads(rendered)
    assert compose["name"] == "jai-dev"
    assert compose["services"]["postgres"]["container_name"] == "jai-postgres-dev"
    assert compose["services"]["app"]["container_name"] == "jai-app-dev"
    dev_port_config = compose["services"]["postgres"]["ports"][0]
    assert dev_port_config["host_ip"] == "127.0.0.1"
    assert str(dev_port_config["published"]) == "5433"
    assert dev_port_config["target"] == 5432

    project = f"jai-matrix-dev-{uuid.uuid4().hex}"
    container = f"jai-matrix-dev-postgres-{uuid.uuid4().hex}"
    override = tmp_path / "compose-matrix.yml"
    override.write_text(
        "\n".join(
            (
                f"name: {project}",
                "services:",
                "  postgres:",
                f"    container_name: {container}",
                "    ports: !override",
                '      - "127.0.0.1::5432"',
            )
        ),
        encoding="utf-8",
    )
    compose_args = (
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(REPO_ROOT / "docker-compose.yml"),
        "-f",
        str(REPO_ROOT / "docker-compose.dev.yml"),
        "-f",
        str(override),
    )
    try:
        dev_env = _test_env(POSTGRES_DEV_PORT="5433")
        _run(*compose_args, "up", "-d", "postgres", cwd=REPO_ROOT, env=dev_env)
        _wait_for_postgres(container, "jai_admin")
        port_output = _run(*compose_args, "port", "postgres", "5432", cwd=REPO_ROOT, env=dev_env)
        dev_port = int(port_output.strip().rsplit(":", 1)[1])
        dev_migration = f"postgresql+asyncpg://jai_migrator:jai_migrator@127.0.0.1:{dev_port}/jai"
        dev_runtime = f"postgresql+asyncpg://jai_app:jai_app@127.0.0.1:{dev_port}/jai"
        _upgrade_host(dev_migration, _test_env())
        assert _role_flags(dev_migration)["current_user"] == "jai_migrator"
        assert _role_flags(dev_runtime)["can_ddl"] is False
        _create_default_privilege_probe(dev_migration)
        assert _runtime_privilege_flags(dev_runtime) == _expected_runtime_privilege_flags()
        _assert_runtime_sequence_behavior(dev_runtime)
        _runtime_smoke(
            _test_env(
                POSTGRES_HOST="127.0.0.1",
                POSTGRES_PORT=str(dev_port),
                POSTGRES_DB="jai",
                POSTGRES_APP_USER="jai_app",
                POSTGRES_APP_PASSWORD="jai_app",
                POSTGRES_MIGRATION_USER="jai_migrator",
                POSTGRES_MIGRATION_PASSWORD="jai_migrator",
                COOKIE_SECURE="false",
                SCHEDULER_ENABLED="false",
            )
        )
    finally:
        subprocess.run(
            (*compose_args, "down", "--volumes", "--remove-orphans"),
            cwd=REPO_ROOT,
            env=_test_env(POSTGRES_DEV_PORT="5433"),
            check=False,
        )

    # Exercise the real provision -> migration -> app dependency chain with
    # the checked-in .env.example.  Its host fallback is localhost:5433, so
    # this catches accidental leakage of host-development parts into containers.
    stack_project = f"jai-matrix-stack-{uuid.uuid4().hex}"
    stack_port = _free_port()
    stack_admin, stack_migrator, stack_runtime = (
        "stack_admin",
        "stack_migrator",
        "stack_runtime",
    )
    stack_override = tmp_path / "compose-stack-matrix.yml"
    stack_override.write_text(
        "\n".join(
            (
                "services:",
                "  db-role-provision:",
                f"    container_name: {stack_project}-provision",
                "  db-migration:",
                f"    container_name: {stack_project}-migration",
                "  app:",
                f"    container_name: {stack_project}-app",
                "    ports: !override",
                f'      - "127.0.0.1:{stack_port}:8000"',
                "    volumes: !override",
                "      - stack_storage:/data/storage",
                "  postgres:",
                f"    container_name: {stack_project}-postgres",
                "    ports: !override []",
                "volumes:",
                "  stack_storage:",
            )
        ),
        encoding="utf-8",
    )
    stack_args = (
        "docker",
        "compose",
        "--env-file",
        str(REPO_ROOT / ".env.example"),
        "-p",
        stack_project,
        "-f",
        str(REPO_ROOT / "docker-compose.yml"),
        "-f",
        str(REPO_ROOT / "docker-compose.dev.yml"),
        "-f",
        str(stack_override),
    )
    stack_env = _test_env(
        APP_HOST_PORT=str(stack_port),
        POSTGRES_ADMIN_USER=stack_admin,
        POSTGRES_ADMIN_PASSWORD="stack@admin:/#secret",
        POSTGRES_MIGRATION_USER=stack_migrator,
        POSTGRES_MIGRATION_PASSWORD="stack@migration:/#secret",
        POSTGRES_APP_USER=stack_runtime,
        POSTGRES_APP_PASSWORD="stack@runtime:/#secret",
        POSTGRES_DB="stack_jai",
    )
    try:
        rendered_stack = json.loads(
            _run(*stack_args, "config", "--format", "json", cwd=REPO_ROOT, env=stack_env)
        )
        for service, user in (("db-migration", stack_migrator), ("app", stack_runtime)):
            service_env = _service_environment(rendered_stack, service)
            assert service_env["POSTGRES_HOST"] == "postgres"
            assert service_env["POSTGRES_PORT"] == "5432"
            clean_service_env = _test_env(**service_env)
            configured_url = (
                _settings_migration_database_url(clean_service_env)
                if service == "db-migration"
                else _settings_database_url(clean_service_env)
            )
            parsed = make_url(configured_url)
            assert parsed.host == "postgres"
            assert parsed.port == 5432
            assert parsed.username == user
        migration_stack_env = _service_environment(rendered_stack, "db-migration")
        assert migration_stack_env["POSTGRES_APP_USER"] == stack_runtime
        _run(*stack_args, "up", "-d", "--build", cwd=REPO_ROOT, env=stack_env)
        _wait_for_http_health(stack_port)
        assert sorted(
            _run(
                *stack_args,
                "ps",
                "--status",
                "exited",
                "--services",
                cwd=REPO_ROOT,
                env=stack_env,
            ).split()
        ) == ["db-migration", "db-role-provision"]
        role_flags = _run(
            *stack_args,
            "exec",
            "-T",
            "postgres",
            "psql",
            "--username",
            stack_admin,
            "--dbname",
            "stack_jai",
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT current_database(), "
            "(SELECT rolsuper FROM pg_roles WHERE rolname = 'stack_runtime'), "
            "(SELECT rolbypassrls FROM pg_roles WHERE rolname = 'stack_runtime'), "
            "has_table_privilege('stack_runtime', 'invoice', 'SELECT')",
            cwd=REPO_ROOT,
            env=stack_env,
        ).strip()
        assert role_flags == "stack_jai|f|f|t"
    finally:
        subprocess.run(
            (*stack_args, "down", "--volumes", "--remove-orphans"),
            cwd=REPO_ROOT,
            env=stack_env,
            check=False,
            capture_output=True,
            text=True,
        )


def test_0030_role_identifier_cannot_escape_grant_statement(
    disposable_servers: list[_Postgres18],
) -> None:
    """A malformed role name fails as one quoted identifier, never as SQL."""
    if not _docker_is_available():
        pytest.skip("requires a Docker daemon; CI integration runners must provide one")

    admin_user, admin_password = "identifier_admin", "admin-secret"
    migration_user, migration_password = "identifier_migrator", "migration-secret"
    app_user, app_password = "runtime-role", "runtime-secret"
    server = _Postgres18(
        "identifier",
        user=admin_user,
        password=admin_password,
        database="identifier_jai",
    )
    disposable_servers.append(server)
    server.start()
    admin_url, migration_url, _runtime_url = _database_urls(
        server.port,
        server.database,
        admin_user=admin_user,
        admin_password=admin_password,
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
    )
    _run_provision(
        server,
        admin_user=admin_user,
        admin_password=admin_password,
        legacy_user="missing_legacy_role",
        legacy_password="unused",
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
    )
    _upgrade_host(
        migration_url,
        _test_env(POSTGRES_APP_USER=app_user, POSTGRES_APP_PASSWORD=app_password),
        "0029",
    )

    malicious_role = 'runtime-role"; CREATE ROLE jai_matrix_injected; --'
    failed_env = _test_env(POSTGRES_APP_USER=malicious_role, POSTGRES_APP_PASSWORD="unused")
    result = subprocess.run(
        (sys.executable, "-m", "alembic", "upgrade", "0030"),
        cwd=BACKEND_ROOT,
        env={**failed_env, "DATABASE_URL": migration_url},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "jai_matrix_injected" not in server.psql(
        "SELECT rolname FROM pg_roles WHERE rolname = 'jai_matrix_injected'"
    )
    assert _role_flags(admin_url)["current_user"] == admin_user


def test_repeat_provisioning_preserves_agent_operation_append_only_grant(
    disposable_servers: list[_Postgres18],
) -> None:
    """A pre-migration provision and post-0056 rerun keep their intended grants."""
    if not _docker_is_available():
        pytest.skip("requires a Docker daemon; CI integration runners must provide one")

    admin_user, admin_password = "audit_admin", "admin@secret:/#"
    migration_user, migration_password = "custom-migrator", "migration@secret:/#"
    app_user, app_password = "runtime-role", "runtime@secret:/#"
    server = _Postgres18(
        "agent-audit", user=admin_user, password=admin_password, database="audit_jai"
    )
    disposable_servers.append(server)
    server.start()
    _admin_url, migration_url, runtime_url = _database_urls(
        server.port,
        server.database,
        admin_user=admin_user,
        admin_password=admin_password,
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
    )

    # The initial call must be safe while agent_operation is still absent.
    _run_provision(
        server,
        admin_user=admin_user,
        admin_password=admin_password,
        legacy_user="missing_legacy_role",
        legacy_password="unused",
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
    )
    assert server.psql(
        "SELECT to_regclass('public.agent_operation') IS NOT NULL",
        user=admin_user,
        password=admin_password,
    ).strip() == "f"

    migration_env = _test_env(
        DATABASE_URL=migration_url,
        POSTGRES_APP_USER=app_user,
        POSTGRES_APP_PASSWORD=app_password,
    )
    _upgrade_host(migration_url, migration_env)
    expected = {
        "can_select": True,
        "can_insert": True,
        "can_update": True,
        "can_delete": False,
    }
    assert _agent_operation_privilege_flags(runtime_url) == expected

    # This real script rerun used to restore DELETE via its all-tables grant.
    _run_provision(
        server,
        admin_user=admin_user,
        admin_password=admin_password,
        legacy_user="missing_legacy_role",
        legacy_password="unused",
        migration_user=migration_user,
        migration_password=migration_password,
        app_user=app_user,
        app_password=app_password,
    )
    assert _agent_operation_privilege_flags(runtime_url) == expected
