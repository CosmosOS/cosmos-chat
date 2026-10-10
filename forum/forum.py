"""Discord forum channels mirrored to Matrix: an index room plus one room per post.

mautrix-discord only bridges text and announcement channels, so the forums
listed in FORUM_CHANNELS are mirrored by this daemon instead:
  - the index room is built like a bridged channel ('#name', m.bridge state,
    in its Discord category's space), so it sits in the room list next to
    the other channels and the onboarding daemon and /join page auto-join
    new accounts to it; accounts already in the category space are joined
    once when it is created. It holds a pinned how-to and one card per post
    (title, tags, author, reply count, last activity, link), edited as the
    post changes so updates never notify anyone
  - each post is its own room in the same category space: name = "📌 " +
    title, topic = tags, author and Discord link, avatar = first image of
    the opening message. Post rooms are world_readable, so a card's title
    (a link to the opening message) shows the post before joining; members
    of the guild space can then join to reply. Nobody is joined
    automatically
  - Discord messages are sent by the bridge's own ghosts (@discord_<id>), so
    authors look exactly as they do in the bridged channels
  - Matrix messages, edits and redactions in a post room go back to the post
    through a webhook on the forum, under the sender's Matrix name and avatar
    (served by the bridge's avatar proxy), the same look as the bridge's
    relay mode
  - "!post Title" + description + #tags in the index creates a Discord post
    through the same webhook, its room (with the author joined) and its
    card; anything else posted in the index is redacted with a hint

The first time a forum is seen, every post with activity in the last
FORUM_BACKFILL_DAYS days is imported: its opening message plus that many
days of replies (a notice links older ones on Discord). A post revived later
is imported the same way. Discord is polled every POLL_SECONDS, so message
edits and deletions made on Discord are not mirrored; a post deleted on
Discord takes its card and room with it (checked when it leaves the active
list, and hourly for every post; the room is emptied at once and purged a
week later). State lives in
/state/forum-index.json. Pure stdlib, no dependencies.

Element's forum view (element/modules/cosmos-forum/index.js) lists every post of
a forum, not only the mirrored ones, through a small HTTP API on API_PORT
(Caddy serves it at chat.gocosmos.org/forum-api/):
  - GET /forums: the mirrored forums (index room, tags) and the post rooms
  - GET /posts?forum=<id>: every post, active or archived, from a catalog
    of the whole forum rescanned hourly (/state/forum-catalog.json); the
    opening message of each post is fetched once for its excerpt and author
  - POST /open {post}: the post's room, imported on the spot for a post
    that has none yet (its opening message plus the last OPEN_RECENT
    replies), so old posts only become rooms when someone reads them
  - POST /new {forum, title, body, tags}: same as !post
Callers prove who they are with a Matrix OpenID token (the widget sign-in
flow), checked against Synapse; only accounts of this server are served.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

SYNAPSE = os.environ.get("SYNAPSE_URL", "http://synapse:8008")
DOMAIN = os.environ.get("MATRIX_DOMAIN", "gocosmos.org")
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
AS_TOKEN = os.environ.get("BRIDGE_AS_TOKEN", "")
ADMIN_TOKEN = os.environ.get("ONBOARD_ADMIN_TOKEN", "")
GUILD = os.environ.get("GUILD_ID", "")
FORUMS = [c.strip() for c in os.environ.get("FORUM_CHANNELS", "").split(",") if c.strip()]
BACKFILL_DAYS = int(os.environ.get("FORUM_BACKFILL_DAYS", "60"))
# Same key and address as the bridge config (bridge.avatar_proxy_key and
# bridge.public_address), so webhook avatars use the bridge's proxy route
PROXY_KEY = os.environ.get("BRIDGE_AVATAR_PROXY_KEY", "")
PUBLIC_ADDRESS = os.environ.get("BRIDGE_PUBLIC_ADDRESS", "https://matrix.gocosmos.org")
STATE = "/state/forum-index.json"
UA = "CosmosForum (https://gocosmos.org, 1.0)"
POLL_SECONDS = 30
DELETE_CHECK_SECONDS = 3600  # how often every known post is checked for deletion
# A deleted post's room is emptied at once and purged later: purging right
# away also erases the kick events, so clients that had not synced yet keep
# a dead room they cannot even leave
PURGE_DELAY = 7 * 86400
MATRIX_MAX_UPLOAD = 20 * 1024 * 1024  # synapse max_upload_size
DISCORD_MAX_UPLOAD = 10 * 1024 * 1024  # webhook attachment limit without boosts
BOT_MXID = f"@discordbot:{DOMAIN}"
API_PORT = 8090
CATALOG = "/state/forum-catalog.json"
CATALOG_SCAN_SECONDS = 3600  # full rescan of every forum, archived posts included
OPEN_RECENT = 100            # replies imported when a post is opened from the forum view
OPEN_LIMIT = 10              # posts one account can bring over per OPEN_WINDOW
OPEN_WINDOW = 600
# The main loop, the catalog worker and the API threads share S, CAT and the
# Discord/Matrix side effects: each holds LOCK while it reads or changes them
LOCK = threading.RLock()


def bridge_user(mxid):
    """The bridge bot or one of its Discord ghosts, never relayed back (a
    remote @discord_x:other.server is a real user)."""
    return mxid == BOT_MXID or (mxid.startswith("@discord_") and mxid.endswith(":" + DOMAIN))


DISCORD_EPOCH = 1420070400000
MESSAGE_TYPES = {0, 19, 20, 23}  # default, reply, slash command, context menu

S = {}            # persisted state, see load_state()
CAT = {}          # every post of the mirrored forums, see load_catalog()
OPENID = {}       # OpenID token -> (Matrix user, cache expiry)
OPENED = {}       # Matrix user -> times they brought a post over, see api_open()
FORUM_INFO = {}   # forum channel id -> Discord channel object (tags, name)
ROLES = {}        # Discord role id -> name, for <@&id> mentions
CHANNELS = {}     # Discord channel id -> name, for <#id> mentions
GUILD_SPACE = ""  # the bridge's space for the whole guild
LAST_ACTIVE = set()  # post ids in Discord's active list at the previous poll
NEXT_DELETE_CHECK = [0]


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def request(url, method="GET", body=None, headers=None, raw=False):
    """One HTTP call. Retries on 429 with the server's hint (Discord sends
    retry_after in seconds, Matrix retry_after_ms) and waits out Discord's
    bucket when it reports no requests remaining."""
    hdrs = {"User-Agent": UA, **(headers or {})}
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                if resp.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(resp.headers.get("X-RateLimit-Reset-After") or 1))
                return payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise
            try:
                hint = json.loads(e.read())
            except Exception:
                hint = {}
            wait = hint.get("retry_after") or hint.get("retry_after_ms", 2000) / 1000
            time.sleep(min(float(wait) + 0.2, 60))


def discord(path, method="GET", body=None):
    return request("https://discord.com/api/v10" + path, method, body,
                   {"Authorization": "Bot " + DISCORD_TOKEN})


def matrix(path, method="GET", body=None, as_user=BOT_MXID, token=None, ts=None,
           raw=False, headers=None):
    """Client-server API call. With the appservice token (the default) it is
    made as as_user: the bridge bot or one of its ghosts. ts backdates sent
    events, which Synapse honours for appservices."""
    params = []
    if token is None:
        token = AS_TOKEN
        if as_user:
            params.append("user_id=" + q(as_user))
    if ts is not None:
        params.append(f"ts={int(ts)}")
    if params:
        path += ("&" if "?" in path else "?") + "&".join(params)
    return request(SYNAPSE + path, method, body,
                   {"Authorization": "Bearer " + token, **(headers or {})}, raw)


def q(value):
    return quote(value, safe="")


def discord_error(e):
    """Discord's numeric error code from an HTTPError, if any."""
    try:
        return json.loads(e.read()).get("code")
    except Exception:
        return None


def snowflake_ms(sid):
    return (int(sid) >> 22) + DISCORD_EPOCH


def ms_snowflake(ms):
    return str(int(ms - DISCORD_EPOCH) << 22)


def iso_ms(stamp):
    return datetime.fromisoformat(stamp).timestamp() * 1000


def load_state():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    # forums: forum id -> {parent, index, webhook, backfilled, linked, members_joined}
    # posts: thread id -> {forum, origin, room, name, tags, author, text, avatar,
    #                      count, activity, last, members, opening, readable,
    #                      card, card_body}
    # d2e / e2d: Discord message id <-> first Matrix event id, both origins
    # relayed: Matrix event ids sent to Discord through the webhook
    # ghosts: ghost user ids known to exist
    # pending: Matrix user -> refused !post event ids to clean up on success
    # cooldown: Matrix user -> time of their last !post
    # purge: room of a deleted post -> when to purge it
    for key in ("forums", "posts", "d2e", "e2d", "relayed", "ghosts", "pending", "cooldown", "purge"):
        state.setdefault(key, {})
    return state


def save_state():
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S, f)
    os.replace(tmp, STATE)


def load_catalog():
    try:
        with open(CATALOG) as f:
            cat = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cat = {}
    # posts: thread id -> {forum, title, tags, owner, created, activity, count,
    #                      archived, locked, excerpt, author}; excerpt is None
    #                      until the opening message has been fetched
    cat.setdefault("posts", {})
    return cat


def save_catalog():
    tmp = CATALOG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(CAT, f)
    os.replace(tmp, CATALOG)


# --- Discord text to Matrix ------------------------------------------------

MENTION_RE = re.compile(r"<(@!?|@&|#)(\d+)>")
EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
TIME_RE = re.compile(r"<t:(-?\d+)(?::[tTdDfFR])?>")
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
URL_RE = re.compile(r"https?://[^\s<]+[^\s<.,:;\"')\]]")
INLINE = [
    (re.compile(r"\|\|(.+?)\|\|", re.S), r"<span data-mx-spoiler>\1</span>"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<strong>\1</strong>"),
    (re.compile(r"__(.+?)__", re.S), r"<u>\1</u>"),
    (re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])", re.S), r"<em>\1</em>"),
    (re.compile(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])", re.S), r"<em>\1</em>"),
    (re.compile(r"~~(.+?)~~", re.S), r"<del>\1</del>"),
]


def resolve(text, msg):
    """Discord's mention, emoji and timestamp tokens as readable text."""
    users = {u["id"]: u.get("global_name") or u["username"] for u in msg.get("mentions", [])}

    def mention(m):
        kind, ident = m.groups()
        if kind == "@&":
            return "@" + ROLES.get(ident, "role")
        if kind.startswith("@"):
            return "@" + users.get(ident, "unknown-user")
        return "#" + CHANNELS.get(ident, "channel")

    text = MENTION_RE.sub(mention, text)
    text = EMOJI_RE.sub(r":\1:", text)
    return TIME_RE.sub(lambda m: time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime(int(m.group(1)))), text)


def to_html(text):
    """Discord markdown to Matrix HTML: code, links, inline styles,
    headers, subtext and quotes."""
    tokens = []

    def stash(fragment):
        tokens.append(fragment)
        return f"\x00{len(tokens) - 1}\x00"

    def code_block(m):
        lang = f' class="language-{html.escape(m.group(1))}"' if m.group(1) else ""
        return stash(f"<pre><code{lang}>{html.escape(m.group(2).strip(chr(10)))}</code></pre>")

    text = re.sub(r"```(?:([\w+#-]+)\n)?(.*?)```", code_block, text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = LINK_RE.sub(lambda m: stash(
        f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>'), text)
    text = URL_RE.sub(lambda m: stash(
        f'<a href="{html.escape(m.group(0))}">{html.escape(m.group(0))}</a>'), text)
    text = html.escape(text, quote=False)
    for rx, rep in INLINE:
        text = rx.sub(rep, text)
    parts = []  # (is_block, fragment)
    for line in text.split("\n"):
        header = re.match(r"(#{1,3}) (.+)", line)
        if header:
            n = len(header.group(1))
            parts.append((True, f"<h{n}>{header.group(2)}</h{n}>"))
        elif line.startswith("&gt; "):
            parts.append((True, f"<blockquote>{line[5:]}</blockquote>"))
        elif line.startswith("-# "):
            parts.append((False, f"<sub>{line[3:]}</sub>"))
        else:
            parts.append((False, line))
    out = ""
    for i, (block, fragment) in enumerate(parts):
        if i and not block and not parts[i - 1][0]:
            out += "<br>"
        out += fragment
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], out)


def message_text(msg):
    """Plain text of a Discord message, including forwards, polls, stickers
    and embed-only bot messages."""
    text = resolve(msg.get("content") or "", msg)
    for snap in msg.get("message_snapshots") or []:
        inner = snap.get("message", {})
        text += ("\n" if text else "") + "↪️ Forwarded:\n" + resolve(inner.get("content") or "", inner)
    if msg.get("poll"):
        question = msg["poll"].get("question", {}).get("text", "")
        answers = [a.get("poll_media", {}).get("text", "") for a in msg["poll"].get("answers", [])]
        text += ("\n" if text else "") + f"📊 Poll: {question}\n" + "\n".join(f"• {a}" for a in answers)
    if not text:
        for embed in msg.get("embeds") or []:
            lines = [embed.get(k) for k in ("title", "description", "url") if embed.get(k)]
            text += ("\n" if text else "") + "\n".join(lines)
    for sticker in msg.get("sticker_items") or []:
        text += ("\n" if text else "") + f"[Sticker: {sticker.get('name', '')}]"
    return text




# --- Matrix helpers --------------------------------------------------------

def send(room, content, sender, ts=None, relates=None):
    if relates:
        content["m.relates_to"] = relates
    txn = secrets.token_hex(8)
    return matrix(f"/_matrix/client/v3/rooms/{q(room)}/send/m.room.message/{txn}",
                  "PUT", content, as_user=sender, ts=ts)["event_id"]


def edit(room, event_id, content, sender=BOT_MXID):
    """Replace a message's content (m.replace), with the usual fallback."""
    fallback = {k: v for k, v in content.items() if k in ("msgtype", "format")}
    fallback["body"] = "* " + content["body"]
    if "formatted_body" in content:
        fallback["formatted_body"] = "* " + content["formatted_body"]
    fallback["m.new_content"] = content
    return send(room, fallback, sender, relates={"rel_type": "m.replace", "event_id": event_id})


def redact(room, event_id, reason):
    matrix(f"/_matrix/client/v3/rooms/{q(room)}/redact/{q(event_id)}/{secrets.token_hex(8)}",
           "PUT", {"reason": reason})


def upload(data, ctype, name, sender=BOT_MXID):
    return matrix(f"/_matrix/media/v3/upload?filename={q(name)}", "POST", data,
                  as_user=sender, headers={"Content-Type": ctype})["content_uri"]


def set_state(room, etype, key, content):
    matrix(f"/_matrix/client/v3/rooms/{q(room)}/state/{etype}/{q(key)}", "PUT", content)


def restricted_state(parent, history="shared"):
    """Initial state of the index and post rooms: joinable by guild space
    members, full history for joiners (post rooms are world_readable, so
    clients can preview a post before joining it), a canonical parent
    space."""
    return [
        {"type": "m.room.join_rules", "state_key": "",
         "content": {"join_rule": "restricted",
                     "allow": [{"type": "m.room_membership", "room_id": GUILD_SPACE}]}},
        {"type": "m.room.history_visibility", "state_key": "",
         "content": {"history_visibility": history}},
        {"type": "m.room.guest_access", "state_key": "", "content": {"guest_access": "forbidden"}},
        {"type": "m.space.parent", "state_key": parent,
         "content": {"via": [DOMAIN], "canonical": True}},
    ]


def ensure_ghost(author, webhook=False):
    """The bridge's ghost for a Discord user. Ghosts the bridge has not
    created yet are registered here with the bridge's displayname template
    and the Discord avatar; existing ghosts are left to the bridge."""
    mxid = f"@discord_{author['id']}:{DOMAIN}"
    if mxid in S["ghosts"]:
        return mxid
    try:
        matrix("/_matrix/client/v3/register", "POST",
               {"type": "m.login.application_service",
                "username": f"discord_{author['id']}", "inhibit_login": True}, as_user=None)
        created = True
    except urllib.error.HTTPError as e:
        if e.code != 400:  # M_USER_IN_USE: the bridge already has this ghost
            raise
        created = False
    if created:
        if webhook:
            name = author.get("username") or "Webhook"
        else:
            name = (author.get("global_name") or author["username"]) + (" (bot)" if author.get("bot") else "")
        matrix(f"/_matrix/client/v3/profile/{q(mxid)}/displayname", "PUT",
               {"displayname": name}, as_user=mxid)
        if author.get("avatar"):
            try:
                png = request(f"https://cdn.discordapp.com/avatars/{author['id']}/"
                              f"{author['avatar']}.png?size=256", raw=True)
                mxc = upload(png, "image/png", "avatar.png", mxid)
                matrix(f"/_matrix/client/v3/profile/{q(mxid)}/avatar_url", "PUT",
                       {"avatar_url": mxc}, as_user=mxid)
            except urllib.error.HTTPError as e:
                log("avatar failed for", mxid, e.code)
    S["ghosts"][mxid] = True
    return mxid


def ensure_member(post, mxid):
    if mxid in post["members"]:
        return
    try:
        matrix(f"/_matrix/client/v3/rooms/{q(post['room'])}/invite", "POST", {"user_id": mxid})
    except urllib.error.HTTPError as e:
        if e.code != 403:  # already invited or joined
            raise
    matrix(f"/_matrix/client/v3/rooms/{q(post['room'])}/join", "POST", {}, as_user=mxid)
    post["members"].append(mxid)


def admin_join(room, users):
    """Join local accounts to a room through the admin API, which needs the
    server admin in the room: it joins, joins everyone, then leaves."""
    admin = matrix("/_matrix/client/v3/account/whoami", token=ADMIN_TOKEN)["user_id"]
    try:
        matrix(f"/_matrix/client/v3/rooms/{q(room)}/invite", "POST", {"user_id": admin})
    except urllib.error.HTTPError as e:
        if e.code != 403:  # still in the room from an interrupted run
            raise
    matrix(f"/_matrix/client/v3/rooms/{q(room)}/join", "POST", {}, token=ADMIN_TOKEN)
    joined = 0
    for user in users:
        if user == admin:
            continue
        try:
            matrix(f"/_synapse/admin/v1/join/{q(room)}", "POST", {"user_id": user}, token=ADMIN_TOKEN)
            joined += 1
        except urllib.error.HTTPError as e:
            log("could not join", user, e.code)
    matrix(f"/_matrix/client/v3/rooms/{q(room)}/leave", "POST", {}, token=ADMIN_TOKEN)
    return joined


# --- Discord to Matrix -----------------------------------------------------

def tag_names(fid, tag_ids):
    tags = {t["id"]: t["name"] for t in FORUM_INFO[fid].get("available_tags", [])}
    return [tags[t] for t in tag_ids if t in tags]


def post_topic(fid, tid, post):
    parts = []
    tags = tag_names(fid, post["tags"])
    if tags:
        parts.append("🏷️ " + ", ".join(tags))
    if post.get("author"):
        parts.append("by " + post["author"])
    parts.append(f"https://discord.com/channels/{GUILD}/{tid}")
    parts.append(f"listed in #{FORUM_INFO[fid]['name']}")
    return " · ".join(parts)


def send_attachment(room, att, sender, ts, reply_to):
    name = att.get("filename") or "file"
    relates = {"m.in_reply_to": {"event_id": reply_to}} if reply_to else None
    if att.get("size", 0) > MATRIX_MAX_UPLOAD:
        return send(room, {"msgtype": "m.text", "body": f"📎 {name}: {att['url']}"}, sender, ts, relates)
    data = request(att["url"], raw=True)
    ctype = att.get("content_type") or "application/octet-stream"
    kind = ctype.split("/")[0]
    info = {"mimetype": ctype, "size": len(data)}
    if att.get("width") and att.get("height"):
        info.update(w=att["width"], h=att["height"])
    content = {"msgtype": {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(kind, "m.file"),
               "body": name, "filename": name, "url": upload(data, ctype, name, sender), "info": info}
    return send(room, content, sender, ts, relates)


def mirror_message(fid, post, msg):
    """Send one Discord message (text, then one event per attachment) into
    the post's room as its author's ghost, with the original timestamp."""
    if msg["id"] in S["d2e"] or msg.get("type", 0) not in MESSAGE_TYPES:
        return
    if msg.get("webhook_id") and msg["webhook_id"] == S["forums"][fid].get("webhook", {}).get("id"):
        return  # a Matrix message this daemon relayed
    ghost = ensure_ghost(msg["author"], webhook=bool(msg.get("webhook_id")))
    ensure_member(post, ghost)
    ts = snowflake_ms(msg["id"])
    ref = (msg.get("message_reference") or {}).get("message_id")
    reply = S["d2e"].get(ref) if msg.get("type") == 19 else None
    first = None
    text = message_text(msg)
    if text:
        first = send(post["room"], {"msgtype": "m.text", "body": text,
                                    "format": "org.matrix.custom.html",
                                    "formatted_body": to_html(text)}, ghost, ts,
                     {"m.in_reply_to": {"event_id": reply}} if reply else None)
    for att in msg.get("attachments") or []:
        try:
            eid = send_attachment(post["room"], att, ghost, ts, None if first else reply)
        except urllib.error.HTTPError as e:
            log("attachment failed:", att.get("filename"), e.code)
            continue
        first = first or eid
    if first:
        S["d2e"][msg["id"]] = first
        S["e2d"][first] = msg["id"]
        post.setdefault("opening", first)


def fetch_after(channel_id, after):
    """Every message of a channel newer than the snowflake after, oldest
    first."""
    msgs = []
    while True:
        batch = discord(f"/channels/{channel_id}/messages?limit=100&after={after}")
        if not batch:
            return msgs
        batch.sort(key=lambda m: int(m["id"]))
        msgs += batch
        after = batch[-1]["id"]
        if len(batch) < 100:
            return msgs


def mirror_new(fid, tid, post):
    for msg in fetch_after(tid, post["last"]):
        mirror_message(fid, post, msg)
        post["last"] = msg["id"]
        post["activity"] = max(post["activity"], snowflake_ms(msg["id"]))
        save_state()


def room_avatar(starter):
    """mxc of a thumbnail of the opening message's first image, if any."""
    for att in (starter or {}).get("attachments") or []:
        if not (att.get("content_type") or "").startswith("image/"):
            continue
        url = att.get("proxy_url") or att["url"]
        if att.get("width") and att.get("height"):
            w = min(512, att["width"])
            url += ("&" if "?" in url else "?") + f"width={w}&height={max(1, att['height'] * w // att['width'])}"
        try:
            return upload(request(url, raw=True), att["content_type"], att.get("filename") or "avatar")
        except urllib.error.HTTPError as e:
            log("room avatar failed:", e.code)
        return None
    return None


def create_post_room(fid, tid, post):
    """The post's own room, a child of the forum's category space."""
    state = restricted_state(S["forums"][fid]["parent"], "world_readable")
    post["readable"] = True
    if post.get("avatar"):
        state.append({"type": "m.room.avatar", "state_key": "", "content": {"url": post["avatar"]}})
    post["room"] = matrix("/_matrix/client/v3/createRoom", "POST", {
        "name": "📌 " + post["name"][:250], "topic": post_topic(fid, tid, post),
        "preset": "private_chat", "visibility": "private", "initial_state": state,
        # the admin API join of a Matrix post's author invites as the (level 0) admin
        "power_level_content_override": {"invite": 0},
    })["room_id"]
    post["members"] = [BOT_MXID]
    S["posts"][tid] = post
    CHANNELS[tid] = post["name"]
    save_state()


def link_post(fid, post):
    set_state(S["forums"][fid]["parent"], "m.space.child", post["room"], {"via": [DOMAIN]})


def import_post(fid, thread, cutoff_ms):
    """Create the room of a post made on Discord, mirror its opening message
    plus every reply since cutoff_ms (all of them for posts created after
    it), then list it in the index."""
    tid = thread["id"]
    try:
        starter = discord(f"/channels/{tid}/messages/{tid}")
    except urllib.error.HTTPError as e:
        if e.code != 404:  # the opening message was deleted
            raise
        starter = None
    author = None
    if starter:
        author = starter["author"].get("global_name") or starter["author"]["username"]
    older = snowflake_ms(tid) < cutoff_ms
    post = {"forum": fid, "origin": "discord", "name": thread["name"],
            "tags": thread.get("applied_tags", []), "author": author,
            "text": message_text(starter) if starter else "", "avatar": room_avatar(starter),
            "count": thread.get("message_count", 0),
            "activity": snowflake_ms(thread.get("last_message_id") or tid),
            "last": ms_snowflake(cutoff_ms) if older else tid}
    create_post_room(fid, tid, post)
    if starter:
        mirror_message(fid, post, starter)
    if older:
        notice = send(post["room"], {"msgtype": "m.notice",
                                     "body": f"Older messages of this post are on Discord: "
                                             f"https://discord.com/channels/{GUILD}/{tid}"},
                      BOT_MXID, snowflake_ms(tid) + 1)
        post.setdefault("opening", notice)
    mirror_new(fid, tid, post)
    link_post(fid, post)
    update_card(fid, tid, post)
    log("post imported:", thread["name"])


# --- index cards -----------------------------------------------------------

def post_link(post):
    """Permalink to the post's opening message. Element turns a plain room
    link in HTML into a pill showing the room id (the card reader has not
    joined), but keeps an event permalink with its own label as a link."""
    if post.get("opening"):
        return f"https://matrix.to/#/{post['room']}/{post['opening']}?via={DOMAIN}"
    return f"https://matrix.to/#/{post['room']}?via={DOMAIN}"


def card_content(fid, tid, post):
    link = post_link(post)
    meta = []
    tags = tag_names(fid, post["tags"])
    if tags:
        meta.append("🏷️ " + ", ".join(tags))
    if post.get("author"):
        meta.append("by " + post["author"])
    meta.append(f"💬 {post['count']}")
    meta.append("🕒 " + time.strftime("%Y-%m-%d", time.gmtime(post["activity"] / 1000)))
    excerpt = (post.get("text") or "").strip().split("\n")[0]
    if len(excerpt) > 160:
        excerpt = excerpt[:160] + "…"
    body = f"📌 {post['name']}\n{' · '.join(meta)}\n" + (f"{excerpt}\n" if excerpt else "") + link
    formatted = (f'<strong>📌 <a href="{html.escape(link)}">{html.escape(post["name"])}</a></strong>'
                 f"<br>{html.escape(' · '.join(meta))}")
    if excerpt:
        formatted += f"<br><em>{html.escape(excerpt)}</em>"
    return {"msgtype": "m.notice", "body": body,
            "format": "org.matrix.custom.html", "formatted_body": formatted}


def update_card(fid, tid, post):
    """Post the card in the index, or edit it when something it shows
    changed (edits do not notify anyone)."""
    content = card_content(fid, tid, post)
    if post.get("card_body") == content["body"]:
        return
    index = S["forums"][fid]["index"]
    if post.get("card"):
        edit(index, post["card"], content)
    else:
        post["card"] = send(index, content, BOT_MXID)
    post["card_body"] = content["body"]
    save_state()


def refresh_post(fid, tid, post, thread):
    """Follow title and tag changes made on Discord, new replies and the
    reply count."""
    if thread["name"] != post["name"] or thread.get("applied_tags", []) != post["tags"]:
        post["name"], post["tags"] = thread["name"], thread.get("applied_tags", [])
        set_state(post["room"], "m.room.name", "", {"name": "📌 " + post["name"][:250]})
        set_state(post["room"], "m.room.topic", "", {"topic": post_topic(fid, tid, post)})
        CHANNELS[tid] = post["name"]
    post["count"] = thread.get("message_count", post["count"])
    last = thread.get("last_message_id")
    if last and int(last) > int(post["last"]):
        mirror_new(fid, tid, post)
    if last:
        post["activity"] = max(post["activity"], snowflake_ms(last))
    update_card(fid, tid, post)
    save_state()


def post_exists(tid):
    try:
        discord(f"/channels/{tid}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:  # Unknown Channel: the post was deleted on Discord
            return False
        raise


def remove_post(fid, tid, post):
    """A post deleted on Discord: drop its card, unlink its room from the
    space and shut the room down (everyone is removed), purging it after
    PURGE_DELAY."""
    if post.get("card"):
        try:
            redact(S["forums"][fid]["index"], post["card"], "Post deleted on Discord")
        except urllib.error.HTTPError as e:
            log("could not remove card:", e.code)
    set_state(S["forums"][fid]["parent"], "m.space.child", post["room"], {})
    del S["posts"][tid]
    CAT["posts"].pop(tid, None)
    CHANNELS.pop(tid, None)
    S["purge"][post["room"]] = time.time() + PURGE_DELAY
    save_state()
    matrix(f"/_synapse/admin/v2/rooms/{q(post['room'])}", "DELETE",
           {"purge": False, "block": False}, token=ADMIN_TOKEN)
    log("post deleted on Discord, room shut down:", post["name"])


def purge_due():
    for room, due in list(S["purge"].items()):
        if time.time() < due:
            continue
        try:
            matrix(f"/_synapse/admin/v2/rooms/{q(room)}", "DELETE",
                   {"purge": True, "block": False}, token=ADMIN_TOKEN)
        except urllib.error.HTTPError as e:
            log("could not purge", room, e.code)
            continue
        del S["purge"][room]
        save_state()


def check_deleted(active):
    """Posts that just left Discord's active list (archived or deleted) are
    checked at once, every known post once an hour."""
    global LAST_ACTIVE
    if time.time() >= NEXT_DELETE_CHECK[0]:
        suspects = [tid for tid in S["posts"] if tid not in active]
        NEXT_DELETE_CHECK[0] = time.time() + DELETE_CHECK_SECONDS
    else:
        suspects = [tid for tid in LAST_ACTIVE - active if tid in S["posts"]]
    LAST_ACTIVE = active
    for tid in suspects:
        try:
            if not post_exists(tid):
                post = S["posts"][tid]
                remove_post(post["forum"], tid, post)
        except Exception as e:
            log("ERROR checking post", tid, repr(e))


def poll_discord():
    cutoff = time.time() * 1000 - BACKFILL_DAYS * 86400000
    threads = [t for t in discord(f"/guilds/{GUILD}/threads/active")["threads"]
               if t.get("parent_id") in FORUMS]
    for thread in threads:  # new posts reach the forum view within a poll
        catalog_thread(thread)
    check_deleted({t["id"] for t in threads})
    for thread in threads:
        fid = thread["parent_id"]
        tid = thread["id"]
        post = S["posts"].get(tid)
        try:
            if post is None:  # a new post, or an old one revived by a reply
                if snowflake_ms(thread.get("last_message_id") or tid) < cutoff:
                    continue  # never archived on Discord, but quiet for longer than the window
                import_post(fid, thread, cutoff)
            else:
                refresh_post(fid, tid, post, thread)
        except Exception as e:  # keep the other posts going
            log("ERROR syncing post", thread.get("name"), repr(e))


def archived_threads(fid, since_ms=0):
    """A forum's archived posts, most recently archived first, stopping at
    the first page archived before since_ms (those have no newer activity)."""
    threads, before = [], None
    while True:
        page = discord(f"/channels/{fid}/threads/archived/public?limit=100"
                       + (f"&before={q(before)}" if before else ""))
        threads += page["threads"]
        if not page.get("has_more") or not page["threads"]:
            return threads
        before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]
        if iso_ms(before) < since_ms:
            return threads


# --- catalog of every post, for the forum view ------------------------------

def catalog_thread(thread):
    """Add a post to the catalog, or refresh what Discord's thread object
    says about it (its opening message is fetched separately)."""
    tid = thread["id"]
    meta = thread.get("thread_metadata") or {}
    entry = CAT["posts"].setdefault(tid, {"excerpt": None, "author": None})
    entry.update(forum=thread["parent_id"], title=thread["name"],
                 tags=thread.get("applied_tags", []), owner=thread.get("owner_id"),
                 created=snowflake_ms(tid),
                 activity=snowflake_ms(thread.get("last_message_id") or tid),
                 count=thread.get("message_count", 0),
                 archived=bool(meta.get("archived")), locked=bool(meta.get("locked")))


def scan_catalog():
    """Rebuild the catalog from every post of every forum, active and
    archived; posts Discord no longer lists were deleted. The Discord
    calls run without LOCK, so the bridge keeps going meanwhile."""
    active = [t for t in discord(f"/guilds/{GUILD}/threads/active")["threads"]
              if t.get("parent_id") in FORUMS]
    found = active + [t for fid in FORUMS for t in archived_threads(fid)]
    with LOCK:
        seen = set()
        for thread in found:
            catalog_thread(thread)
            seen.add(thread["id"])
        for tid in [t for t in CAT["posts"] if t not in seen]:
            del CAT["posts"][tid]
        save_catalog()
    log(f"post catalog: {len(seen)} post(s) in {len(FORUMS)} forum(s)")


def excerpt(text, limit=280):
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def fetch_openings(batch):
    """Excerpt and author of up to batch posts whose opening message was
    never fetched, most recently active first. Returns how many are left."""
    with LOCK:
        todo = sorted((t for t, p in CAT["posts"].items() if p.get("excerpt") is None),
                      key=lambda t: -CAT["posts"][t]["activity"])
    for tid in todo[:batch]:
        try:
            msg = discord(f"/channels/{tid}/messages/{tid}")
        except urllib.error.HTTPError as e:
            if e.code >= 500:  # retried on the next round
                raise
            msg = None  # the opening message was deleted (404), or unreadable
        author = None
        if msg:
            author = msg["author"].get("global_name") or msg["author"]["username"]
        else:
            owner = CAT["posts"].get(tid, {}).get("owner")
            try:
                user = discord(f"/users/{owner}") if owner else None
                author = user and (user.get("global_name") or user["username"])
            except urllib.error.HTTPError:
                pass
        with LOCK:
            entry = CAT["posts"].get(tid)
            if entry is not None:
                entry["excerpt"] = excerpt(message_text(msg)) if msg else ""
                entry["author"] = author
    if todo[:batch]:
        with LOCK:
            save_catalog()
    return max(0, len(todo) - batch)


def catalog_worker():
    """Background thread: the hourly rescan, and the opening messages of
    new posts (a few per second, so the first fill of a big forum takes
    minutes without starving the mirror of Discord rate limit)."""
    next_scan = 0
    while True:
        left = 0
        try:
            if time.time() >= next_scan:
                next_scan = time.time() + 300  # retry soon if the scan fails
                scan_catalog()
                next_scan = time.time() + CATALOG_SCAN_SECONDS
            left = fetch_openings(20)
        except Exception as e:
            log("ERROR updating the post catalog", repr(e))
        time.sleep(5 if left else 30)


def backfill_forum(fid):
    """Import every post with activity since the cutoff, active or
    archived, least recently active first, so the newest cards end up at
    the bottom of the index."""
    cutoff = time.time() * 1000 - BACKFILL_DAYS * 86400000
    threads = [t for t in discord(f"/guilds/{GUILD}/threads/active")["threads"]
               if t.get("parent_id") == fid]
    threads += archived_threads(fid, cutoff)
    recent = [t for t in threads if snowflake_ms(t.get("last_message_id") or t["id"]) >= cutoff]
    recent.sort(key=lambda t: snowflake_ms(t.get("last_message_id") or t["id"]))
    log(f"backfilling {len(recent)} post(s) of #{FORUM_INFO[fid]['name']}")
    for thread in recent:
        try:
            post = S["posts"].get(thread["id"])
            if post is None:
                import_post(fid, thread, cutoff)
            else:  # resume a post interrupted by a restart
                mirror_new(fid, thread["id"], post)
                link_post(fid, post)
                update_card(fid, thread["id"], post)
        except Exception as e:  # keep the other posts going
            log("ERROR importing post", thread.get("name"), repr(e))
    S["forums"][fid]["backfilled"] = True
    save_state()


# --- Matrix to Discord -----------------------------------------------------

def proxy_url(mxc):
    """The bridge's signed avatar proxy URL for an mxc URI (see
    hashMediaProxyURL in mautrix-discord)."""
    if not (mxc and PROXY_KEY and mxc.startswith("mxc://")):
        return None
    server, media = mxc[6:].split("/", 1)
    path = f"/mautrix-discord/avatar/{server}/{media}/"
    digest = hmac.new(PROXY_KEY.encode(), path.encode(), hashlib.sha256).digest()
    return PUBLIC_ADDRESS + path + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def member_profile(room, user):
    """Name and proxied avatar of a user in a room, or from their global
    profile when they are not in it (a forum view user who left the index)."""
    try:
        member = matrix(f"/_matrix/client/v3/rooms/{q(room)}/state/m.room.member/{q(user)}")
    except urllib.error.HTTPError:
        try:
            member = matrix(f"/_matrix/client/v3/profile/{q(user)}")
        except urllib.error.HTTPError:
            member = {}
    return member.get("displayname") or user, proxy_url(member.get("avatar_url"))


def webhook_name(name):
    """Discord rejects webhook names containing these words."""
    name = re.sub(r"(?i)discord|clyde", "", name).strip()[:80]
    return name if name and name.lower() not in ("everyone", "here") else "Matrix user"


def plain_body(content):
    """A Matrix message body without the legacy reply fallback."""
    body = content.get("body") or ""
    if (content.get("m.relates_to") or {}).get("m.in_reply_to"):
        lines = body.split("\n")
        while lines and lines[0].startswith("> "):
            lines.pop(0)
        if lines and not lines[0]:
            lines.pop(0)
        body = "\n".join(lines)
    return body


def reply_embed(tid, post, target):
    """The bridge relay's reply embed: a link to the replied message, its
    author and its first line."""
    dmsg = S["e2d"].get(target)
    if not dmsg:
        return None
    try:
        ev = matrix(f"/_matrix/client/v3/rooms/{q(post['room'])}/event/{q(target)}")
    except urllib.error.HTTPError:
        return None
    ghost = re.match(r"@discord_(\d+):", ev["sender"])
    if ghost:
        who = f"<@{ghost.group(1)}>"
    elif ev["sender"] == BOT_MXID and post.get("author"):  # opening of a Matrix-made post
        who = post["author"]
    else:
        who = member_profile(post["room"], ev["sender"])[0]
    content = ev.get("content", {})
    if ev["sender"] == BOT_MXID and post.get("origin") == "matrix":
        content = {"body": post.get("text") or post["name"]}
    line = plain_body(content).strip().split("\n")[0]
    if len(line) > 72:
        line = line[:72] + "…"
    line = re.sub(r"([\\*_~`|>\[\]])", r"\\\1", line)
    url = f"https://discord.com/channels/{GUILD}/{tid}/{dmsg}"
    return {"description": f"**[Replying to]({url}) {who}**\n{line}"}


def multipart(payload, filename, data, ctype):
    boundary = secrets.token_hex(16)
    safe = re.sub(r'[^\w.\- ]', "_", filename) or "file"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
            f"filename=\"{safe}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def ensure_webhook(fid):
    """The forum's relay webhook, reusing one this bot already created."""
    forum = S["forums"][fid]
    if not forum.get("webhook"):
        hooks = [h for h in discord(f"/channels/{fid}/webhooks")
                 if h.get("name") == "Matrix" and h.get("token")]
        hook = hooks[0] if hooks else discord(f"/channels/{fid}/webhooks", "POST", {"name": "Matrix"})
        forum["webhook"] = {"id": hook["id"], "token": hook["token"]}
        save_state()
    return forum["webhook"]


def execute(fid, tid=None, method="POST", message_id=None, payload=None, file=None):
    """Run the forum webhook: inside post tid, or without tid to create a
    new post (payload thread_name). A deleted webhook (Discord error 10015)
    is recreated once."""
    for attempt in (1, 2):
        hook = ensure_webhook(fid)
        url = f"https://discord.com/api/v10/webhooks/{hook['id']}/{hook['token']}"
        if message_id:
            url += f"/messages/{message_id}"
        params = ([f"thread_id={tid}"] if tid else []) + (["wait=true"] if method == "POST" else [])
        if params:
            url += "?" + "&".join(params)
        try:
            if file:
                data, ctype = multipart(payload, *file)
                return request(url, method, data, {"Content-Type": ctype})
            return request(url, method, payload)
        except urllib.error.HTTPError as e:
            if attempt == 1 and discord_error(e) == 10015:
                log("webhook was deleted, recreating it")
                S["forums"][fid].pop("webhook", None)
                continue
            raise


def relay_event(tid, post, ev):
    """A Matrix message, edit or redaction in a post room, to Discord."""
    sender = ev["sender"]
    if bridge_user(sender) or ev["event_id"] in S["e2d"]:
        return
    fid = post["forum"]
    if ev["type"] == "m.room.redaction":
        target = ev.get("redacts") or ev.get("content", {}).get("redacts")
        if target in S["relayed"]:
            execute(fid, tid, "DELETE", S["e2d"][target])
        return
    content = ev.get("content") or {}
    rel = content.get("m.relates_to") or {}
    if rel.get("rel_type") == "m.replace":
        if rel.get("event_id") in S["relayed"]:
            new = content.get("m.new_content") or {}
            execute(fid, tid, "PATCH", S["e2d"][rel["event_id"]],
                    {"content": plain_body(new)[:2000], "allowed_mentions": {"parse": []}})
        return
    msgtype = content.get("msgtype")
    if not msgtype:
        return
    name, avatar = member_profile(post["room"], sender)
    payload = {"username": webhook_name(name), "allowed_mentions": {"parse": []}}
    if avatar:
        payload["avatar_url"] = avatar
    target = None if rel.get("is_falling_back") else (rel.get("m.in_reply_to") or {}).get("event_id")
    embed = reply_embed(tid, post, target) if target else None
    if embed:
        payload["embeds"] = [embed]
    sent = None
    if msgtype in ("m.image", "m.file", "m.video", "m.audio") and content.get("url"):
        filename = content.get("filename") or content.get("body") or "file"
        caption = content["body"] if content.get("filename") and content.get("body") != filename else ""
        size = (content.get("info") or {}).get("size", 0)
        data = None
        if size <= DISCORD_MAX_UPLOAD:
            server, media = content["url"][6:].split("/", 1)
            data = matrix(f"/_matrix/client/v1/media/download/{q(server)}/{q(media)}", raw=True)
        if data is not None and len(data) <= DISCORD_MAX_UPLOAD:
            ctype = (content.get("info") or {}).get("mimetype") or "application/octet-stream"
            sent = execute(fid, tid, payload={**payload, "content": caption[:2000]},
                           file=(filename, data, ctype))
        else:
            payload["content"] = f"📎 {filename} (too large for Discord, see Matrix)"
            sent = execute(fid, tid, payload=payload)
    else:
        text = plain_body(content)
        if msgtype == "m.emote":
            text = f"_{text}_"
        for start in range(0, max(len(text), 1), 2000):
            msg = execute(fid, tid, payload={**payload, "content": text[start:start + 2000]})
            sent = sent or msg
            payload.pop("embeds", None)
    S["e2d"][ev["event_id"]] = sent["id"]
    S["d2e"][sent["id"]] = ev["event_id"]
    S["relayed"][ev["event_id"]] = True
    save_state()


# --- new posts: !post in the index room, or the forum view -----------------

POST_COOLDOWN = 600  # seconds between two posts of the same Matrix user
NOT_A_POST = ("Only new posts go here. To create one, send: !post Your title, then a "
              "description on the next lines and optional #tags (see the pinned message). "
              "To reply to a post, open its card.")
HOWTO_VERSION = 2  # bump to rewrite the pinned how-to of existing index rooms


class PostError(Exception):
    """A new post refused before it reached Discord; the message says why."""


def postable_tags(fid):
    """Tags a Matrix user may apply: Discord keeps moderated ones for staff."""
    return [t for t in FORUM_INFO[fid].get("available_tags", []) if not t.get("moderated")]


def instructions(fid):
    names = [t["name"] for t in postable_tags(fid)]
    tags = ", ".join("#" + n for n in names)
    example = " ".join("#" + n for n in names[:2])
    optional = f" and #tags are optional ({tags})" if tags else ""
    body = ("📌 How to create a post from Matrix\n"
            "On chat.gocosmos.org, use the New post button of the forum view. From any "
            "other Matrix app, send one message starting with !post:\n\n"
            "!post Your title\n"
            "What your post is about\n"
            + (f"{example}\n" if example else "") + "\n"
            f"The first line is the title, the next lines the description{optional}. "
            "The bot creates the post on Discord under your name, opens a room for it and "
            "adds you to it: add screenshots and details there.\n"
            "Each card below opens a post's room, and replies there are shared with Discord. "
            "Other messages in this room are removed to keep the list clean.")
    formatted = ("<strong>📌 How to create a post from Matrix</strong><br>"
                 "On chat.gocosmos.org, use the <strong>New post</strong> button of the forum "
                 "view. From any other Matrix app, send one message starting with "
                 "<code>!post</code>:"
                 "<pre><code>!post Your title\nWhat your post is about"
                 + (f"\n{html.escape(example)}" if example else "") + "</code></pre>"
                 f"The first line is the title, the next lines the description"
                 f"{html.escape(optional)}. The bot creates the post on Discord under your "
                 "name, opens a room for it and adds you to it: add screenshots and details "
                 "there.<br>"
                 "Each card below opens a post's room, and replies there are shared with Discord. "
                 "Other messages in this room are removed to keep the list clean.")
    return {"msgtype": "m.text", "body": body,
            "format": "org.matrix.custom.html", "formatted_body": formatted}


def index_topic(fid):
    return " · ".join(filter(None, [(FORUM_INFO[fid].get("topic") or "").strip(),
                                    "Each card opens a post's room",
                                    "New post: !post Your title (see the pinned message)"]))


def parse_post(fid, body):
    """'!post Title\\ndescription\\n#tags' -> (title, description, tag ids)."""
    lines = body.strip().split("\n")
    title = lines[0][len("!post"):].strip()
    rest = lines[1:]
    if not title:
        while rest and not rest[0].strip():
            rest.pop(0)
        title = rest.pop(0).strip() if rest else ""
    by_name = {t["name"].lower(): t["id"] for t in FORUM_INFO[fid].get("available_tags", [])}
    tag_ids = []
    for word in re.findall(r"(?<![\w#])#([^\s#]+(?:#)?)", "\n".join(rest)):
        tag = by_name.get(word.lower())
        if tag and tag not in tag_ids:
            tag_ids.append(tag)
    while rest and (not rest[-1].strip() or all(
            w.startswith("#") and w[1:].lower() in by_name for w in rest[-1].split())):
        rest.pop()  # a trailing line of tags is not part of the description
    return title, "\n".join(rest).strip(), tag_ids


def reject(fid, ev, reason):
    """Explain a refused !post in a reply; both are removed once the user
    posts successfully."""
    index = S["forums"][fid]["index"]
    notice = send(index, {"msgtype": "m.notice", "body": "⚠️ " + reason}, BOT_MXID,
                  relates={"m.in_reply_to": {"event_id": ev["event_id"]}})
    S["pending"].setdefault(ev["sender"], []).extend([ev["event_id"], notice])
    save_state()


def new_post(fid, sender, title, description, tag_ids):
    """Create a Discord post through the webhook under the sender's Matrix
    name and avatar, then its room (the sender joined) and its card.
    Raises PostError when the post is refused."""
    wait = S["cooldown"].get(sender, 0) + POST_COOLDOWN - time.time()
    if wait > 0:
        raise PostError(f"You can create one post every {POST_COOLDOWN // 60} minutes; "
                        f"try again in {int(wait // 60) + 1} min.")
    title = " ".join(title.split())[:100]
    if not title:
        raise PostError("Your post needs a title: !post Your title")
    allowed = {t["id"] for t in postable_tags(fid)}
    tag_ids = [t for t in dict.fromkeys(tag_ids) if t in allowed][:5]
    if FORUM_INFO[fid].get("flags", 0) & 16 and not tag_ids:  # REQUIRE_TAG
        names = ", ".join("#" + t["name"] for t in postable_tags(fid))
        raise PostError(f"This forum requires at least one tag: {names}")
    description = description.strip()[:2000]
    index = S["forums"][fid]["index"]
    name, avatar = member_profile(index, sender)
    payload = {"thread_name": title, "content": description or title,
               "applied_tags": tag_ids, "username": webhook_name(name),
               "allowed_mentions": {"parse": []}}
    if avatar:
        payload["avatar_url"] = avatar
    try:
        msg = execute(fid, payload=payload)
    except urllib.error.HTTPError as e:
        log("could not create Discord post:", e.code)
        raise PostError("The post could not be created on Discord, please try again later.")
    tid = msg["channel_id"]
    post = {"forum": fid, "origin": "matrix", "name": title, "tags": tag_ids, "author": name,
            "text": description, "avatar": None, "count": 0,
            "activity": snowflake_ms(msg["id"]), "last": msg["id"]}
    create_post_room(fid, tid, post)
    tags = tag_names(fid, tag_ids)
    body = f"📌 {title}" + (f"\n🏷️ {', '.join(tags)}" if tags else "") + f"\nby {name}"
    formatted = (f"<strong>📌 {html.escape(title)}</strong><br>"
                 + (f"<em>🏷️ {html.escape(', '.join(tags))}</em><br>" if tags else "")
                 + f"by {html.escape(name)}")
    if description:
        body += "\n\n" + description
        formatted += "<br><br>" + to_html(description)
    opening = send(post["room"], {"msgtype": "m.text", "body": body,
                                  "format": "org.matrix.custom.html",
                                  "formatted_body": formatted}, BOT_MXID)
    S["d2e"][msg["id"]] = opening
    post["opening"] = opening
    S["e2d"][opening] = msg["id"]
    link_post(fid, post)
    if sender.endswith(":" + DOMAIN):
        admin_join(post["room"], [sender])
    else:  # the admin API only joins local accounts
        matrix(f"/_matrix/client/v3/rooms/{q(post['room'])}/invite", "POST", {"user_id": sender})
    S["cooldown"][sender] = time.time()
    update_card(fid, tid, post)
    log("post created from Matrix:", title, "by", sender)
    return tid, post


def create_post(fid, ev):
    """A !post from the index room."""
    sender = ev["sender"]
    index = S["forums"][fid]["index"]
    title, description, tag_ids = parse_post(fid, plain_body(ev.get("content") or {}))
    try:
        new_post(fid, sender, title, description, tag_ids)
    except PostError as e:
        return reject(fid, ev, str(e))
    redact(index, ev["event_id"], "Posted: see its card below")
    for old in S["pending"].pop(sender, []):
        try:
            redact(index, old, "Replaced by a successful post")
        except urllib.error.HTTPError:
            pass
    save_state()


def handle_index_event(fid, ev):
    sender = ev["sender"]
    if bridge_user(sender) or ev["type"] != "m.room.message":
        return
    content = ev.get("content") or {}
    rel = content.get("m.relates_to") or {}
    if (content.get("msgtype") == "m.text" and rel.get("rel_type") != "m.replace"
            and plain_body(content).lstrip().startswith("!post")):
        create_post(fid, ev)
    else:
        redact(S["forums"][fid]["index"], ev["event_id"], NOT_A_POST)


def sync_filter():
    rooms = [f["index"] for f in S["forums"].values() if f.get("index")]
    rooms += [p["room"] for p in S["posts"].values()]
    return {"presence": {"not_types": ["*"]}, "account_data": {"not_types": ["*"]},
            "room": {"rooms": rooms,
                     "timeline": {"limit": 50, "types": ["m.room.message", "m.room.redaction"]},
                     "state": {"not_types": ["*"]}, "ephemeral": {"not_types": ["*"]},
                     "account_data": {"not_types": ["*"]}}}


def matrix_sync(timeout_ms):
    """Long-poll the index and post rooms as the bridge bot: !post in the
    index, replies in post rooms. The first sync only takes a position, so
    history from before the daemon started is never acted on. The long poll
    itself runs without LOCK, so the forum view API answers meanwhile."""
    with LOCK:
        since = S.get("since")
        path = (f"/_matrix/client/v3/sync?timeout={timeout_ms if since else 0}"
                f"&filter={q(json.dumps(sync_filter()))}")
    if since:
        path += "&since=" + q(since)
    resp = matrix(path)
    with LOCK:
        handle_sync(since, resp)


def handle_sync(since, resp):
    S["since"] = resp["next_batch"]
    if since:
        indexes = {f["index"]: fid for fid, f in S["forums"].items() if f.get("index")}
        posts = {p["room"]: (tid, p) for tid, p in S["posts"].items()}
        for room, data in resp.get("rooms", {}).get("join", {}).items():
            for ev in data.get("timeline", {}).get("events", []):
                try:
                    if room in indexes:
                        handle_index_event(indexes[room], ev)
                    elif room in posts:
                        relay_event(*posts[room], ev)
                except Exception as e:
                    log("ERROR handling", ev.get("event_id"), repr(e))
                    if room in posts and ev["type"] == "m.room.message" and not bridge_user(ev["sender"]):
                        send(room, {"msgtype": "m.notice",
                                    "body": "⚠️ This message could not be delivered to Discord."},
                             BOT_MXID, relates={"m.in_reply_to": {"event_id": ev["event_id"]}})
    save_state()


# --- forum view API ----------------------------------------------------------

class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def openid_user(token):
    """The local account behind a Matrix OpenID token (asked to Synapse,
    then cached for a few minutes), or None."""
    if not token or len(token) > 512:
        return None
    hit = OPENID.get(token)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        info = request(f"{SYNAPSE}/_matrix/federation/v1/openid/userinfo?access_token={q(token)}")
    except urllib.error.HTTPError:
        return None
    user = info.get("sub") or ""
    if not user.endswith(":" + DOMAIN) or bridge_user(user):
        return None
    if len(OPENID) > 5000:
        OPENID.clear()
    OPENID[token] = (user, time.time() + 300)
    return user


def api_forums():
    """The mirrored forums, and which forum and post each post room is."""
    with LOCK:
        forums = []
        for fid in FORUMS:
            info, forum = FORUM_INFO.get(fid, {}), S["forums"].get(fid, {})
            if not forum.get("index"):
                continue
            postable = {t["id"] for t in postable_tags(fid)}
            forums.append({
                "id": fid, "name": info.get("name", ""), "topic": (info.get("topic") or "").strip(),
                "index": forum["index"], "url": f"https://discord.com/channels/{GUILD}/{fid}",
                "require_tag": bool(info.get("flags", 0) & 16),
                "tags": [{"id": t["id"], "name": t["name"], "emoji": t.get("emoji_name"),
                          "postable": t["id"] in postable} for t in info.get("available_tags", [])]})
        rooms = {p["room"]: {"forum": p["forum"], "post": tid} for tid, p in S["posts"].items()}
    return {"forums": forums, "rooms": rooms}


def api_posts(fid):
    """Every post of a forum, most recently active first."""
    with LOCK:
        if fid not in S["forums"]:
            raise ApiError(404, "Unknown forum.")
        posts = {}
        for tid, entry in CAT["posts"].items():
            if entry["forum"] == fid:
                posts[tid] = {"id": tid, "title": entry["title"], "tags": entry["tags"],
                              "author": entry.get("author") or "", "excerpt": entry.get("excerpt") or "",
                              "created": entry["created"], "activity": entry["activity"],
                              "count": entry["count"], "archived": entry["archived"],
                              "locked": entry["locked"], "room": None}
        for tid, post in S["posts"].items():
            if post["forum"] != fid:
                continue
            item = posts.setdefault(tid, {  # created from Matrix since the last poll
                "id": tid, "title": post["name"], "tags": post.get("tags", []), "excerpt": "",
                "created": snowflake_ms(tid), "activity": post.get("activity", 0),
                "count": post.get("count", 0), "archived": False, "locked": False})
            item["room"] = post["room"]
            item["author"] = item.get("author") or post.get("author") or ""
            item["excerpt"] = item["excerpt"] or excerpt(post.get("text") or "")
            item["activity"] = max(item["activity"], post.get("activity", 0))
    return {"posts": sorted(posts.values(), key=lambda p: -p["activity"])}


def api_open(user, tid):
    """The room of a post, imported now if it has none: the opening message
    and the last OPEN_RECENT replies (a notice links older ones)."""
    with LOCK:
        if tid in S["posts"]:
            return {"room": S["posts"][tid]["room"]}
        entry = CAT["posts"].get(tid)
        if not entry or entry["forum"] not in S["forums"]:
            raise ApiError(404, "This post no longer exists on Discord.")
        recent = [t for t in OPENED.get(user, []) if t > time.time() - OPEN_WINDOW]
        if len(recent) >= OPEN_LIMIT:
            raise ApiError(429, "You opened many older posts in a row, please try again in a few minutes.")
        OPENED[user] = recent + [time.time()]
        try:
            thread = discord(f"/channels/{tid}")
            newest = discord(f"/channels/{tid}/messages?limit={OPEN_RECENT}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ApiError(404, "This post no longer exists on Discord.")
            raise
        cutoff = 0
        if len(newest) >= OPEN_RECENT:
            cutoff = snowflake_ms(min(newest, key=lambda m: int(m["id"]))["id"]) - 1
        import_post(entry["forum"], thread, cutoff)
        log("post opened from the forum view by", user)
        return {"room": S["posts"][tid]["room"]}


def api_new(user, body):
    fid = body.get("forum")
    title, description, tags = body.get("title"), body.get("body", ""), body.get("tags", [])
    if not (isinstance(fid, str) and isinstance(title, str) and isinstance(description, str)
            and isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        raise ApiError(400, "Invalid post.")
    with LOCK:
        if not S["forums"].get(fid, {}).get("index"):
            raise ApiError(404, "Unknown forum.")
        try:
            tid, post = new_post(fid, user, title, description, tags)
        except PostError as e:
            raise ApiError(400, str(e))
        return {"room": post["room"], "post": tid}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CosmosForum"
    sys_version = ""

    def log_message(self, fmt, *args):  # errors are logged by route()
        pass

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        url = urlparse(self.path)
        path = url.path.removeprefix("/forum-api")
        try:
            auth = self.headers.get("Authorization", "")
            user = openid_user(auth[7:]) if auth.startswith("Bearer ") else None
            if not user:
                raise ApiError(401, "Your session could not be checked, please reload the page.")
            if method == "GET" and path == "/forums":
                return self.reply(200, api_forums())
            if method == "GET" and path == "/posts":
                return self.reply(200, api_posts(parse_qs(url.query).get("forum", [""])[0]))
            if method == "POST" and path in ("/open", "/new"):
                length = int(self.headers.get("Content-Length") or 0)
                if length > 16384:
                    raise ApiError(413, "Too long.")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ApiError(400, "Invalid request.")
                if path == "/open":
                    return self.reply(200, api_open(user, str(body.get("post", ""))))
                return self.reply(200, api_new(user, body))
            raise ApiError(404, "Not found.")
        except ApiError as e:
            self.reply(e.code, {"error": str(e)})
        except ValueError:  # malformed JSON or Content-Length
            self.reply(400, {"error": "Invalid request."})
        except Exception as e:
            log("ERROR in the forum view API", method, path, repr(e))
            self.reply(500, {"error": "Something went wrong, please try again later."})


def start_api():
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), ApiHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()


# --- setup -----------------------------------------------------------------

def bridged_spaces():
    """Discord channel id -> Matrix room of the bridge's guild and category
    spaces (found through their m.bridge state)."""
    spaces = {}
    for room in matrix("/_matrix/client/v3/joined_rooms")["joined_rooms"]:
        try:
            state = matrix(f"/_matrix/client/v3/rooms/{q(room)}/state")
        except urllib.error.HTTPError:
            continue
        create_type = cid = None
        for ev in state:
            if ev["type"] == "m.room.create":
                create_type = ev["content"].get("type")
            elif ev["type"] == "m.bridge" and cid is None:
                cid = ev["content"].get("channel", {}).get("id")
        if create_type == "m.space" and cid:
            spaces[cid] = room
    return spaces


def create_index_room(fid):
    """The forum's index room, built like a bridged channel: '#name',
    m.bridge state (so onboarding and /join auto-join new accounts),
    joinable by guild space members. Its first message, pinned, explains
    !post."""
    channel = FORUM_INFO[fid]
    bridge = {"bridgebot": BOT_MXID,
              "protocol": {"id": "discord", "displayname": "Discord",
                           "external_url": "https://discord.com/"},
              "network": {"id": GUILD},
              "channel": {"id": fid, "displayname": channel["name"],
                          "external_url": f"https://discord.com/channels/{GUILD}/{fid}"}}
    key = f"net.gocosmos.forum://discord/{GUILD}/{fid}"
    index = matrix("/_matrix/client/v3/createRoom", "POST", {
        "name": "#" + channel["name"], "topic": index_topic(fid),
        "preset": "private_chat", "visibility": "private",
        "initial_state": restricted_state(S["forums"][fid]["parent"]) + [
            {"type": "m.bridge", "state_key": key, "content": bridge},
            {"type": "uk.half-shot.bridge", "state_key": key, "content": bridge},
        ],
        # the admin API join of existing members invites as the (level 0) admin
        "power_level_content_override": {"invite": 0},
    })["room_id"]
    S["forums"][fid]["index"] = index
    save_state()
    pinned = send(index, instructions(fid), BOT_MXID)
    set_state(index, "m.room.pinned_events", "", {"pinned": [pinned]})
    S["forums"][fid]["howto"] = HOWTO_VERSION
    save_state()
    log(f"index room created for #{channel['name']}:", index)


def update_howto(fid):
    """Rewrite the pinned how-to and the topic of an index room made by an
    earlier version (edits, so nobody is notified)."""
    forum = S["forums"][fid]
    if forum.get("howto") == HOWTO_VERSION:
        return
    try:
        pinned = matrix(f"/_matrix/client/v3/rooms/{q(forum['index'])}/state/m.room.pinned_events/")
    except urllib.error.HTTPError:
        pinned = {}
    for event_id in pinned.get("pinned", [])[:1]:
        edit(forum["index"], event_id, instructions(fid))
    set_state(forum["index"], "m.room.topic", "", {"topic": index_topic(fid)})
    forum["howto"] = HOWTO_VERSION
    save_state()


def migrate_posts(fid):
    """Bring rooms made by earlier versions up to date: post rooms become
    world_readable and cards link to the opening message."""
    for tid, post in S["posts"].items():
        if post["forum"] != fid:
            continue
        if not post.get("readable"):
            set_state(post["room"], "m.room.history_visibility", "",
                      {"history_visibility": "world_readable"})
            post["readable"] = True
        if not post.get("opening") and S["d2e"].get(tid):
            post["opening"] = S["d2e"][tid]
        update_card(fid, tid, post)
    save_state()


def setup_forum(fid, spaces):
    """Create the index, import the history, then link the index into the
    category and join existing members, so nobody sees a half-built list or
    gets unread badges for the backfill. Every step resumes after a
    restart."""
    forum = S["forums"].setdefault(fid, {})
    forum["parent"] = spaces.get(FORUM_INFO[fid].get("parent_id")) or GUILD_SPACE
    if not forum.get("index"):
        create_index_room(fid)
    ensure_webhook(fid)
    if not forum.get("backfilled"):
        backfill_forum(fid)
    if not forum.get("linked"):
        set_state(forum["parent"], "m.space.child", forum["index"], {"via": [DOMAIN]})
        forum["linked"] = True
        save_state()
    update_howto(fid)
    migrate_posts(fid)
    if not forum.get("members_joined"):
        members = matrix(f"/_matrix/client/v3/rooms/{q(forum['parent'])}/joined_members")["joined"]
        users = [u for u in members if not u.startswith("@discord") and u.endswith(":" + DOMAIN)]
        log(f"joined {admin_join(forum['index'], users)} existing member(s) to the index room")
        forum["members_joined"] = True
        save_state()


def refresh_guild():
    ROLES.clear()
    ROLES.update({r["id"]: r["name"] for r in discord(f"/guilds/{GUILD}/roles")})
    CHANNELS.update({c["id"]: c["name"] for c in discord(f"/guilds/{GUILD}/channels")})
    CHANNELS.update({tid: p["name"] for tid, p in S["posts"].items()})
    for fid in FORUMS:
        FORUM_INFO[fid] = discord(f"/channels/{fid}")


def main():
    global S, CAT, GUILD_SPACE
    missing = [k for k in ("DISCORD_BOT_TOKEN", "BRIDGE_AS_TOKEN", "ONBOARD_ADMIN_TOKEN",
                           "GUILD_ID", "FORUM_CHANNELS") if not os.environ.get(k)]
    if missing:
        log("missing env vars:", ", ".join(missing), "- idling")
        while True:
            time.sleep(3600)
    if not PROXY_KEY:
        log("BRIDGE_AVATAR_PROXY_KEY not set: Matrix avatars will not show on Discord")

    S = load_state()
    CAT = load_catalog()
    refresh_guild()
    spaces = bridged_spaces()
    GUILD_SPACE = spaces[GUILD]
    for fid in FORUMS:
        setup_forum(fid, spaces)
    threading.Thread(target=catalog_worker, daemon=True).start()
    start_api()
    log(f"forum mirror up; {len(FORUMS)} forum(s), {len(S['posts'])} post(s), "
        f"forum view API on :{API_PORT}")

    next_poll = next_refresh = 0
    while True:
        if time.time() >= next_refresh:
            try:
                with LOCK:
                    refresh_guild()
                    purge_due()
            except Exception as e:
                log("ERROR refreshing guild info", repr(e))
            next_refresh = time.time() + 3600
        if time.time() >= next_poll:
            try:
                with LOCK:
                    poll_discord()
            except Exception as e:
                log("ERROR polling Discord", repr(e))
            next_poll = time.time() + POLL_SECONDS
        try:
            matrix_sync(int(max(1, next_poll - time.time()) * 1000))
        except Exception as e:
            log("ERROR syncing Matrix", repr(e))
            time.sleep(5)


main()
