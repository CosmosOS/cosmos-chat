#!/bin/bash
# Runs once on first postgres startup: creates the mautrix-discord DB/user.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE USER mautrix_discord WITH PASSWORD '$BRIDGE_DB_PASSWORD';
    CREATE DATABASE mautrix_discord OWNER mautrix_discord LOCALE 'C' TEMPLATE template0;
EOSQL
