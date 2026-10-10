# Roadmap: Cosmos Community Chat Zone

Status tracker for the Matrix/Element chat zone. Architecture and security
details live in [docs/secure-chat-zone.md](docs/secure-chat-zone.md).

_Last updated: 2026-10-06_

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
- [x] **Onboarding daemon** (`onboarding/onboard.py`, hardened container in the
  stack): reacting ✅ on a watched Discord message auto-creates a Matrix
  account with the Discord username, displayname and avatar, auto-joins it to
  every bridged channel + the space (appservice impersonation for invites,
  per-user ratelimit override for the join burst, local `rc_joins` raised),
  and DMs the credentials; verified end-to-end with a real user (42 rooms)
- [x] **Public join page** (`join/join.py` + vendored ALTCHA widget, served at
  https://chat.gocosmos.org/join): one shareable signup link for gocosmos.org,
  bot protection without Google (self-hosted proof-of-work captcha, honeypot,
  per-IP and global rate limits, single-use signed challenges); accounts are
  created via Synapse's shared-secret endpoint while the raw client API stays
  invite-token gated
- [x] **Register attack surface closed**: raw client registration API disabled
  (`enable_registration: false`; the join page and onboarding daemon use
  internal admin endpoints and keep working), Element's `#/register` screen
  and Create account buttons removed (`UIFeature.registration: false`),
  custom Cosmos welcome screen whose Create account button links to /join,
  and /join redirects already-signed-in visitors to the app
- [x] CI deploy refreshes bind-mounted configs: recreates the light services
  (element, join, onboarding) and gracefully reloads Caddy on every push, so
  config-only changes actually reach the running containers
- [x] Native Element welcome restored (the sanitized embedded welcome page
  rendered unstyled: Element 1.12 strips style blocks and classes and forces
  links into new tabs, so custom embedded pages cannot look native)
- [x] **App shell patched at the proxy** (`scripts/patch-element-index.sh`,
  regenerated from the running image on every deploy): Element's native
  Create account buttons and the `#/register` route now land on the ALTCHA
  /join page, and Google's recaptcha hosts are stripped from Element's CSP,
  so the browser itself refuses those origins and ALTCHA is provably the
  only captcha in the flow
- [x] /join page restyled with Element's compound design tokens (glass panel,
  pill buttons, dark palette) so signup matches the app pixel for pixel
- [x] /join accounts auto-joined to every bridged channel + the space, same
  as the Discord reaction flow (bridge-bot invites + accepted invites with
  the per-user ratelimit lifted, run in the background so signup stays
  instant; the registration session is logged out afterwards)
- [x] **Auto-join respects Discord permissions**: room eligibility is computed
  with Discord's permission algorithm (role perms + channel overwrites) via
  the bridge info state's channel id. /join accounts get only what @everyone
  sees; Discord-reaction onboarding computes the reacting member's own
  visibility, so staff get staff rooms and regular members do not. The
  test12345 account was kicked from #staff-bot-cmds and the staff only
  space (gabolate and soultron keep them: their Discord roles grant access)
- [x] **Matrix to Discord relay on every writable channel**: relay webhooks
  created (`!discord set-relay --create` sent as @cosmosbridge) in the 19
  portals whose Discord channel grants @everyone SEND_MESSAGES, so Matrix
  messages now appear on Discord under the sender's Matrix name; verified
  end to end in #dev-tests (message relayed, Matrix redaction deleted the
  Discord copy too), setup command noise redacted from all portals.
  Read-only and announcement channels (rules, welcome, help, projects,
  projects-archive, refactor-discussion, website, nativeaot-git,
  cosmos-announcements, github-activity) intentionally stay one-way:
  a relay webhook there would let Matrix users bypass Discord's send
  restrictions, since webhooks can post regardless of channel permissions

- [x] CosmosOS space categories ordered for all members (an `order` field on
  each `m.space.child` state event in the guild space, set as the bridge
  bot): Info, Off Topic, Cosmos, Cosmos Development, Gen3 Development,
  Voice Channels, staff only, Archived. Room state, so current and future
  members all see the same sidebar order

- [x] #cosmos-general relay enabled: the channel's @everyone overwrite
  denied Manage Webhooks, an allow overwrite for the `cosmos.bridge` role
  was added on Discord, then `!discord set-relay --create` succeeded, so
  every writable channel is now two-way

- [x] **#rules and #cosmos-announcements read-only on Matrix, like Discord**:
  portal power levels raised to `events_default: 50` (posting needs PL 50)
  while reactions and own-message redactions stay at 0, so everyone reads
  and reacts but only staff post. PL 50 granted to the admin/coredev
  members' Discord puppets (their Discord posts keep bridging in), to
  their existing Matrix accounts (valentinbreiz, soultron17, zarlo), to
  the Dyno bot's puppet and to @cosmosbridge. Since posting is now
  staff-gated on Matrix too, the webhook bypass concern no longer applies
  and both channels got relay webhooks (needed a Manage Webhooks allow
  for the cosmos.bridge role on #rules, same fix as #cosmos-general), so
  staff announcements posted from Matrix reach Discord. Verified with a
  regular account: posting rejected, reacting and removing the reaction
  allowed. Note: staff who get a Matrix account or the role later need a
  PL 50 grant in these two rooms, it is not automatic

- [x] Mobile browsers stay on the web app: Element hard-redirects mobile
  user agents to /mobile_guide ("install Element X") and ignores
  `mobile_guide_toast: false` there (open bug element-web#21616), so Caddy
  now 302s /mobile_guide* back to /#/welcome (Element skips the mobile
  redirect when a fragment is present, so no loop) and the toast is
  disabled in config.json

- [x] Custom mobile guide (`caddy/assets/mobile.html` served at
  /mobile_guide, replacing the earlier plain redirect): mobile visitors get
  a Cosmos-styled choice page recommending Element X and FluffyChat (App
  Store, Google Play and F-Droid links, homeserver gocosmos.org spelled
  out, badge SVGs reused from Element's own assets so nothing external
  loads) plus a "Use the web app anyway" button that sets Element's
  skip_mobile_redirect sessionStorage flag and lands on /#/welcome, and a
  link to /join for people without an account

- [x] Gen3 Development space deleted (2026-10-06): the Discord category was
  gone (its #nativeaot-dev and #nativeaot-git channels moved to Archived,
  the bridge had already dropped its portal), so the orphaned Matrix space
  was unlinked from the CosmosOS space as the bridge bot and shut down +
  purged through the admin API (29 members kicked, no aliases). The
  CosmosOS space now lists 7 categories

- [x] **Matrix invite announced to the whole Discord** (2026-10-06): an
  @everyone post in #cosmos-announcements, written in the admin's
  announcement style, invites members to Matrix in response to Discord's
  global age verification rollout (face scan or ID for some accounts, after
  the 2025 vendor leak of ~70k ID photos). Members react ✅ on the post
  itself (added to `ONBOARD_WATCH`) or use /join; the post also names
  Element X / FluffyChat with homeserver gocosmos.org and asks members to
  open their server DMs. Posted by the onboarding bot, which needed a
  channel overwrite on its `Bot` role (Send Messages, Mention @everyone,
  Manage Messages). A fresh 🧪 onboarding test message was also posted in
  #staff-bot-cmds and watched

- [x] **Onboarding no longer strands members with closed DMs**: a member
  whose Discord DMs were closed got a Matrix account (created and joined
  to 39 rooms) whose password could not be delivered, and the failure was
  recorded as final. The daemon now sends a first DM before creating
  anything; if it bounces, the member's ✅ is removed so they can open DMs
  and react again. Accounts the daemon created whose credentials never
  arrived get a fresh password on the next attempt (devices logged out),
  while existing accounts it did not create are never touched (same name
  is not same person). Other failures retry after a 10 minute back-off
  instead of being final. The stranded member's state entry was marked as
  daemon-created and they received their login on the next run

- [x] **Open to accounts from other Matrix servers, moderated by Draupnir**
  (2026-10-06): federation already worked (federation tester green) but
  matrix.org users got `403 You are not invited` on the CosmosOS space,
  since every bridged room and space was invite-only, and chat.gocosmos.org
  only signs in local accounts (`disable_custom_urls`). Now the CosmosOS
  space is public with the address `#cosmos:gocosmos.org`; the 38 channel
  and category rooms @everyone sees on Discord are "space members can join";
  #staff-bot-cmds and the staff only space stay invite-only. The bridge's
  own `restricted_rooms` option was left off: it would also open private
  channels. Instead the onboarding daemon (`sync_access`) recomputes
  @everyone's Discord permissions every 10 minutes, closes a room whose
  channel turns private, and opens a room only after Draupnir is its admin
  and member (access token minted through the admin login API per run, not
  stored). A room someone locked to invite-only stays locked.
  Draupnir v3.1.0 runs behind the `moderation` compose profile (token in
  `secrets/`, one-time `scripts/setup-draupnir.sh`), protects every room it
  is in (58 at launch, forum post rooms included), shows as Terminator,
  and takes commands in
  #staff-bot-cmds, from Matrix or from Discord through the bridge, where
  invites are now admin-only. On: the Community Moderation Effort ban list
  (13k users, 101 servers), our own `#cosmos-bans` list, raid lockdown of
  the space at 50 joins/hour (it only acts on public rooms, hence the
  space as the single public door), mention limit 10. Off on purpose:
  flood and first-message-image protections, which cannot exempt the
  bridge's Discord users. Redactions of relayed messages delete them on
  Discord too. The forum mirror invites remote `!post` authors (the admin
  join API is local-only) and no longer mistakes remote `@discord_*` users
  for bridge ghosts. `@valentinbreiz` is admin of the space, to reopen it
  after a raid lockdown

- [x] **Arrivals in #welcome only, like Discord** (2026-10-06): every
  channel was cluttered with "was invited / joined / changed their profile
  picture" lines. Membership events cannot be moved out of a room (Matrix
  needs them for permissions), so: chat.gocosmos.org now hides join/leave,
  avatar and display name changes by default (`setting_defaults` in
  `element/config.json`, per member override still possible; other apps
  keep their own settings); the onboarding daemon and the /join page join
  new accounts to the guild space first, then to the open channels
  directly, so no invite event appears (only invite-only staff rooms, or
  rooms Draupnir locked, still get one); and the onboarding daemon greets
  each new member of the space ("👋 Welcome X (from matrix.org) to
  Cosmos!", no ping) in #welcome (fallback: the room of Discord's system
  channel, which is #off-topic). The greeting is posted by the bridge
  bot, which the bridge never relays, so Discord's own join message is not
  doubled. The first run records existing members without greeting them

- [x] **File downloads fixed in Element** (2026-10-09): clicking download
  did nothing although Synapse served every file (200). Element saves files
  through a sandboxed iframe of its own `/usercontent/` page, and
  chat.gocosmos.org sent `X-Frame-Options: DENY` / `frame-ancestors 'none'`
  on every path. `/usercontent/*` now allows same-origin framing only;
  every other path still refuses all framing

- [x] **DMs no longer follow you into every CosmosOS category**
  (2026-10-10): Element lists your DMs with a space's members inside that
  space ("People" section, `Spaces.showPeopleInSpace`), and everyone is in
  every category. It is a per-account, per-space room account data setting
  (`im.vector.web.settings`) that `setting_defaults` cannot reach, so it
  was turned off on the 8 bridged spaces for the 47 existing local
  accounts (334 settings, one-time pass through admin login tokens,
  nobody had chosen a value yet), and the onboarding daemon and /join page
  now turn it off for each new account. Accounts from other servers keep
  Element's default; anyone can turn it back on in the space's Preferences

- [x] **Discord-style forums in Element** (2026-10-10): #cosmos-general got
  every kind of conversation, partly because Matrix users had no help
  forum: only #cosmos-projects was mirrored. Element has no forum room type
  (a spec idea, matrix-spec#2321; only the Sable client renders one), and
  the bridge can only reply in Discord threads from relay mode, not start
  them. So: #cosmos-help and #other-projects are now mirrored too (same
  index room + one room per post, 60 days imported), and an Element plugin
  (`element/modules/cosmos-forum.js`, runtime module API, no fork) shows a
  forum room as a forum, in place of its timeline and next to the room
  list: all 1,585 Discord posts with search, tag filters, sorting and a
  New post form. Older posts are brought over (opening message + last 100
  replies) the first time someone opens one, instead of 1,585 rooms up
  front. Post rooms get a back button, forum rooms a Chat view toggle. The
  forum mirror catalogs every post (hourly rescan, opening messages
  fetched once for excerpts) and serves it at `/forum-api/`, authenticated
  with Matrix OpenID tokens. Element waits for plugins before starting, so
  every hook is isolated: the API down or a renamed Element element only
  brings back the timeline of cards. Tested end to end against Element
  1.12.25 in Chromium (39 checks) before deploy. The pinned how-to and
  topic of the index rooms now say "!post Your title" for every forum

## ⏭️ Next (in order)

- [ ] Put the signup link (https://chat.gocosmos.org/join) on gocosmos.org
- [ ] Backups: restic (pg_dump + signing key + configs) to off-box storage;
  test a restore
- [ ] Disk usage alert (80 % threshold) + weekly image update routine

## 💤 Later / nice to have

- [ ] AAAA records for matrix/chat (only A records exist; the VPS has IPv6)
- [ ] Prometheus/Grafana monitoring for Synapse
- [ ] coturn for voice/video calls
