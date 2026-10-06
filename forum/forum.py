"""Discord forum channels mirrored to Matrix, one room per post.

mautrix-discord only bridges text and announcement channels, so the forums
listed in FORUM_CHANNELS are mirrored by this daemon instead:
  - each forum becomes a sub-space inside its Discord category's space. It
    carries m.bridge state, so the onboarding daemon and the /join page
    auto-join new accounts to it like any bridged space; accounts already in
    the category space are joined once when the sub-space is created
  - each post becomes a room in that sub-space: name = post title, topic =
    tags, author and Discord link, avatar = first image of the opening
    message. Members of the guild space can join any post, nobody is joined
    automatically, and the sub-space lists posts by latest activity
  - Discord messages are sent by the bridge's own ghosts (@discord_<id>), so
    authors look exactly as they do in the bridged channels
  - Matrix messages, edits and redactions go back to the post through a
    webhook on the forum, under the sender's Matrix name and avatar (served
    by the bridge's avatar proxy), the same look as the bridge's relay mode

The first time a forum is seen, every post with activity in the last
FORUM_BACKFILL_DAYS days is imported: its opening message plus that many
days of replies (a notice links older ones on Discord). A post revived later
is imported the same way. Discord is polled every POLL_SECONDS, so edits and
deletions made on Discord are not mirrored. State lives in /state/forum.json.
Pure stdlib, no dependencies.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote

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
STATE = "/state/forum.json"
UA = "CosmosForum (https://gocosmos.org, 1.0)"
POLL_SECONDS = 30
MATRIX_MAX_UPLOAD = 20 * 1024 * 1024  # synapse max_upload_size
DISCORD_MAX_UPLOAD = 10 * 1024 * 1024  # webhook attachment limit without boosts
BOT_MXID = f"@discordbot:{DOMAIN}"
DISCORD_EPOCH = 1420070400000
MESSAGE_TYPES = {0, 19, 20, 23}  # default, reply, slash command, context menu

S = {}            # persisted state, see load_state()
FORUM_INFO = {}   # forum channel id -> Discord channel object (tags, name)
ROLES = {}        # Discord role id -> name, for <@&id> mentions
CHANNELS = {}     # Discord channel id -> name, for <#id> mentions
GUILD_SPACE = ""  # the bridge's space for the whole guild


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
    # forums: forum id -> {space, webhook, backfilled}
    # posts: thread id -> {forum, room, name, tags, author, last, activity, members}
    # d2e / e2d: Discord message id <-> first Matrix event id, both origins
    # relayed: Matrix event ids sent to Discord through the webhook
    # ghosts: ghost user ids known to exist
    for key in ("forums", "posts", "d2e", "e2d", "relayed", "ghosts"):
        state.setdefault(key, {})
    return state


def save_state():
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S, f)
    os.replace(tmp, STATE)


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

def send(room, content, sender, ts=None, reply_to=None):
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    txn = secrets.token_hex(8)
    return matrix(f"/_matrix/client/v3/rooms/{q(room)}/send/m.room.message/{txn}",
                  "PUT", content, as_user=sender, ts=ts)["event_id"]


def upload(data, ctype, name, sender=BOT_MXID):
    return matrix(f"/_matrix/media/v3/upload?filename={q(name)}", "POST", data,
                  as_user=sender, headers={"Content-Type": ctype})["content_uri"]


def set_state(room, etype, key, content):
    matrix(f"/_matrix/client/v3/rooms/{q(room)}/state/{etype}/{q(key)}", "PUT", content)


def restricted_room_state(parent):
    """Initial state shared by the sub-space and post rooms: joinable by
    guild space members, full history for joiners, a canonical parent."""
    return [
        {"type": "m.room.join_rules", "state_key": "",
         "content": {"join_rule": "restricted",
                     "allow": [{"type": "m.room_membership", "room_id": GUILD_SPACE}]}},
        {"type": "m.room.history_visibility", "state_key": "",
         "content": {"history_visibility": "shared"}},
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


# --- Discord to Matrix -----------------------------------------------------

def send_attachment(room, att, sender, ts, reply_to):
    name = att.get("filename") or "file"
    if att.get("size", 0) > MATRIX_MAX_UPLOAD:
        return send(room, {"msgtype": "m.text", "body": f"📎 {name}: {att['url']}"}, sender, ts, reply_to)
    data = request(att["url"], raw=True)
    ctype = att.get("content_type") or "application/octet-stream"
    kind = ctype.split("/")[0]
    info = {"mimetype": ctype, "size": len(data)}
    if att.get("width") and att.get("height"):
        info.update(w=att["width"], h=att["height"])
    content = {"msgtype": {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(kind, "m.file"),
               "body": name, "filename": name, "url": upload(data, ctype, name, sender), "info": info}
    return send(room, content, sender, ts, reply_to)


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
                                    "formatted_body": to_html(text)}, ghost, ts, reply)
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


def post_meta(fid, thread, author):
    tags = {t["id"]: t["name"] for t in FORUM_INFO[fid].get("available_tags", [])}
    names = [tags[t] for t in thread.get("applied_tags", []) if t in tags]
    topic = []
    if names:
        topic.append("🏷️ " + ", ".join(names))
    if author:
        topic.append("by " + author)
    topic.append(f"https://discord.com/channels/{GUILD}/{thread['id']}")
    return thread["name"][:255], " · ".join(topic)


def order_key(post):
    """Space child order: lexicographically smaller sorts first, so the
    most recently active post comes first."""
    return f"{10 ** 13 - int(post['activity']):013d}"


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


def import_post(fid, thread, cutoff_ms):
    """Create the room of a post and mirror its opening message plus every
    message since cutoff_ms (all of them for posts created after it)."""
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
    name, topic = post_meta(fid, thread, author)
    space = S["forums"][fid]["space"]
    state = restricted_room_state(space)
    avatar = room_avatar(starter)
    if avatar:
        state.append({"type": "m.room.avatar", "state_key": "", "content": {"url": avatar}})
    room = matrix("/_matrix/client/v3/createRoom", "POST", {
        "name": name, "topic": topic, "preset": "private_chat", "visibility": "private",
        "initial_state": state,
    })["room_id"]
    older = snowflake_ms(tid) < cutoff_ms
    post = {"forum": fid, "room": room, "name": thread["name"],
            "tags": thread.get("applied_tags", []), "author": author,
            "last": ms_snowflake(cutoff_ms) if older else tid,
            "activity": snowflake_ms(thread.get("last_message_id") or tid),
            "members": [BOT_MXID]}
    S["posts"][tid] = post
    CHANNELS[tid] = thread["name"]
    save_state()
    log("post room created:", thread["name"], room)
    if starter:
        mirror_message(fid, post, starter)
    if older:
        send(room, {"msgtype": "m.notice",
                    "body": f"Older messages of this post are on Discord: "
                            f"https://discord.com/channels/{GUILD}/{tid}"},
             BOT_MXID, snowflake_ms(tid) + 1)
    mirror_new(fid, tid, post)
    set_state(space, "m.space.child", room, {"via": [DOMAIN], "order": order_key(post)})
    save_state()


def refresh_meta(fid, tid, post, thread):
    """Follow title and tag changes made on Discord."""
    if thread["name"] == post["name"] and thread.get("applied_tags", []) == post["tags"]:
        return
    name, topic = post_meta(fid, thread, post.get("author"))
    set_state(post["room"], "m.room.name", "", {"name": name})
    set_state(post["room"], "m.room.topic", "", {"topic": topic})
    post["name"], post["tags"] = thread["name"], thread.get("applied_tags", [])
    CHANNELS[tid] = thread["name"]
    save_state()


def poll_discord():
    cutoff = time.time() * 1000 - BACKFILL_DAYS * 86400000
    for thread in discord(f"/guilds/{GUILD}/threads/active")["threads"]:
        fid = thread.get("parent_id")
        if fid not in FORUMS:
            continue
        tid = thread["id"]
        post = S["posts"].get(tid)
        try:
            if post is None:  # a new post, or an old one revived by a reply
                import_post(fid, thread, cutoff)
                continue
            refresh_meta(fid, tid, post, thread)
            last = thread.get("last_message_id")
            if last and int(last) > int(post["last"]):
                before = post["activity"]
                mirror_new(fid, tid, post)
                if post["activity"] != before:
                    set_state(S["forums"][fid]["space"], "m.space.child", post["room"],
                              {"via": [DOMAIN], "order": order_key(post)})
        except Exception as e:  # keep the other posts going
            log("ERROR syncing post", thread.get("name"), repr(e))


def backfill_forum(fid):
    """Import every post with activity since the cutoff, active or
    archived, oldest activity first."""
    cutoff = time.time() * 1000 - BACKFILL_DAYS * 86400000
    threads = [t for t in discord(f"/guilds/{GUILD}/threads/active")["threads"]
               if t.get("parent_id") == fid]
    before = None
    while True:
        page = discord(f"/channels/{fid}/threads/archived/public?limit=100"
                       + (f"&before={q(before)}" if before else ""))
        threads += page["threads"]
        if not page.get("has_more") or not page["threads"]:
            break
        before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]
        if iso_ms(before) < cutoff:  # archived before the cutoff: no newer activity
            break
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
                set_state(S["forums"][fid]["space"], "m.space.child", post["room"],
                          {"via": [DOMAIN], "order": order_key(post)})
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
    try:
        member = matrix(f"/_matrix/client/v3/rooms/{q(room)}/state/m.room.member/{q(user)}")
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
    who = f"<@{ghost.group(1)}>" if ghost else member_profile(post["room"], ev["sender"])[0]
    line = plain_body(ev.get("content", {})).strip().split("\n")[0]
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
    forum = S["forums"][fid]
    if not forum.get("webhook"):
        hook = discord(f"/channels/{fid}/webhooks", "POST", {"name": "Matrix"})
        forum["webhook"] = {"id": hook["id"], "token": hook["token"]}
        save_state()
    return forum["webhook"]


def execute(fid, tid, method="POST", message_id=None, payload=None, file=None):
    """Run the forum webhook inside a post. A deleted webhook (Discord error
    10015) is recreated once."""
    for attempt in (1, 2):
        hook = ensure_webhook(fid)
        url = f"https://discord.com/api/v10/webhooks/{hook['id']}/{hook['token']}"
        if message_id:
            url += f"/messages/{message_id}"
        url += f"?thread_id={tid}" + ("&wait=true" if method == "POST" else "")
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
    sender = ev["sender"]
    if sender == BOT_MXID or sender.startswith("@discord_") or ev["event_id"] in S["e2d"]:
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
    target = (rel.get("m.in_reply_to") or {}).get("event_id")
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


def sync_filter():
    return {"presence": {"not_types": ["*"]}, "account_data": {"not_types": ["*"]},
            "room": {"rooms": [p["room"] for p in S["posts"].values()],
                     "timeline": {"limit": 50, "types": ["m.room.message", "m.room.redaction"]},
                     "state": {"not_types": ["*"]}, "ephemeral": {"not_types": ["*"]},
                     "account_data": {"not_types": ["*"]}}}


def matrix_sync(timeout_ms):
    """Long-poll the post rooms as the bridge bot and relay what Matrix
    users wrote. The first sync only takes a position, so history from
    before the daemon started is never relayed."""
    since = S.get("since")
    path = (f"/_matrix/client/v3/sync?timeout={timeout_ms if since else 0}"
            f"&filter={q(json.dumps(sync_filter()))}")
    if since:
        path += "&since=" + q(since)
    resp = matrix(path)
    S["since"] = resp["next_batch"]
    if since:
        rooms = {p["room"]: (tid, p) for tid, p in S["posts"].items()}
        for room, data in resp.get("rooms", {}).get("join", {}).items():
            if room not in rooms:
                continue
            tid, post = rooms[room]
            for ev in data.get("timeline", {}).get("events", []):
                try:
                    relay_event(tid, post, ev)
                except Exception as e:
                    log("ERROR relaying", ev.get("event_id"), repr(e))
                    if ev["type"] == "m.room.message" and not ev["sender"].startswith("@discord"):
                        send(room, {"msgtype": "m.notice",
                                    "body": "⚠️ This message could not be delivered to Discord."},
                             BOT_MXID, reply_to=ev["event_id"])
    save_state()


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


def join_existing_members(space, source):
    """Members of the category space get the new forum as new accounts will.
    The admin API join needs the server admin in the room, so it joins,
    joins everyone, then leaves."""
    admin = matrix("/_matrix/client/v3/account/whoami", token=ADMIN_TOKEN)["user_id"]
    try:
        matrix(f"/_matrix/client/v3/rooms/{q(space)}/invite", "POST", {"user_id": admin})
    except urllib.error.HTTPError as e:
        if e.code != 403:  # still in the room from an interrupted run
            raise
    matrix(f"/_matrix/client/v3/rooms/{q(space)}/join", "POST", {}, token=ADMIN_TOKEN)
    joined = 0
    for user in matrix(f"/_matrix/client/v3/rooms/{q(source)}/joined_members")["joined"]:
        if user.startswith("@discord") or user == admin or not user.endswith(":" + DOMAIN):
            continue
        try:
            matrix(f"/_synapse/admin/v1/join/{q(space)}", "POST", {"user_id": user}, token=ADMIN_TOKEN)
            joined += 1
        except urllib.error.HTTPError as e:
            log("could not join", user, e.code)
    matrix(f"/_matrix/client/v3/rooms/{q(space)}/leave", "POST", {}, token=ADMIN_TOKEN)
    log(f"joined {joined} existing member(s) to the forum space")


def ensure_forum_space(fid, spaces):
    """Create the forum's sub-space once and join existing members to it;
    both steps resume after a restart."""
    forum = S["forums"].setdefault(fid, {})
    channel = FORUM_INFO[fid]
    parent = spaces.get(channel.get("parent_id")) or GUILD_SPACE
    if not forum.get("space"):
        create_forum_space(fid, channel, parent)
    if not forum.get("members_joined"):
        join_existing_members(forum["space"], parent)
        forum["members_joined"] = True
        save_state()


def create_forum_space(fid, channel, parent):
    bridge = {"bridgebot": BOT_MXID,
              "protocol": {"id": "discord", "displayname": "Discord",
                           "external_url": "https://discord.com/"},
              "network": {"id": GUILD},
              "channel": {"id": fid, "displayname": channel["name"],
                          "external_url": f"https://discord.com/channels/{GUILD}/{fid}"}}
    key = f"net.gocosmos.forum://discord/{GUILD}/{fid}"
    state = restricted_room_state(parent) + [
        {"type": "m.bridge", "state_key": key, "content": bridge},
        {"type": "uk.half-shot.bridge", "state_key": key, "content": bridge},
    ]
    space = matrix("/_matrix/client/v3/createRoom", "POST", {
        "name": "#" + channel["name"], "topic": channel.get("topic") or "",
        "preset": "private_chat", "visibility": "private",
        "creation_content": {"type": "m.space"}, "initial_state": state,
        # the admin API join below invites as the (power level 0) admin
        "power_level_content_override": {"invite": 0},
    })["room_id"]
    S["forums"][fid]["space"] = space
    save_state()
    set_state(parent, "m.space.child", space, {"via": [DOMAIN]})
    log(f"forum space created for #{channel['name']}:", space)


def refresh_guild():
    ROLES.clear()
    ROLES.update({r["id"]: r["name"] for r in discord(f"/guilds/{GUILD}/roles")})
    CHANNELS.update({c["id"]: c["name"] for c in discord(f"/guilds/{GUILD}/channels")})
    CHANNELS.update({tid: p["name"] for tid, p in S["posts"].items()})
    for fid in FORUMS:
        FORUM_INFO[fid] = discord(f"/channels/{fid}")


def main():
    global S, GUILD_SPACE
    missing = [k for k in ("DISCORD_BOT_TOKEN", "BRIDGE_AS_TOKEN", "ONBOARD_ADMIN_TOKEN",
                           "GUILD_ID", "FORUM_CHANNELS") if not os.environ.get(k)]
    if missing:
        log("missing env vars:", ", ".join(missing), "- idling")
        while True:
            time.sleep(3600)
    if not PROXY_KEY:
        log("BRIDGE_AVATAR_PROXY_KEY not set: Matrix avatars will not show on Discord")

    S = load_state()
    refresh_guild()
    spaces = bridged_spaces()
    GUILD_SPACE = spaces[GUILD]
    for fid in FORUMS:
        ensure_forum_space(fid, spaces)
        ensure_webhook(fid)
        if not S["forums"][fid].get("backfilled"):
            backfill_forum(fid)
    log(f"forum mirror up; {len(FORUMS)} forum(s), {len(S['posts'])} post room(s)")

    next_poll = next_refresh = 0
    while True:
        if time.time() >= next_refresh:
            try:
                refresh_guild()
            except Exception as e:
                log("ERROR refreshing guild info", repr(e))
            next_refresh = time.time() + 3600
        if time.time() >= next_poll:
            try:
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
