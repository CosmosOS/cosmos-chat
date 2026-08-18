#!/usr/bin/env bash
# Generates .env and synapse/homeserver.yaml with fresh random secrets.
# Both outputs are gitignored. Refuses to overwrite existing files.
set -euo pipefail
cd "$(dirname "$0")/.."

rand() { openssl rand -hex 32; }

if [[ -f .env ]]; then
  echo "ERROR: .env already exists; remove it first if you really want new secrets." >&2
  exit 1
fi
if [[ -f synapse/homeserver.yaml ]]; then
  echo "ERROR: synapse/homeserver.yaml already exists; remove it first if you really want new secrets." >&2
  exit 1
fi

PG_SYNAPSE=$(rand)
PG_BRIDGE=$(rand)
REG_SECRET=$(rand)

sed -e "s/^POSTGRES_SYNAPSE_PASSWORD=.*/POSTGRES_SYNAPSE_PASSWORD=$PG_SYNAPSE/" \
    -e "s/^POSTGRES_BRIDGE_PASSWORD=.*/POSTGRES_BRIDGE_PASSWORD=$PG_BRIDGE/" \
    -e "s/^REGISTRATION_SHARED_SECRET=.*/REGISTRATION_SHARED_SECRET=$REG_SECRET/" \
    .env.example > .env
chmod 600 .env

sed -e "s/__POSTGRES_SYNAPSE_PASSWORD__/$PG_SYNAPSE/" \
    -e "s/__REGISTRATION_SHARED_SECRET__/$REG_SECRET/" \
    -e "s/__MACAROON_SECRET_KEY__/$(rand)/" \
    -e "s/__FORM_SECRET__/$(rand)/" \
    synapse/homeserver.example.yaml > synapse/homeserver.yaml
chmod 600 synapse/homeserver.yaml

echo "Generated: .env, synapse/homeserver.yaml (both gitignored, mode 600)"
echo "NOW EDIT: ACME_EMAIL in .env (Let's Encrypt contact address)"
