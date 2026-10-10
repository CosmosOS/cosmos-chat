# Draupnir: moderation for the rooms open to other servers

[Draupnir](https://github.com/the-draupnir-project/Draupnir) protects every
room it is in. It shows as **Terminator** (`@draupnir:gocosmos.org`); its
protection settings live as state events in its management room, so they
must be set again if that room ever changes. The onboarding daemon
(`sync_access` in `onboarding/onboard.py`) makes it admin of each room and
joins it there before that room opens to Matrix accounts from other servers.

## Setup (once, on the VPS as the chat user)

```bash
cd ~/cosmos-chat
# @draupnir account + token in secrets/, admin of its management room
# (#staff-bot-cmds, where only admins may invite from now on: every member
# can command it), #cosmos:gocosmos.org address on the guild space with the
# moderator as its admin
./scripts/setup-draupnir.sh @valentinbreiz:gocosmos.org '!SCclVdOnANJSSBTzmO:gocosmos.org'
# run it: add "moderation" to COMPOSE_PROFILES in .env
docker compose up -d draupnir
```

Configure it from #staff-bot-cmds (Matrix, or Discord through the bridge):

```
!draupnir list create cosmos cosmos-bans
!draupnir watch #community-moderation-effort-bl:neko.dev --no-confirm
!draupnir protections enable JoinWaveShortCircuitProtection
!draupnir protections enable MentionLimitProtection
!draupnir protections config set MentionLimitProtection maxMentions 10
```

Run the `watch` alone and wait for its ✅: joining that big room over
federation can take a few minutes. If it times out the room ends up
*protected* instead of watched (Draupnir protects every room it joins), and
Draupnir tries to ban people inside the list room itself. Then run the
`watch` again, `!draupnir rooms remove` the list (Draupnir leaves it but
keeps it watched) and `watch` it once more to rejoin.

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
- **Policy change notifications** (`PolicyChangeNotification`): every
  change to a watched list ("cme-bans updated with 1 change: ...", about 3
  a day) is posted in #staff-bot-cmds, so on Discord too. Left unset,
  Draupnir creates a room of its own for them; the room is set with
  `!draupnir protections config set PolicyChangeNotification
  notificationRoomID "!SCclVdOnANJSSBTzmO:gocosmos.org"` (quoted: a bare
  room ID is refused with "Expected union value").

Off on purpose: `BasicFloodingProtection` and `FirstMessageIsImageProtection`
cannot exempt anyone, so they would ban the bridge's Discord users (a Discord
member whose first bridged message is an image, a fast burst of attachments).

## Everyday use (in #staff-bot-cmds, on Matrix or Discord)

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
