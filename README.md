# Cosmos Community Chat Zone

Self-hosted [Matrix](https://matrix.org) homeserver for the Cosmos community, with
an [Element](https://element.io) web client and a [mautrix-discord](https://docs.mau.fi/bridges/go/discord/index.html)
bridge that mirrors the CosmosOS Discord server, so the community can chat from
Matrix or Discord and everyone sees the same conversations.

Everything runs in **rootless Docker** on a single VPS, hardened following the
OWASP Docker Top 10.

- 📋 Architecture & security plan: [docs/secure-chat-zone.md](docs/secure-chat-zone.md)
- 🗺️ Status & next tasks: [ROADMAP.md](ROADMAP.md)

| Endpoint | Role |
|---|---|
| `https://chat.gocosmos.org` | Element web client |
| `https://matrix.gocosmos.org` | Synapse (client API + federation over 443) |
| `@user:gocosmos.org` | User IDs (delegated via `.well-known` on gocosmos.org) |

## Stack

| Service | Image | Notes |
|---|---|---|
| Caddy | `caddy:2` | TLS termination, routing, security headers; the only service exposed publicly |
| Synapse | `matrixdotorg/synapse` | Matrix homeserver, loopback-only, behind Caddy |
| Element | `vectorim/element-web` | Static web client, separate origin from Synapse |
| PostgreSQL | `postgres:17` | Databases for Synapse and the bridge, internal network only |
| mautrix-discord | `dock.mau.dev/mautrix/discord` | Discord ↔ Matrix bridge (relay mode via webhooks) |
| Onboarding | `python:3.13-slim` | Discord reaction → auto-created Matrix account (`onboarding/onboard.py`) |
| Join page | `python:3.13-slim` | Public signup at `/join` behind a self-hosted ALTCHA captcha (`join/join.py`) |

Security highlights: images pinned by sha256 digest, `cap_drop: ALL`,
`no-new-privileges`, read-only root filesystems, memory/pid limits, an
`internal: true` backend network, captcha-gated registration (see below), and the
Synapse admin API blocked at the reverse proxy (reachable only through an SSH
tunnel to the VPS loopback).

## Deploy

Secrets (`.env`, `synapse/homeserver.yaml`, bridge configs) are **not** in this
repo: they are generated locally and never committed.

```bash
# 1. Generate secrets (writes .env + synapse/homeserver.yaml, both gitignored)
./scripts/gen-secrets.sh
$EDITOR .env                      # set ACME_EMAIL; set COMPOSE_PROFILES=public
                                  # once DNS points at the VPS (starts Caddy)

# 2. Ship the repo to the VPS (a dedicated non-sudo user runs the stack)
scp -r . <vps>:/tmp/cosmos-chat && \
ssh <vps> 'sudo rm -rf /home/chat/cosmos-chat && sudo mv /tmp/cosmos-chat /home/chat/ && sudo chown -R chat:chat /home/chat/cosmos-chat'

# 3. Upload wellknown/* to the gocosmos.org web hosting
#    → must be reachable at https://gocosmos.org/.well-known/matrix/{server,client}

# 4. Start the stack (on the VPS, as the chat user)
cd ~/cosmos-chat
# one-time: synapse runs as 991:991 with all capabilities dropped,
# so the data volume must be pre-owned by that UID
docker run --rm -v cosmos-chat_synapse-data:/data alpine chown -R 991:991 /data
docker compose up -d

# 5. Verify
#    https://federationtester.matrix.org/#gocosmos.org
#    https://chat.gocosmos.org

# 6. Create the first admin account
docker compose exec synapse register_new_matrix_user \
    -c /config/homeserver.yaml -a http://localhost:8008

# 7. Bridge the Discord server → bridge/README.md
```

## Registration

Two doors, both self-hosted, no Google services involved:

- **https://chat.gocosmos.org/join**: the public signup link (shared from
  gocosmos.org). Protected by [ALTCHA](https://altcha.org), a FOSS
  proof-of-work captcha served entirely from our own stack (widget vendored
  in `join/altcha.js`, MIT), plus a honeypot field and per-IP rate limits.
  Accounts are created through Synapse's shared-secret endpoint and
  auto-joined to every bridged channel and the space (same flow as the
  Discord door); visitors already signed in are redirected to the app.
- **Discord reaction**: reacting ✅ on the watched announcement message
  auto-creates a mirrored account (see `onboarding/onboard.py`).

Every other surface is closed: the raw client registration API is disabled
(`enable_registration: false`; both doors above use internal admin endpoints,
which keep working). Element keeps its native UI, but the served app shell is
patched (`scripts/patch-element-index.sh`): every Create account button and
the `#/register` route land on `/join`, and Google's recaptcha hosts are
stripped from the CSP, so the ALTCHA page is the only captcha that can ever
run in a visitor's browser.

## Repo layout

```
compose.yml               # the whole stack (Caddy, Synapse, Postgres, Element, bridge)
caddy/Caddyfile           # TLS, routing, security headers
synapse/                  # homeserver.example.yaml (template) + log.config
element/config.json       # Element web configuration
postgres/                 # first-boot init script (bridge DB)
bridge/                   # mautrix-discord setup guide (configs generated, gitignored)
onboarding/onboard.py     # Discord reaction -> Matrix account daemon
join/                     # public signup page (ALTCHA captcha + vendored widget)
wellknown/                # files served at gocosmos.org/.well-known/matrix/
scripts/gen-secrets.sh    # creates .env + homeserver.yaml with random secrets
docs/secure-chat-zone.md  # architecture & threat-model documentation
```
