# Draupnir: moderation for the rooms open to other servers

[Draupnir](https://github.com/the-draupnir-project/Draupnir) protects every
room it is in. The onboarding daemon (`sync_access` in
`onboarding/onboard.py`) makes it admin of each room and joins it there
before that room opens to Matrix accounts from other servers.

## Setup (once, on the VPS as the chat user)

```bash
cd ~/cosmos-chat
# @draupnir account + token in secrets/, #moderation:gocosmos.org management
# room, #cosmos:gocosmos.org address on the guild space, the moderator as
# admin of both
./scripts/setup-draupnir.sh @valentinbreiz:gocosmos.org
# run it: add "moderation" to COMPOSE_PROFILES in .env
docker compose up -d draupnir
```

Accept the invite to **Draupnir moderation** and configure it there:

```
!draupnir list create cosmos cosmos-bans
!draupnir watch #community-moderation-effort-bl:neko.dev --no-confirm
!draupnir protections enable JoinWaveShortCircuitProtection
!draupnir protections enable MentionLimitProtection
!draupnir protections config set MentionLimitProtection maxMentions 10
```

Then open the rooms: set `DRAUPNIR_MXID=@draupnir:gocosmos.org` in `.env`
and `docker compose up -d --force-recreate onboarding`.

Config edits (`production.yaml`) need `docker compose up -d --force-recreate
draupnir`: deploys leave Draupnir running.

## What is on

- **Community Moderation Effort ban list**: known spammers and scammers are
  banned (and their servers blocked) in every room before they act.
- **Raid lockdown** (`JoinWaveShortCircuitProtection`): 50 joins in an hour
  switch the space to invite-only. It only acts on public rooms, which is
  why the space is the single public door and the channels are "space
  members can join".
- **Mention limit**: messages pinging more than 10 people are removed.
- **Our own ban list** (`#cosmos-bans:gocosmos.org`): a ban added to it
  applies to every protected room, and a manual ban in one room offers to
  add it there.

Off on purpose: `BasicFloodingProtection` and `FirstMessageIsImageProtection`
cannot exempt anyone, so they would ban the bridge's Discord users (a Discord
member whose first bridged message is an image, a fast burst of attachments).

## Everyday use (in the management room)

```
!draupnir ban @spammer:example.org cosmos spam   # every protected room
!draupnir ban evil.example cosmos spam           # a whole server
!draupnir redact @spammer:example.org            # remove their recent messages
!draupnir unban @someone:example.org
!draupnir status
```

Redacted messages relayed to Discord are deleted there too.

**After a raid lockdown** the space stays invite-only (the onboarding sync
never reopens a room someone locked). Reopen it once the raid is over:
CosmosOS space > Settings > Visibility > Public.
