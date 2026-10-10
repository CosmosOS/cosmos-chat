"""Discord to Matrix onboarding daemon.

Watches the white-check-mark reactions on the Discord messages listed in
ONBOARD_WATCH (comma-separated "channelid:messageid" pairs). For each new
reactor it:
  1. creates a Matrix account named after their Discord username,
  2. sets the displayname and mirrors their Discord avatar,
  3. joins the account to every bridged channel they can see on Discord:
     the guild space first, then the open rooms directly (no invite shown in
     the channel) and the invite-only ones through an invite from the bridge
     bot, which is room admin in every portal,
  4. DMs the reactor their credentials and the Element URL.

A first DM is sent before anything is created: members whose DMs are closed
get their reaction removed instead of an account they cannot log into, and
can react again once DMs are open. Processed user IDs are persisted in
/state/processed.json so reactions are only handled once; failures are
retried after a back-off.

Once DRAUPNIR_MXID is set it also opens the community to Matrix accounts
from other servers, every 10 minutes (sync_access): the guild space is
public, the rooms @everyone can see on Discord are joinable by its members,
staff rooms stay invite-only, and Draupnir moderates every open room.
New members of the guild space are greeted in the welcome room
(welcome_new), the one place showing arrivals. Pure stdlib, no
dependencies.
"""
import html
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
RETRY_SECONDS = 600
# The moderation bot (draupnir/README.md). Rooms are only opened to other
# servers once it is set.
DRAUPNIR = os.environ.get("DRAUPNIR_MXID", "")
ACCESS_STATE = "/state/access.json"
ACCESS_SECONDS = 600
WELCOME_STATE = "/state/welcome.json"
WELCOME_LOOKUP_SECONDS = 3600  # how often the welcome room is looked up again


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


def load_state(path=None):
    try:
        with open(path or STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, path=None):
    path = path or STATE
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


class DMClosed(Exception):
    """The Discord member does not accept DMs from server members."""


def dm(user_id, text):
    channel = discord("/users/@me/channels", "POST", {"recipient_id": user_id})
    try:
        discord(f"/channels/{channel['id']}/messages", "POST", {"content": text})
    except urllib.error.HTTPError as e:
        if e.code == 403:  # Discord error 50007: cannot send messages to this user
            raise DMClosed() from e
        raise


VIEW_CHANNEL = 0x400
ADMINISTRATOR = 0x8


def visible_channel_ids(member=None):
    """Discord channel ids the given guild member may see, computed with
    Discord's permission algorithm (base role perms, then @everyone, role and
    member overwrites). member=None means @everyone; None is returned for
    administrators, who see every channel."""
    roles = {r["id"]: int(r["permissions"])
             for r in discord(f"/guilds/{GUILD}/roles")}
    member_roles = member["roles"] if member else []
    base = roles.get(GUILD, 0)
    for rid in member_roles:
        base |= roles.get(rid, 0)
    if base & ADMINISTRATOR:
        return None
    visible = set()
    for ch in discord(f"/guilds/{GUILD}/channels"):
        perms = base
        ows = {o["id"]: o for o in ch.get("permission_overwrites", [])}
        if GUILD in ows:
            perms = perms & ~int(ows[GUILD]["deny"]) | int(ows[GUILD]["allow"])
        allow = deny = 0
        for rid in member_roles:
            if rid in ows:
                allow |= int(ows[rid]["allow"])
                deny |= int(ows[rid]["deny"])
        perms = perms & ~deny | allow
        if member and member["user"]["id"] in ows:
            o = ows[member["user"]["id"]]
            perms = perms & ~int(o["deny"]) | int(o["allow"])
        if perms & VIEW_CHANNEL:
            visible.add(ch["id"])
    return visible


def bot_rooms():
    """(room id, current state events) for every room the bridge bot is in."""
    for room in matrix("/_matrix/client/v3/joined_rooms", token=AS_TOKEN,
                       as_user=BOT_MXID)["joined_rooms"]:
        try:
            yield room, matrix(f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state",
                               token=AS_TOKEN, as_user=BOT_MXID)
        except urllib.error.HTTPError:
            continue


def portal_info(state):
    """(create type, name, Discord channel id) of a room. The channel id is
    None for rooms without bridge info state (the bridge's personal spaces,
    forum post rooms)."""
    create_type = name = cid = None
    for ev in state:
        if ev["type"] == "m.room.create":
            create_type = ev["content"].get("type")
        elif ev["type"] == "m.room.name":
            name = ev["content"].get("name", "")
        elif ev["type"] in ("m.bridge", "uk.half-shot.bridge") and cid is None:
            cid = ev["content"].get("channel", {}).get("id")
    return create_type, name or "", cid


def state_content(state, etype, key=""):
    for ev in state:
        if ev["type"] == etype and ev["state_key"] == key:
            return ev["content"]
    return {}


def bridged_rooms(visible):
    """Bridged rooms (channel portals + guild/category spaces) the target
    user may see per Discord permissions. visible is the permitted Discord
    channel id set, or None for see-everything. Rooms without bridge info
    state (the bridge's personal spaces) are never included. The guild space
    comes first: its members can then join the open rooms without invite."""
    rooms = []
    for room, state in bot_rooms():
        create_type, name, cid = portal_info(state)
        if cid is None:
            continue
        if create_type == "m.space":
            if cid == GUILD:
                rooms.insert(0, (room, "space"))
            elif visible is None or cid in visible:
                rooms.append((room, "space"))
        elif name.startswith("#"):
            if visible is None or cid in visible:
                rooms.append((room, name))
    return rooms


def join_bridged(room, name, mxid, user_token):
    """Join the account to a bridged room. Open rooms are joined directly, so
    the channel shows no invite; invite-only ones (staff rooms, or rooms
    Draupnir locked) get an invite from the bridge bot first."""
    rq = quote(room, safe="")
    invited = False
    for _ in range(8):
        try:
            matrix(f"/_matrix/client/v3/rooms/{rq}/join", "POST", {}, token=user_token)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = json.loads(e.read()).get("retry_after_ms", 2000) / 1000
                except Exception:
                    wait = 2
                time.sleep(min(wait + 0.1, 15))
                continue
            if e.code != 403 or invited:
                log("join failed:", name, e.code)
                return False
        try:
            matrix(f"/_matrix/client/v3/rooms/{rq}/invite", "POST",
                   {"user_id": mxid}, token=AS_TOKEN, as_user=BOT_MXID)
        except urllib.error.HTTPError as e:
            log("invite failed:", name, e.code)
            return False
        invited = True
    log("join gave up after retries:", name)
    return False


def hide_space_people(mxid, user_token, rooms):
    """Element lists your DMs with a space's members inside that space;
    everyone is in every category, so DMs would follow you into each one.
    That "People" section is a per-account, per-space Element setting the
    config cannot default, so it is turned off on each bridged space."""
    for room, name in rooms:
        if name != "space":
            continue
        path = (f"/_matrix/client/v3/user/{quote(mxid, safe='')}/rooms/"
                f"{quote(room, safe='')}/account_data/im.vector.web.settings")
        try:
            try:
                current = matrix(path, token=user_token)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                current = {}
            matrix(path, "PUT", {**current, "Spaces.showPeopleInSpace": False}, token=user_token)
        except Exception as e:  # a display preference never fails onboarding
            log("could not hide people in space", room, repr(e))


def ensure_moderator(room, state, session):
    """Draupnir joined to the room as room admin (bans, redactions, join
    rules, server ACLs). Its access token is minted through the admin API
    once per sync (cached in session) instead of being stored here."""
    rq = quote(room, safe="")
    pl = state_content(state, "m.room.power_levels")
    if pl.get("users", {}).get(DRAUPNIR, 0) < 100:
        pl = {**pl, "users": {**pl.get("users", {}), DRAUPNIR: 100}}
        matrix(f"/_matrix/client/v3/rooms/{rq}/state/m.room.power_levels", "PUT", pl,
               token=AS_TOKEN, as_user=BOT_MXID)
    if state_content(state, "m.room.member", DRAUPNIR).get("membership") == "join":
        return
    if "token" not in session:
        session["token"] = matrix(
            f"/_synapse/admin/v1/users/{quote(DRAUPNIR, safe='')}/login", "POST",
            {"valid_until_ms": int(time.time() * 1000) + 3600_000})["access_token"]
    join = f"/_matrix/client/v3/rooms/{rq}/join"
    try:  # restricted rooms let it in as a guild space member, no invite
        matrix(join, "POST", {}, token=session["token"])
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
        # an invite also makes Draupnir post a "not a manager" notice in its
        # management room, so it is only sent for rooms still invite-only
        matrix(f"/_matrix/client/v3/rooms/{rq}/invite", "POST",
               {"user_id": DRAUPNIR}, token=AS_TOKEN, as_user=BOT_MXID)
        matrix(join, "POST", {}, token=session["token"])
    log("Draupnir joined", state_content(state, "m.room.name").get("name", room))


def sync_access(access):
    """Who can join each bridged room, in line with Discord: the guild space
    is public, so Matrix accounts from any server can join it; the rooms of
    the channels and categories @everyone can see are joinable by its
    members; every other room stays invite-only. Draupnir joins a room as
    its admin before the room opens. access maps room id -> the join rule
    this sync last set: a room it opened that is invite-only now was locked
    by Draupnir (raid protection) or a moderator and stays locked, while a
    room no longer public on Discord is always closed."""
    visible = visible_channel_ids()  # @everyone
    if visible is None:  # @everyone is administrator: never open staff rooms
        log("@everyone sees every channel, not opening any room")
        return
    rooms = [(room, state, *portal_info(state)) for room, state in bot_rooms()]
    space = next((r[0] for r in rooms if r[2] == "m.space" and r[4] == GUILD), None)
    if space is None:
        return
    restricted = {"join_rule": "restricted",
                  "allow": [{"type": "m.room_membership", "room_id": space}]}
    session = {}
    try:
        # the guild space last: every room is moderated before outsiders can enter
        for room, state, create_type, name, cid in sorted(rooms, key=lambda r: r[0] == space):
            label = name or ("guild space" if room == space else room)
            rules = state_content(state, "m.room.join_rules")
            current = rules.get("join_rule", "invite")
            try:
                if room == space:
                    target = {"join_rule": "public"}
                elif cid and (create_type == "m.space" or name.startswith("#")):
                    target = restricted if cid in visible else {"join_rule": "invite"}
                else:
                    # forum post rooms keep their own (restricted) rules
                    if current in ("public", "restricted"):
                        ensure_moderator(room, state, session)
                    continue
                path = f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/m.room.join_rules"
                if target["join_rule"] == "invite":
                    if current != "invite":
                        matrix(path, "PUT", target, token=AS_TOKEN, as_user=BOT_MXID)
                        log("closed", label)
                    access[room] = "invite"
                    continue
                if access.get(room, "invite") != "invite" and current == "invite":
                    continue  # locked by Draupnir or a moderator
                ensure_moderator(room, state, session)
                if {k: rules.get(k) for k in target} != target:
                    matrix(path, "PUT", target, token=AS_TOKEN, as_user=BOT_MXID)
                    log("opened", label)
                access[room] = target["join_rule"]
            except Exception as e:  # one broken room must not stall the others
                log("ERROR syncing access of", label, repr(e))
    finally:
        if "token" in session:
            try:
                matrix("/_matrix/client/v3/logout", "POST", {}, token=session["token"])
            except Exception:
                pass


def find_welcome():
    """(guild space, welcome room): the bridged room named #welcome, else
    the one of Discord's system channel (where Discord posts its join
    messages; #off-topic on Cosmos). None when either is missing."""
    system = discord(f"/guilds/{GUILD}").get("system_channel_id")
    space = by_cid = by_name = None
    for room, state in bot_rooms():
        create_type, name, cid = portal_info(state)
        if create_type == "m.space" and cid == GUILD:
            space = room
        elif name == "#welcome":
            by_name = room
        elif cid and cid == system:
            by_cid = room
    welcome = by_name or by_cid
    return (space, welcome) if space and welcome else None


def welcome_new(welcome, rooms):
    """Greets each new member of the guild space in the welcome room, like
    Discord's join messages: one place for arrivals instead of join events
    in every channel (Element hides those by default, element/config.json).
    Posted by the bridge bot, which the bridge never relays to Discord, so
    Discord's own join message is not doubled. The first run only records
    the current members."""
    space, room = rooms
    members = matrix(f"/_matrix/client/v3/rooms/{quote(space, safe='')}/joined_members",
                     token=AS_TOKEN, as_user=BOT_MXID)["joined"]
    people = {user: m.get("display_name") or user for user, m in members.items()
              if user not in (BOT_MXID, DRAUPNIR)
              and not (user.startswith("@discord_") and user.endswith(":" + DOMAIN))}
    if "welcomed" not in welcome:
        welcome["welcomed"] = sorted(people)
        return
    seen = set(welcome["welcomed"])
    for user, name in people.items():
        if user in seen:
            continue
        server = user.split(":", 1)[1]
        origin = "" if server == DOMAIN else f" (from {server})"
        link = f'<a href="https://matrix.to/#/{user}">{html.escape(name)}</a>'
        txn = f"welcome{int(time.time() * 1000)}{len(welcome['welcomed'])}"
        matrix(f"/_matrix/client/v3/rooms/{quote(room, safe='')}/send/m.room.message/{txn}",
               "PUT", {"msgtype": "m.text",
                       "body": f"👋 Welcome {name}{origin} to Cosmos!",
                       "format": "org.matrix.custom.html",
                       "formatted_body": f"👋 Welcome {link}{html.escape(origin)} to Cosmos!",
                       "m.mentions": {}},  # greet without pinging
               token=AS_TOKEN, as_user=BOT_MXID)
        log("welcomed", user)
        welcome["welcomed"].append(user)  # kept even if a later greeting fails


def process(user, state):
    uid, uname = user["id"], user["username"]
    localpart = re.sub(r"[^a-z0-9._=\-]", ".", uname.lower())
    mxid = f"@{localpart}:{DOMAIN}"
    admin_user = f"/_synapse/admin/v2/users/{quote(mxid, safe='')}"
    prev = state.get(uid, {})
    entry = {"username": uname, "mxid": mxid, "ts": time.time()}

    try:
        matrix(admin_user)
        exists = True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        exists = False

    # An account this daemon created for this member whose credentials never
    # reached them gets a fresh password. Any other existing account is left
    # alone: the same username does not mean the same person.
    if exists and not (prev.get("created") and prev.get("mxid") == mxid):
        dm(uid, f"You already have a Matrix account ({mxid}). Sign in at {ELEMENT_URL}")
        entry["status"] = "already-existed"
        return entry

    # Raises DMClosed before anything is created if the credentials could not
    # be delivered
    dm(uid, "⏳ Setting up your Cosmos Matrix account, this takes about a minute...")

    member = discord(f"/guilds/{GUILD}/members/{uid}")
    display = member.get("nick") or member["user"].get("global_name") or uname
    password = secrets.token_urlsafe(12)
    if exists:
        matrix(admin_user, "PUT", {"password": password, "logout_devices": True})
    else:
        matrix(admin_user, "PUT", {"password": password, "displayname": display})
    entry.update(created=True, status="pending: credentials not delivered")
    state[uid] = entry
    save_state(state)
    user_token = matrix("/_matrix/client/v3/login", "POST",
                        {"type": "m.login.password",
                         "identifier": {"type": "m.id.user", "user": localpart},
                         "password": password})["access_token"]

    avatar_hash = member.get("avatar") or member["user"].get("avatar")
    if avatar_hash and not exists:
        cdn = (f"https://cdn.discordapp.com/guilds/{GUILD}/users/{uid}/avatars/{avatar_hash}.png"
               if member.get("avatar")
               else f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png")
        png = http(cdn + "?size=256", raw=True)
        mxc = http(SYNAPSE + "/_matrix/media/v3/upload?filename=avatar.png", "POST", png,
                   {"Authorization": "Bearer " + user_token,
                    "Content-Type": "image/png"})["content_uri"]
        matrix(f"/_matrix/client/v3/profile/{quote(mxid, safe='')}/avatar_url", "PUT",
               {"avatar_url": mxc}, token=user_token)

    # Lift the per-user ratelimit while we burst-join ~40 rooms, restore after.
    override = f"/_synapse/admin/v1/users/{quote(mxid, safe='')}/override_ratelimit"
    matrix(override, "POST", {"messages_per_second": 0, "burst_count": 0})

    # Only the channels this Discord member can actually see: staff get the
    # staff rooms, everyone else gets the public ones
    rooms = bridged_rooms(visible_channel_ids(member))
    joined = sum(join_bridged(room, name, mxid, user_token) for room, name in rooms)
    hide_space_people(mxid, user_token, rooms)

    matrix(override, "DELETE")
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
    access = load_state(ACCESS_STATE)
    log(f"onboarding daemon up; watching {len(WATCH)} message(s), "
        f"{len(state)} user(s) already processed; room access sync "
        + (f"on, moderated by {DRAUPNIR}" if DRAUPNIR else "off (DRAUPNIR_MXID unset)"))
    retry_at = {}  # Discord user id -> earliest next attempt after a failure
    next_access = 0
    welcome, welcome_rooms, next_lookup = load_state(WELCOME_STATE), None, 0
    while True:
        if DRAUPNIR and time.time() >= next_access:
            next_access = time.time() + ACCESS_SECONDS
            try:
                sync_access(access)
            except Exception as e:
                log("ERROR syncing room access", repr(e))
            save_state(access, ACCESS_STATE)
        try:
            if time.time() >= next_lookup:
                next_lookup = time.time() + WELCOME_LOOKUP_SECONDS
                welcome_rooms = find_welcome()
                if not welcome_rooms:
                    log("no guild space or welcome room found, not greeting")
            if welcome_rooms:
                try:
                    welcome_new(welcome, welcome_rooms)
                finally:
                    save_state(welcome, WELCOME_STATE)
        except Exception as e:
            log("ERROR greeting new members", repr(e))
            # the rooms may have changed: look them up again soon
            next_lookup = min(next_lookup, time.time() + 300)
        for watch in WATCH:
            channel_id, message_id = watch.split(":", 1)
            try:
                for user in reactors(channel_id, message_id):
                    uid = user["id"]
                    done = uid in state and not state[uid]["status"].startswith(("error", "pending"))
                    if user.get("bot") or done or retry_at.get(uid, 0) > time.time():
                        continue
                    log("reaction from", user["username"])
                    try:
                        state[uid] = process(user, state)
                        log("done:", state[uid]["status"])
                    except DMClosed:
                        # Drop the reaction so the member can open their DMs and
                        # react again; back off only if it could not be removed
                        log("DMs closed:", user["username"])
                        try:
                            discord(f"/channels/{channel_id}/messages/{message_id}"
                                    f"/reactions/{CHECK}/{uid}", "DELETE")
                        except urllib.error.HTTPError as e:
                            log("could not remove reaction:", e.code)
                            retry_at[uid] = time.time() + RETRY_SECONDS
                    except Exception as e:  # keep the daemon alive, record the failure
                        log("ERROR processing", user["username"], repr(e))
                        state[uid] = {**state.get(uid, {}), "username": user["username"],
                                      "status": "error: " + repr(e)[:200],
                                      "ts": time.time()}
                        retry_at[uid] = time.time() + RETRY_SECONDS
                    save_state(state)
            except Exception as e:
                log("ERROR polling", watch, repr(e))
        time.sleep(POLL_SECONDS)


main()
