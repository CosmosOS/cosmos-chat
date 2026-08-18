# Roadmap: Cosmos Community Chat Zone

Status tracker for the Matrix/Element chat zone. Architecture and security
details live in [docs/secure-chat-zone.md](docs/secure-chat-zone.md).

_Last updated: 2026-08-18_

## ✅ Done

- [x] VPS provisioned (Infomaniak, Debian 13, 4 vCPU / 11 GB / 20 GB)
- [x] SSH keys installed locally + `~/.ssh/config` alias for the VPS
- [x] SSH key copies removed from this repo
- [x] Architecture & security plan written (`docs/secure-chat-zone.md`)
- [x] System updated (apt full-upgrade)
- [x] 2 GB swapfile + `vm.swappiness=10`
- [x] SSH hardened: key-only, no root login, `AllowUsers debian chat`, max 3 auth tries
- [x] nftables firewall: default-deny inbound, only 22/80/443 open, enabled at boot
- [x] fail2ban: sshd jail (systemd backend, aggressive mode, 1 h bans)
- [x] unattended-upgrades enabled (automatic Debian security updates)
- [x] `chat` user created (non-sudo, lingering enabled)
- [x] Docker CE 29.7.2 installed; **rootful daemon disabled**
- [x] Rootless Docker running as `chat` (cgroup v2: cpu/memory/pids delegated)
- [x] `net.ipv4.ip_unprivileged_port_start=80` (rootless can bind 80/443)
- [x] slirp4netns port driver (real client IPs preserved); verified with a test container
- [x] Infomaniak cloud firewall: TCP 80 + 443 opened; verified reachable from the internet with real source IPs
- [x] Compose stack scaffolded in this repo: Caddy, Synapse, PostgreSQL, Element, mautrix-discord (+ hardening per plan §5)
- [x] Secrets tooling: `scripts/gen-secrets.sh`, `.env.example`, `.gitignore`
- [x] `.well-known` files + `.htaccess` prepared in `wellknown/`
- [x] Deploy instructions in `README.md`, bridge guide in `bridge/README.md`
- [x] Secrets generated (`.env` + `synapse/homeserver.yaml`, ACME email set)
- [x] Repo deployed to VPS at `/home/chat/cosmos-chat`
- [x] Images pulled and pinned by sha256 digest in `compose.yml`
- [x] Postgres + Synapse + Element running and **healthy** on the VPS
  (signing key generated, DB initialized; Caddy intentionally not started, needs DNS for TLS)
- [x] `.well-known` files live on gocosmos.org (uploaded via hosting file manager to
  `public_html/.well-known/matrix/`); verified: HTTP 200, JSON content-type, CORS header
- [x] External exposure audit: only SSH reachable from the internet; Synapse admin/client API
  bound to VPS loopback only (SSH tunnel access)
- [x] Homeserver admin account created + registration invite token minted
- [x] mautrix-discord configured (config + registration generated, DB initialized) and
  registered with Synapse; bridge container running, `@discordbot:gocosmos.org` alive
- [x] Discord bot `CosmosMatrixBridge` created, intents enabled, added to CosmosOS
  (after unban + temporarily disabling Dyno's account-age Autoban rule)
- [x] **CosmosOS guild bridged** (`guilds bridge --entire`): 30 text-channel portals +
  space/categories; Discord→Matrix live
- [x] Matrix→Discord relay verified in #staff-bot-cmds (`!discord set-relay --create`
  + test message delivered); relay webhook needed per channel for two-way
- [x] Docker data-root moved to the 250 GB data disk (`/mnt/data/docker`, persistent
  fstab mount); media store, Postgres and images no longer on the 20 GB root disk
- [x] Repo published to github.com/CosmosOS/cosmos-chat (secrets/IPs scrubbed)
- [x] CI/CD: GitHub Actions deploys on every push to main (SSH as unprivileged user,
  git reset + compose up + health check); Caddy gated behind the `public` compose
  profile until DNS exists
- [x] Dyno's Autoban module re-enabled on CosmosOS
- [x] DNS A records for matrix.gocosmos.org and chat.gocosmos.org live
  (verified on the public resolvers and the authoritative a2dns servers)
- [x] **Caddy live**: `COMPOSE_PROFILES=public` set in the VPS `.env`,
  Let's Encrypt certificates obtained for both domains (needed
  `cap_add: NET_BIND_SERVICE`; the caddy binary's file capabilities make
  exec fail under no-new-privileges + cap_drop ALL)
- [x] TLS verified from outside: Element 200, Synapse client API 200,
  `/_synapse/admin` blocked with 403, `.well-known` served
- [x] **Federation validated**: federationtester.matrix.org reports
  AllChecksOK + valid certificates for `gocosmos.org`
- [x] Relay identity fixed: bot login moved to a dedicated `@cosmosbridge`
  Matrix account (per mautrix docs), admin account logged out of the bridge;
  Matrix messages now go through the channel relay webhook and show the
  sender's Matrix displayname on Discord (verified in #staff-bot-cmds)
- [x] Avatar proxy wired up: bridge `public_address` set to
  matrix.gocosmos.org + Caddy route `/mautrix-discord/*` to the bridge
- [x] Admin logged in via Element at chat.gocosmos.org
- [x] Discord avatar mirrored onto the admin's Matrix profile; relay messages
  on Discord now show the sender's name and picture (no automatic
  username-matching in mautrix: dual-account users either set a Matrix
  avatar or `login-qr` with their own Discord account for native identity)

## ⏭️ Next (in order)

- [ ] Relay webhooks for more channels when wanted (`!discord set-relay
  --create` per portal; only #staff-bot-cmds is two-way for now); keep
  announcement and read-only channels one-way
- [ ] Backups: restic (pg_dump + signing key + configs) to off-box storage;
  test a restore
- [ ] Disk usage alert (80 % threshold) + weekly image update routine

## 💤 Later / nice to have

- [ ] AAAA records for matrix/chat (only A records exist; the VPS has IPv6)
- [ ] Prometheus/Grafana monitoring for Synapse
- [ ] coturn for voice/video calls
