-- Per-database setup. Run against EACH database (quantumedge, quantumedge_test)
-- as superuser, after 01_roles_databases.sql:
--   psql -d quantumedge -f 02_database_setup.sql
--
-- Extensions are created by superuser because qe_owner is deliberately not one.

\set ON_ERROR_STOP on

-- pg_trgm: fuzzy ticker/company-name matching for Layer 3 search and tip parsing.
-- unaccent: Vietnamese diacritic-insensitive lookup ("Hoa Phat" finds "Hòa Phát").
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- The schema belongs to qe_owner; nobody gets implicit rights here.
ALTER SCHEMA public OWNER TO qe_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

GRANT USAGE, CREATE ON SCHEMA public TO qe_owner;

-- qe_web may traverse the schema but is granted NOTHING on any table or sequence.
-- Its only read privilege is on the published_ticker_page view, granted by the
-- migration that creates that view. Default privileges are explicitly left empty
-- so future tables never become readable by accident.
GRANT USAGE ON SCHEMA public TO qe_web;

ALTER DEFAULT PRIVILEGES FOR ROLE qe_owner IN SCHEMA public
  REVOKE ALL ON TABLES FROM qe_web;
ALTER DEFAULT PRIVILEGES FOR ROLE qe_owner IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM qe_web;
