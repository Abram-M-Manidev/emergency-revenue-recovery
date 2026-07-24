-- Provisions a separate database for the backend test suite so
-- `docker compose exec api pytest` never touches the dev database. Runs
-- once, only on first initialization of a fresh postgres_data volume (see
-- docker-entrypoint-initdb.d in the official postgres image docs).
-- apps/api/tests/conftest.py always points DATABASE_URL at this database's
-- name (the dev database name plus `_test`), regardless of what
-- DATABASE_URL the api container itself was started with.
CREATE DATABASE errs_test;
