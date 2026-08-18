"""Discord to Matrix onboarding daemon.

Watches the white-check-mark reactions on the Discord messages listed in
ONBOARD_WATCH (comma-separated "channelid:messageid" pairs). For each new
reactor it:
  1. creates a Matrix account named after their Discord username,
  2. sets the displayname and mirrors their Discord avatar,
  3. invites the account to every bridged channel (acting as the bridge bot,
     which is room admin in every portal) and accepts each invite,
  4. DMs the reactor their credentials and the Element URL.

Processed user IDs are persisted in /state/processed.json so reactions are
only handled once. Pure stdlib, no dependencies.
"""
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote

SYNAPSE = os.environ.get("SYNAPSE_URL", "http://synapse:8008")
DOMAIN = os.environ.get("MATRIX_DOMAIN", "gocosmos.org")
ELEMENT_URL = os.environ.get("ELEMENT_URL", "https://chat.gocosmos.org")
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ADMIN_TOKEN = os.environ.get("ONBOARD_ADMIN_TOKEN", "")
AS_TOKEN = os.environ.get("BRIDGE_AS_TOKEN", "")
GUILD = os.environ.get("GUILD_ID", "")
WATCH = [w.strip() for w in os.environ.get("ONBOARD_WATCH", "").split(",") if ":" in w]
CHECK = "%E2%9C%85"  # the white-check-mark emoji, urlencoded
STATE = "/state/processed.json"
UA = "CosmosOnboarding (https://gocosmos.org, 1.0)"
POLL_SECONDS = 30


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def http(url, method="GET", body=None, headers=None, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body
    hdrs.setdefault("User-Agent", UA)
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = resp.read()
        return payload if raw else (json.loads(payload) if payload else {})


def discord(path, method="GET", body=None):
    return http("https://discord.com/api/v10" + path, method, body,
                {"Authorization": "Bot " + DISCORD_TOKEN})


BOT_MXID = f"@discordbot:{DOMAIN}"


def matrix(path, method="GET", body=None, token=None, as_user=None):
    # as_user: appservice impersonation; the registration's sender_localpart is
    # a random user, so acting as the bridge bot needs an explicit user_id.
    if as_user:
        path += ("&" if "?" in path else "?") + "user_id=" + quote(as_user, safe="")
    return http(SYNAPSE + path, method, body,
                {"Authorization": "Bearer " + (token or ADMIN_TOKEN)})


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)


def dm(user_id, text):
    channel = discord("/users/@me/channels", "POST", {"recipient_id": user_id})
    discord(f"/channels/{channel['id']}/messages", "POST", {"content": text})


def bridged_rooms():
    """Every room the bridge bot is in that is a channel portal or the space."""
    rooms = []
    for room in matrix("/_matrix/client/v3/joined_rooms", token=AS_TOKEN,
                       as_user=BOT_MXID)["joined_rooms"]:
        rq = quote(room, safe="")
        try:
            create = matrix(f"/_matrix/client/v3/rooms/{rq}/state/m.room.create",
                            token=AS_TOKEN, as_user=BOT_MXID)
            if create.get("type") == "m.space":
                rooms.append((room, "space"))
                continue
            name = matrix(f"/_matrix/client/v3/rooms/{rq}/state/m.room.name",
                          token=AS_TOKEN, as_user=BOT_MXID).get("name", "")
        except urllib.error.HTTPError:
            continue
        if name.startswith("#"):
            rooms.append((room, name))
    return rooms


def process(user):
    uid, uname = user["id"], user["username"]
    localpart = re.sub(r"[^a-z0-9._=\-]", ".", uname.lower())
    mxid = f"@{localpart}:{DOMAIN}"
    entry = {"username": uname, "mxid": mxid, "ts": time.time()}

    try:
        matrix(f"/_synapse/admin/v2/users/{quote(mxid, safe='')}")
        dm(uid, f"You already have a Matrix account ({mxid}). Sign in at {ELEMENT_URL}")
        entry["status"] = "already-existed"
        return entry
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    member = discord(f"/guilds/{GUILD}/members/{uid}")
    display = member.get("nick") or member["user"].get("global_name") or uname
    password = secrets.token_urlsafe(12)
    matrix(f"/_synapse/admin/v2/users/{quote(mxid, safe='')}", "PUT",
           {"password": password, "displayname": display})
    user_token = matrix("/_matrix/client/v3/login", "POST",
                        {"type": "m.login.password",
                         "identifier": {"type": "m.id.user", "user": localpart},
                         "password": password})["access_token"]

    avatar_hash = member.get("avatar") or member["user"].get("avatar")
    if avatar_hash:
        cdn = (f"https://cdn.discordapp.com/guilds/{GUILD}/users/{uid}/avatars/{avatar_hash}.png"
               if member.get("avatar")
               else f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png")
        png = http(cdn + "?size=256", raw=True)
        mxc = http(SYNAPSE + "/_matrix/media/v3/upload?filename=avatar.png", "POST", png,
                   {"Authorization": "Bearer " + user_token,
                    "Content-Type": "image/png"})["content_uri"]
        matrix(f"/_matrix/client/v3/profile/{quote(mxid, safe='')}/avatar_url", "PUT",
               {"avatar_url": mxc}, token=user_token)

    joined = 0
    for room, name in bridged_rooms():
        rq = quote(room, safe="")
        try:
            matrix(f"/_matrix/client/v3/rooms/{rq}/invite", "POST",
                   {"user_id": mxid}, token=AS_TOKEN, as_user=BOT_MXID)
        except urllib.error.HTTPError as e:
            log("invite failed:", name, e.code)
        try:
            matrix(f"/_matrix/client/v3/rooms/{rq}/join", "POST", {}, token=user_token)
            joined += 1
        except urllib.error.HTTPError as e:
            log("join failed:", name, e.code)

    dm(uid,
       "Welcome to the Cosmos Matrix server! 🎉\n"
       f"Your account is ready and already joined to {joined} bridged channels.\n\n"
       f"Sign in at {ELEMENT_URL}\n"
       f"Username: `{localpart}`\n"
       f"Temporary password: `{password}`\n\n"
       "Please change the password right away: Settings > General > Change password.")
    entry["status"] = f"created, joined {joined} rooms"
    return entry


def reactors(channel_id, message_id):
    users, after = [], None
    while True:
        path = f"/channels/{channel_id}/messages/{message_id}/reactions/{CHECK}?limit=100"
        if after:
            path += "&after=" + after
        batch = discord(path)
        users += batch
        if len(batch) < 100:
            return users
        after = batch[-1]["id"]


def main():
    missing = [k for k in ("DISCORD_BOT_TOKEN", "ONBOARD_ADMIN_TOKEN",
                           "BRIDGE_AS_TOKEN", "GUILD_ID")
               if not os.environ.get(k)]
    if missing:
        log("missing env vars:", ", ".join(missing), "- idling")
        while True:
            time.sleep(3600)

    state = load_state()
    log(f"onboarding daemon up; watching {len(WATCH)} message(s), "
        f"{len(state)} user(s) already processed")
    while True:
        for watch in WATCH:
            channel_id, message_id = watch.split(":", 1)
            try:
                for user in reactors(channel_id, message_id):
                    if user.get("bot") or user["id"] in state:
                        continue
                    log("reaction from", user["username"])
                    try:
                        state[user["id"]] = process(user)
                        log("done:", state[user["id"]]["status"])
                    except Exception as e:  # keep the daemon alive, record the failure
                        log("ERROR processing", user["username"], repr(e))
                        state[user["id"]] = {"username": user["username"],
                                             "status": "error: " + repr(e)[:200],
                                             "ts": time.time()}
                    save_state(state)
            except Exception as e:
                log("ERROR polling", watch, repr(e))
        time.sleep(POLL_SECONDS)


main()
