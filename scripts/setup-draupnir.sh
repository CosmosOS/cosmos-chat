#!/bin/bash
# One-time Draupnir setup, run on the VPS as the chat user from
# ~/cosmos-chat (see draupnir/README.md):
#   ./scripts/setup-draupnir.sh @moderator:gocosmos.org '!managementroom:gocosmos.org'
# 1. registers @draupnir (not a server admin) and writes its access token to
#    secrets/draupnir-access-token, lifts its ratelimit (mass bans),
# 2. joins it as admin to its management room (managementRoom in
#    draupnir/production.yaml, an unencrypted bridged staff channel), where
#    only admins may invite from now on: every member can command Draupnir,
# 3. gives the guild space the address #cosmos:gocosmos.org and makes the
#    moderator its admin, so they can reopen it after Draupnir's raid
#    protection locked it.
# Each step is skipped when already done.
set -e
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
python3 - "${1:?usage: $0 @moderator:gocosmos.org '!managementroom:gocosmos.org'}" "${2:?management room id}" <<'PY'
import hashlib, hmac, json, os, secrets, sys, urllib.error, urllib.request
from urllib.parse import quote

H = "http://127.0.0.1:8008"
DOMAIN = "gocosmos.org"
ADMIN, AS = os.environ["ONBOARD_ADMIN_TOKEN"], os.environ["BRIDGE_AS_TOKEN"]
BOT = f"@discordbot:{DOMAIN}"
MXID = f"@draupnir:{DOMAIN}"
TOKEN_FILE = "secrets/draupnir-access-token"
moderator, room = sys.argv[1], sys.argv[2]


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

# 2. management room: Draupnir joined as admin, invites limited to admins
pl = call(f"/_matrix/client/v3/rooms/{q(room)}/state/m.room.power_levels", token=AS, as_bot=True)
if pl.get("users", {}).get(MXID, 0) < 100 or pl.get("invite", 0) < 50:
    pl.setdefault("users", {})[MXID] = 100
    pl["invite"] = max(pl.get("invite", 0), 50)
    call(f"/_matrix/client/v3/rooms/{q(room)}/state/m.room.power_levels", "PUT", pl,
         token=AS, as_bot=True)
if room not in call("/_matrix/client/v3/joined_rooms", token=token)["joined_rooms"]:
    call(f"/_matrix/client/v3/rooms/{q(room)}/invite", "POST", {"user_id": MXID},
         token=AS, as_bot=True)
    call(f"/_matrix/client/v3/rooms/{q(room)}/join", "POST", {}, token=token)
print("management room", room, "joined, Draupnir admin, invites limited to admins")

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
try:  # as the admin: the bridge may only create aliases in its own namespace
    call(f"/_matrix/client/v3/directory/room/{q(space_alias)}", "PUT", {"room_id": space})
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
