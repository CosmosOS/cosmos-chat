# Roadmap: Cosmos Community Chat Zone

Status tracker for the Matrix/Element chat zone. Architecture and security
details live in [docs/secure-chat-zone.md](docs/secure-chat-zone.md); this
file only tracks what is done and what remains.

_Last updated: 2026-08-15_

## ✅ Done

- [x] Host secured: SSH key-only + no root login, nftables (only 22/80/443
  open), fail2ban, unattended-upgrades, 2 GB swap
- [x] Rootless Docker running as the non-sudo `chat` user (cgroup v2 limits,
  unprivileged ports, slirp4netns so real client IPs are preserved)
- [x] Infomaniak cloud firewall opened for TCP 80/443; verified reachable from
  the internet with real source IPs
- [x] Compose stack deployed and healthy: Postgres, Synapse, Element, bridge
  (Caddy gated behind the `public` compose profile until DNS exists)
- [x] `.well-known` delegation live on gocosmos.org (HTTP 200, JSON
  content-type, CORS header verified)
- [x] Exposure audit: only SSH reachable from the internet; Synapse
  admin/client API loopback-only (SSH tunnel access)
- [x] Admin account created + registration invite token minted
- [x] Discord bridge operational: bot `CosmosMatrixBridge` in CosmosOS (needed
  an unban + temporarily disabling Dyno's account-age Autoban rule, re-enabled
  since), guild bridged with `--entire` (30 text-channel portals + space),
  Discord to Matrix live, Matrix to Discord relay verified in #staff-bot-cmds
- [x] Docker data-root on the 250 GB data disk (`/mnt/data/docker`): media,
  Postgres and images no longer on the 20 GB root disk
- [x] Repo public at github.com/CosmosOS/cosmos-chat (secrets/IPs scrubbed)
- [x] CI/CD: GitHub Actions deploys to the VPS on every push to main
  (restricted SSH key, git reset + compose up + health check)

## 🔴 Blocked: needs action in external panels (Valentin)

- [ ] **Add the DNS records for gocosmos.org**: `matrix` and `chat`, A + AAAA,
  pointing at the VPS (exact records in
  [docs/secure-chat-zone.md](docs/secure-chat-zone.md) §1)

## ⏭️ Next (in order)

- [ ] Once DNS resolves: set `COMPOSE_PROFILES=public` in the VPS `.env`,
  deploy, verify TLS on both domains
- [ ] Validate federation via federationtester.matrix.org for `gocosmos.org`
- [ ] Log in at chat.gocosmos.org (admin account + invite token ready)
- [ ] Enable relay webhooks (`!discord set-relay --create`) in the channels
  that should be two-way (done: #staff-bot-cmds); leave announcement and
  read-only channels one-way
- [ ] Set bridge `public_address` + Caddy route `/mautrix-discord/avatar/*`
  so Matrix avatars show on Discord relay messages
- [ ] Backups: restic (pg_dump + signing key + configs) to off-box storage;
  test a restore
- [ ] Disk usage alert (80 % threshold) + weekly image update routine

## 💤 Later / nice to have

- [ ] Prometheus/Grafana monitoring for Synapse
- [ ] coturn for voice/video calls
