-- Runs ONCE, automatically, the first time the Postgres container starts with an empty data volume
-- (the image runs every *.sql file in /docker-entrypoint-initdb.d, as the superuser).
-- To start over: docker rm -f household-db; docker volume rm household-db-data; start the container again.

-- 1. The app's own login. NOT a superuser: superusers ignore row-level security,
--    and taste_mcp.py should have no more power than it needs.
CREATE ROLE taste_app LOGIN PASSWORD 'taste-dev-pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- 2. Real data.
\connect household
\i /docker-entrypoint-initdb.d/schema.psql
\i /docker-entrypoint-initdb.d/household.psql

-- 3. A separate database for the tests, so they never touch your real history.
CREATE DATABASE household_test;
\connect household_test
\i /docker-entrypoint-initdb.d/schema.psql
\i /docker-entrypoint-initdb.d/household.psql