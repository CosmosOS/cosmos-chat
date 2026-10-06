#!/bin/bash
# One-time Draupnir setup, run on the VPS as the chat user from
# ~/cosmos-chat (see draupnir/README.md):
#   ./scripts/setup-draupnir.sh @moderator:gocosmos.org
# 1. registers @draupnir (not a server admin) and writes its access token to
#    secrets/draupnir-access-token, lifts its ratelimit (mass bans),
# 2. creates its management room #moderation:gocosmos.org (invite-only,
#    unencrypted) and invites the moderator as room admin,
# 3. gives the guild space the address #cosmos:gocosmos.org and makes the
#    moderator its admin, so they can reopen it after Draupnir's raid
#    protection locked it.
# Each step is skipped when already done.
set -e
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
python3 - "${1:?usage: $0 @moderator:gocosmos.org}" <<'PY'
import hashlib, hmac, json, os, secrets, sys, urllib.error, urllib.request
from urllib.parse import quote

H = "http://127.0.0.1:8008"
DOMAIN = "gocosmos.org"
ADMIN, AS = os.environ["ONBOARD_ADMIN_TOKEN"], os.environ["BRIDGE_AS_TOKEN"]
BOT = f"@discordbot:{DOMAIN}"
MXID = f"@draupnir:{DOMAIN}"
TOKEN_FILE = "secrets/draupnir-access-token"
moderator = sys.argv[1]


def call(path, method="GET", body=None, token=ADMIN, as_bot=False):
    if as_bot:
        path += ("&" if "?" in path else "?") + "user_id=" + quote(BOT, safe="")
    req = urllib.request.Request(H + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60) as r:
        p = r.read()
        return json.loads(p) if p else {}


def q(v):
    return quote(v, safe="")


# 1. account and token
if os.path.exists(TOKEN_FILE):
    token = open(TOKEN_FILE).read().strip()
    print("token file exists, account", call("/_matrix/client/v3/account/whoami", token=token)["user_id"])
else:
    nonce = call("/_synapse/admin/v1/register")["nonce"]
    password = secrets.token_urlsafe(32)  # never used: the token is enough
    mac = hmac.new(os.environ["REGISTRATION_SHARED_SECRET"].encode(),
                   b"\x00".join([nonce.encode(), b"draupnir", password.encode(), b"notadmin"]),
                   hashlib.sha1).hexdigest()
    token = call("/_synapse/admin/v1/register", "POST",
                 {"nonce": nonce, "username": "draupnir", "password": password,
                  "displayname": "Draupnir", "admin": False, "mac": mac})["access_token"]
    os.makedirs("secrets", exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    print("registered", MXID, "token written to", TOKEN_FILE)
call(f"/_synapse/admin/v1/users/{q(MXID)}/override_ratelimit", "POST",
     {"messages_per_second": 0, "burst_count": 0})
print("ratelimit lifted")

# 2. management room
alias = f"#moderation:{DOMAIN}"
try:
    room = call(f"/_matrix/client/v3/directory/room/{q(alias)}")["room_id"]
    print("management room exists:", room)
except urllib.error.HTTPError as e:
    if e.code != 404:
        raise
    room = call("/_matrix/client/v3/createRoom", "POST", {
        "preset": "private_chat",
        "room_alias_name": "moderation",
        "name": "Draupnir moderation",
        "topic": "Commands for Draupnir, the moderation bot of the Cosmos rooms. "
                 "Everyone here can command it: invite trusted moderators only. Type !draupnir help",
        "invite": [moderator],
        "power_level_content_override": {"users": {MXID: 100, moderator: 100}},
        "initial_state": [{"type": "m.room.guest_access", "state_key": "",
                           "content": {"guest_access": "forbidden"}}],
    }, token)["room_id"]
    print("management room created:", room, alias, "- invited", moderator)

# 3. guild space address and admin
guild = os.environ["GUILD_ID"]
space = None
for r in call("/_matrix/client/v3/joined_rooms", token=AS, as_bot=True)["joined_rooms"]:
    state = call(f"/_matrix/client/v3/rooms/{q(r)}/state", token=AS, as_bot=True)
    create = next(ev["content"] for ev in state if ev["type"] == "m.room.create")
    cids = {ev["content"].get("channel", {}).get("id") for ev in state if ev["type"] == "m.bridge"}
    if create.get("type") == "m.space" and guild in cids:
        space = r
        break
assert space, "guild space not found"
space_alias = f"#cosmos:{DOMAIN}"
try:
    call(f"/_matrix/client/v3/directory/room/{q(space_alias)}", "PUT",
         {"room_id": space}, token=AS, as_bot=True)
except urllib.error.HTTPError as e:
    if e.code != 409:  # alias already exists
        raise
call(f"/_matrix/client/v3/rooms/{q(space)}/state/m.room.canonical_alias", "PUT",
     {"alias": space_alias}, token=AS, as_bot=True)
pl = call(f"/_matrix/client/v3/rooms/{q(space)}/state/m.room.power_levels", token=AS, as_bot=True)
if pl.get("users", {}).get(moderator, 0) < 100:
    pl.setdefault("users", {})[moderator] = 100
    call(f"/_matrix/client/v3/rooms/{q(space)}/state/m.room.power_levels", "PUT", pl,
         token=AS, as_bot=True)
print("guild space", space, "is", space_alias, "with", moderator, "as admin")
PY
