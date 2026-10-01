-- Roles and databases. Run ONCE against the `postgres` database as superuser.
--
-- Passwords are passed as psql variables, never written here:
--   psql -v owner_pw='...' -v web_pw='...' -f 01_roles_databases.sql
--
-- Two roles, deliberately asymmetric:
--   qe_owner  owns every object; the Python pipeline connects as this.
--   qe_web    reads ONE view and holds no privilege on any base table. The Next.js
--             app connects as this, so a forgotten filter cannot leak an unpublished
--             draft - it raises a permission error instead.
-- qe_owner is intentionally NOT a superuser: that mirrors what a managed Postgres
-- gives you, so nothing here depends on privileges we would lose on migration.
--
-- Note: role creation uses SELECT ... \gexec rather than a DO block, because psql
-- does not interpolate :'variables' inside dollar-quoted strings.

\set ON_ERROR_STOP on

-- Create roles if absent, then set the password unconditionally (idempotent re-run).
SELECT format('CREATE ROLE qe_owner LOGIN CREATEDB PASSWORD %L', :'owner_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qe_owner')
\gexec

SELECT format('ALTER ROLE qe_owner PASSWORD %L', :'owner_pw')
\gexec

SELECT format('CREATE ROLE qe_web LOGIN PASSWORD %L', :'web_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qe_web')
\gexec

SELECT format('ALTER ROLE qe_web PASSWORD %L', :'web_pw')
\gexec

-- Databases
SELECT 'CREATE DATABASE quantumedge OWNER qe_owner ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'quantumedge')
\gexec

SELECT 'CREATE DATABASE quantumedge_test OWNER qe_owner ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'quantumedge_test')
\gexec

-- No implicit access for anyone.
REVOKE ALL ON DATABASE quantumedge      FROM PUBLIC;
REVOKE ALL ON DATABASE quantumedge_test FROM PUBLIC;
GRANT CONNECT ON DATABASE quantumedge      TO qe_owner, qe_web;
GRANT CONNECT ON DATABASE quantumedge_test TO qe_owner;

\echo 'roles and databases ready'
