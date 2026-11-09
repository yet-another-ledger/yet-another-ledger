#!/bin/sh
set -eu

# On a fresh cluster the image init hook has created these roles.  An old
# v0.4 volume has only POSTGRES_USER (usually `jai`) and that role owns all
# schema objects, so safely retry through its explicitly supplied credentials.

psql_as_admin() {
  if [ -n "${DATABASE_URL:-}" ]; then
    psql "$DATABASE_URL" "$@"
  elif [ -n "${POSTGRES_ADMIN_DATABASE_URL:-}" ]; then
    # Compatibility with manually-run pre-M12 provisioning commands.
    psql "$POSTGRES_ADMIN_DATABASE_URL" "$@"
  else
    PGPASSWORD="${POSTGRES_ADMIN_PASSWORD:-${PGPASSWORD:?POSTGRES_ADMIN_PASSWORD is required}}" psql --host "$POSTGRES_HOST" --port "$POSTGRES_PORT" --username "$POSTGRES_ADMIN_USER" --dbname "$POSTGRES_DB" "$@"
  fi
}

psql_as_legacy() {
  if [ -n "${POSTGRES_LEGACY_DATABASE_URL:-}" ]; then
    psql "$POSTGRES_LEGACY_DATABASE_URL" "$@"
  else
    PGPASSWORD="${POSTGRES_LEGACY_PASSWORD:?POSTGRES_LEGACY_PASSWORD is required}" psql --host "$POSTGRES_HOST" --port "$POSTGRES_PORT" --username "$POSTGRES_LEGACY_USER" --dbname "$POSTGRES_DB" "$@"
  fi
}

attempt=0
while :; do
  if psql_as_admin --set ON_ERROR_STOP=1 --command 'SELECT 1' >/dev/null 2>&1; then
    owner_connection=admin
    break
  fi
  if psql_as_legacy --set ON_ERROR_STOP=1 --command 'SELECT 1' >/dev/null 2>&1; then
    owner_connection=legacy
    break
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo 'PostgreSQL administrator or legacy bootstrap connection is unavailable.' >&2
    exit 1
  fi
  sleep 1
done

if [ "$owner_connection" = admin ]; then
  psql_as_owner() { psql_as_admin "$@"; }
else
  psql_as_owner() { psql_as_legacy "$@"; }
fi

psql_as_owner --set ON_ERROR_STOP=1 --set migration_user="$POSTGRES_MIGRATION_USER" --set migration_password="$POSTGRES_MIGRATION_PASSWORD" --set app_user="$POSTGRES_APP_USER" --set app_password="$POSTGRES_APP_PASSWORD" --set legacy_user="$POSTGRES_LEGACY_USER" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'migration_user', :'migration_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migration_user') \gexec
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
ALTER ROLE :"migration_user" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'migration_password';
ALTER ROLE :"app_user" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_password';

-- `REASSIGN OWNED` is intentionally not used: an old initdb owner also owns
-- template0/template1/postgres, and PostgreSQL refuses to transfer those
-- system-required shared objects.  Transfer every user-schema object in this
-- application database instead, including schemas/tables/sequences/functions
-- and standalone types.  This is safe to repeat and leaves cluster globals
-- untouched.
SELECT set_config('jai.legacy_owner', :'legacy_user', false);
SELECT set_config('jai.migration_owner', :'migration_user', false);
DO $transfer$
DECLARE
  old_owner oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('jai.legacy_owner'));
  new_owner name := current_setting('jai.migration_owner');
  object_row record;
BEGIN
  IF old_owner IS NULL OR current_setting('jai.legacy_owner') = new_owner THEN
    RETURN;
  END IF;
  FOR object_row IN
    SELECT n.nspname
    FROM pg_namespace n
    WHERE n.nspowner = old_owner
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_'
  LOOP
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', object_row.nspname, new_owner);
  END LOOP;
  FOR object_row IN
    SELECT c.oid::regclass AS object_name, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relowner = old_owner
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_'
  LOOP
    CASE object_row.relkind
      WHEN 'v' THEN EXECUTE format('ALTER VIEW %s OWNER TO %I', object_row.object_name, new_owner);
      WHEN 'm' THEN EXECUTE format('ALTER MATERIALIZED VIEW %s OWNER TO %I', object_row.object_name, new_owner);
      WHEN 'r', 'p', 'f' THEN EXECUTE format('ALTER TABLE %s OWNER TO %I', object_row.object_name, new_owner);
      ELSE NULL;
    END CASE;
  END LOOP;
  -- Linked identity/serial sequences can only change owner after their table.
  FOR object_row IN
    SELECT c.oid::regclass AS object_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relowner = old_owner AND c.relkind = 'S'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_'
  LOOP
    EXECUTE format('ALTER SEQUENCE %s OWNER TO %I', object_row.object_name, new_owner);
  END LOOP;
  FOR object_row IN
    SELECT p.oid::regprocedure AS object_name
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.proowner = old_owner
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_'
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO %I', object_row.object_name, new_owner);
  END LOOP;
  FOR object_row IN
    SELECT n.nspname, t.typname
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typowner = old_owner
      -- PostgreSQL creates an array companion for every base type.  It is
      -- not independently ALTERable (and regtype renders it as foo[]), but
      -- ALTER TYPE of its owning base/domain/range moves it along with it.
      AND t.typelem = 0
      AND t.typrelid = 0
      -- A multirange is generated with its range and is likewise not
      -- independently ALTERable; ALTER TYPE on the range carries it along.
      AND NOT EXISTS (
        SELECT 1 FROM pg_range r WHERE r.rngmultitypid = t.oid
      )
      -- Extension members are owned by the extension and PostgreSQL rejects
      -- changing them independently.  They are never application objects.
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_type'::regclass
          AND d.objid = t.oid
          AND d.deptype = 'e'
      )
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_'
  LOOP
    -- Use identifier formatting, not regtype text: it is valid for enum,
    -- domain, range and multirange names and cannot turn an array into an
    -- invalid `ALTER TYPE foo[]` statement.
    EXECUTE format('ALTER TYPE %I.%I OWNER TO %I', object_row.nspname, object_row.typname, new_owner);
  END LOOP;
END
$transfer$;
ALTER DATABASE :"DBNAME" OWNER TO :"migration_user";

GRANT CONNECT ON DATABASE :"DBNAME" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
-- M15's operation log is append-only. Provisioning may run again after
-- migrations, so restore its narrower table grant when that table exists.
SELECT format('REVOKE DELETE ON TABLE %I.%I FROM %I', 'public', 'agent_operation', :'app_user')
WHERE to_regclass('public.agent_operation') IS NOT NULL \gexec
-- ``UPDATE`` permits setval(), which lets the runtime role move application
-- counters.  Explicitly revoke it as well as granting the minimum runtime
-- sequence privileges so a rerun converges old provisioned volumes.
REVOKE UPDATE ON ALL SEQUENCES IN SCHEMA public FROM :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public REVOKE UPDATE ON SEQUENCES FROM :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
SQL
