# Roadmap — Cosmos Community Chat Zone

Tracks the rollout of the Matrix/Element chat zone (see
[docs/secure-chat-zone.md](docs/secure-chat-zone.md) for the full plan).

_Last updated: 2026-08-15_

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
- [x] slirp4netns port driver (real client IPs preserved) — verified with test container
- [x] Infomaniak cloud firewall: TCP 80 + 443 opened — verified reachable from the internet with real source IPs
- [x] Compose stack scaffolded in this repo: Caddy, Synapse, PostgreSQL, Element, mautrix-discord (+ hardening per plan §6)
- [x] Secrets tooling: `scripts/gen-secrets.sh`, `.env.example`, `.gitignore`
- [x] `.well-known` files + `.htaccess` prepared in `wellknown/`
- [x] Deploy instructions in `README.md`, bridge guide in `bridge/README.md`
- [x] Secrets generated (`.env` + `synapse/homeserver.yaml`, ACME email set)
- [x] Repo deployed to VPS at `/home/chat/cosmos-chat`
- [x] Images pulled and pinned by sha256 digest in `compose.yml`
- [x] Postgres + Synapse + Element running and **healthy** on the VPS
  (signing key generated, DB initialized; Caddy intentionally not started — needs DNS for TLS)
- [x] `.well-known` files live on gocosmos.org (uploaded via hosting file manager to
  `public_html/.well-known/matrix/`) — verified: HTTP 200, JSON content-type, CORS header
- [x] External exposure audit: only SSH reachable from the internet; Synapse admin/client API
  bound to VPS loopback only (SSH tunnel access)
- [x] Homeserver admin account created + registration invite token minted
- [x] mautrix-discord configured (config + registration generated, DB initialized) and
  registered with Synapse — bridge container running, `@discordbot:gocosmos.org` alive
- [x] Discord bot `CosmosMatrixBridge` created, intents enabled, added to CosmosOS
  (after unban + disabling Dyno's account-age Autoban rule — re-enable it!)
- [x] **CosmosOS guild bridged** (`guilds bridge --entire`): 30 text-channel portals +
  space/categories; Discord→Matrix live
- [x] Matrix→Discord relay verified in #staff-bot-cmds (`!discord set-relay --create`
  + test message delivered) — relay webhook needed per channel for two-way
- [x] Docker data-root moved to the 250 GB data disk (`/mnt/data/docker`, persistent
  fstab mount) — media store, Postgres and images no longer on the 20 GB root disk

## 🔴 Blocked — needs action in external panels (Valentin)

- [ ] **Add DNS records for gocosmos.org:**
  ```
  matrix.gocosmos.org   A     <VPS_IPV4>
  matrix.gocosmos.org   AAAA  <VPS_IPV6>
  chat.gocosmos.org     A     <VPS_IPV4>
  chat.gocosmos.org     AAAA  <VPS_IPV6>
  ```
- [ ] **Re-enable Dyno's Autoban module** on CosmosOS (was disabled to let the bot join)

## ⏭️ Next (in order)

- [ ] Once DNS resolves: `docker compose up -d caddy`; verify TLS on both domains
- [ ] Validate federation via federationtester.matrix.org for `gocosmos.org`
- [ ] Log in at chat.gocosmos.org (admin account + invite token already created via SSH tunnel)
- [ ] Enable relay webhooks (`!discord set-relay --create`) in the channels that should
  be two-way (done: #staff-bot-cmds); decide whether announcement/read-only channels
  stay Discord→Matrix only
- [ ] Post-DNS: set bridge `public_address` + Caddy route `/mautrix-discord/avatar/*`
  so Matrix avatars show on Discord relay messages
- [ ] Backups: restic (pg_dump + signing key + configs) to off-box storage; test restore
- [ ] Disk usage alert (80 % threshold) + weekly image update routine

## 💤 Later / nice to have

- [ ] Prometheus/Grafana monitoring for Synapse
- [ ] coturn for voice/video calls
