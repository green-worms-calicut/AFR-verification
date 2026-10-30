#!/bin/bash
# Run once by the Postgres image when its volume is new: the app's own login, owning the verifier database, so the
# web receiver and the worker don't connect as the superuser. VERIFIER_DB_PASSWORD is hex (openssl rand -hex 32).
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE verifier_app LOGIN PASSWORD '${VERIFIER_DB_PASSWORD}';
ALTER DATABASE ${POSTGRES_DB} OWNER TO verifier_app;
ALTER SCHEMA public OWNER TO verifier_app;
SQL
