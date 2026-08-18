"""Public registration page for chat.gocosmos.org/join.

Serves a small signup form protected by an ALTCHA proof-of-work captcha
(self-hosted, no third-party calls; widget vendored in altcha.js). On a
solved challenge it creates the Matrix account through Synapse's
shared-secret registration endpoint, which bypasses the invite-token
requirement that keeps the raw client API closed to bots.

Endpoints (Caddy proxies chat.gocosmos.org/join* here):
  GET  /join            the signup page
  GET  /join/altcha.js  the vendored ALTCHA widget (MIT)
  GET  /join/challenge  a fresh HMAC-signed proof-of-work challenge
  POST /join/register   verify the solution, create the account

Bot protection is layered: proof-of-work (cost per attempt), a honeypot
field, per-IP and global rate limits, and challenge expiry + single use.
Pure stdlib, no dependencies, no persistent state.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote

SYNAPSE = os.environ.get("SYNAPSE_URL", "http://synapse:8008")
DOMAIN = os.environ.get("MATRIX_DOMAIN", "gocosmos.org")
REG_SECRET = os.environ.get("REGISTRATION_SHARED_SECRET", "")
# Same tokens the onboarding daemon uses: the bridge bot invites the new
# account into every bridged room and the admin API lifts the join ratelimit
ADMIN_TOKEN = os.environ.get("ONBOARD_ADMIN_TOKEN", "")
AS_TOKEN = os.environ.get("BRIDGE_AS_TOKEN", "")
BOT_MXID = f"@discordbot:{DOMAIN}"
# Discord credentials to compute channel visibility: /join accounts have no
# Discord identity, so they only get channels @everyone can see
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD = os.environ.get("GUILD_ID", "")
UA = "CosmosOnboarding (https://gocosmos.org, 1.0)"
# Challenges only need to outlive their 10 minute expiry, so a per-boot
# random key is fine (a restart just voids outstanding challenges).
HMAC_KEY = os.environ.get("JOIN_HMAC_KEY") or secrets.token_hex(32)

MAX_NUMBER = 250_000          # average solve: ~125k hashes, a few seconds
CHALLENGE_TTL = 600           # seconds a challenge stays valid
USERNAME_RE = re.compile(r"^[a-z0-9._=-]{2,32}$")
RESERVED = {"admin", "root", "matrix", "system", "support", "help", "abuse",
            "moderator", "cosmos", "discordbot", "cosmosbridge", "onboarding"}
PASSWORD_MIN = 12             # keep in sync with homeserver.yaml password policy

APP = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(APP, "index.html"), "rb") as f:
    INDEX = f.read()
with open(os.path.join(APP, "altcha.js"), "rb") as f:
    WIDGET = f.read()

lock = threading.Lock()
used_challenges = {}          # challenge hex -> expiry ts (anti-replay)
buckets = {}                  # (kind, ip) -> [timestamps]
LIMITS = {"challenge": (30, 600), "attempt": (10, 3600), "created": (3, 86400)}
GLOBAL_CREATED = (100, 86400)


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def allowed(kind, ip):
    max_hits, window = LIMITS[kind]
    now = time.time()
    with lock:
        hits = buckets.setdefault((kind, ip), [])
        hits[:] = [t for t in hits if now - t < window]
        if len(hits) >= max_hits:
            return False
        if kind == "created":
            g_max, g_window = GLOBAL_CREATED
            g = buckets.setdefault(("created", "*"), [])
            g[:] = [t for t in g if now - t < g_window]
            if len(g) >= g_max:
                return False
        hits.append(now)
        if kind == "created":
            g.append(now)
        return True


def make_challenge():
    salt = f"{secrets.token_hex(12)}?expires={int(time.time()) + CHALLENGE_TTL}"
    number = secrets.randbelow(MAX_NUMBER)
    challenge = hashlib.sha256((salt + str(number)).encode()).hexdigest()
    signature = hmac.new(HMAC_KEY.encode(), challenge.encode(),
                         hashlib.sha256).hexdigest()
    return {"algorithm": "SHA-256", "challenge": challenge, "salt": salt,
            "signature": signature, "maxnumber": MAX_NUMBER}


def verify_captcha(payload_b64):
    """Standard ALTCHA server verification; returns the challenge or None."""
    try:
        p = json.loads(b64decode(payload_b64))
        salt, number = p["salt"], int(p["number"])
        challenge, signature = p["challenge"], p["signature"]
    except Exception:
        return None
    if p.get("algorithm") != "SHA-256" or not (0 <= number <= MAX_NUMBER):
        return None
    expires = parse_qs(salt.partition("?")[2]).get("expires", ["0"])[0]
    if int(expires or 0) < time.time():
        return None
    expected = hashlib.sha256((salt + str(number)).encode()).hexdigest()
    good_sig = hmac.new(HMAC_KEY.encode(), challenge.encode(),
                        hashlib.sha256).hexdigest()
    if not (hmac.compare_digest(expected, challenge)
            and hmac.compare_digest(good_sig, signature)):
        return None
    with lock:
        now = time.time()
        for c in [c for c, exp in used_challenges.items() if exp < now]:
            del used_challenges[c]
        if challenge in used_challenges:
            return None
    return challenge


def synapse_register(username, password):
    """Shared-secret registration (/_synapse/admin/v1/register).

    Returns the response, which includes an access token for the new user.
    """
    def call(method, body=None):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(SYNAPSE + "/_synapse/admin/v1/register",
                                     data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    nonce = call("GET")["nonce"]
    mac = hmac.new(REG_SECRET.encode(),
                   b"\0".join([nonce.encode(), username.encode(),
                               password.encode(), b"notadmin"]),
                   hashlib.sha1).hexdigest()
    return call("POST", {"nonce": nonce, "username": username,
                         "password": password, "admin": False, "mac": mac})


def matrix(path, method="GET", body=None, token=None, as_user=None):
    # as_user: appservice impersonation; the registration's sender_localpart is
    # a random user, so acting as the bridge bot needs an explicit user_id.
    if as_user:
        path += ("&" if "?" in path else "?") + "user_id=" + quote(as_user, safe="")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SYNAPSE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = resp.read()
        return json.loads(payload) if payload else {}


def discord(path):
    req = urllib.request.Request("https://discord.com/api/v10" + path,
                                 headers={"Authorization": "Bot " + DISCORD_TOKEN,
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


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


def bridged_rooms(visible):
    """Bridged rooms (channel portals + guild/category spaces) the target
    user may see per Discord permissions. visible is the permitted Discord
    channel id set, or None for see-everything. Rooms without bridge info
    state (the bridge's personal spaces) are never included."""
    rooms = []
    for room in matrix("/_matrix/client/v3/joined_rooms", token=AS_TOKEN,
                       as_user=BOT_MXID)["joined_rooms"]:
        rq = quote(room, safe="")
        try:
            state = matrix(f"/_matrix/client/v3/rooms/{rq}/state",
                           token=AS_TOKEN, as_user=BOT_MXID)
        except urllib.error.HTTPError:
            continue
        create_type = name = cid = None
        for ev in state:
            if ev["type"] == "m.room.create":
                create_type = ev["content"].get("type")
            elif ev["type"] == "m.room.name":
                name = ev["content"].get("name", "")
            elif ev["type"] in ("m.bridge", "uk.half-shot.bridge") and cid is None:
                cid = ev["content"].get("channel", {}).get("id")
        if cid is None:
            continue
        if create_type == "m.space":
            if cid == GUILD or visible is None or cid in visible:
                rooms.append((room, "space"))
        elif (name or "").startswith("#"):
            if visible is None or cid in visible:
                rooms.append((room, name))
    return rooms


def auto_join(mxid, user_token):
    """Invite the account to every bridged room as the bridge bot and accept
    each invite, mirroring the Discord reaction onboarding. Runs in a
    background thread so the signup response stays instant."""
    visible = visible_channel_ids()  # no Discord identity: @everyone only
    override = f"/_synapse/admin/v1/users/{quote(mxid, safe='')}/override_ratelimit"
    matrix(override, "POST", {"messages_per_second": 0, "burst_count": 0},
           token=ADMIN_TOKEN)
    joined = 0
    try:
        for room, name in bridged_rooms(visible):
            rq = quote(room, safe="")
            try:
                matrix(f"/_matrix/client/v3/rooms/{rq}/invite", "POST",
                       {"user_id": mxid}, token=AS_TOKEN, as_user=BOT_MXID)
            except urllib.error.HTTPError as e:
                log("invite failed:", name, e.code)
            for _ in range(6):
                try:
                    matrix(f"/_matrix/client/v3/rooms/{rq}/join", "POST", {},
                           token=user_token)
                    joined += 1
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        try:
                            wait = json.loads(e.read()).get("retry_after_ms", 2000) / 1000
                        except Exception:
                            wait = 2
                        time.sleep(min(wait + 0.1, 15))
                        continue
                    log("join failed:", name, e.code)
                    break
            else:
                log("join gave up after retries:", name)
    finally:
        try:
            matrix(override, "DELETE", token=ADMIN_TOKEN)
        except Exception as e:
            log("ratelimit override cleanup failed:", repr(e))
        try:  # drop the registration session; the user logs in on their own
            matrix("/_matrix/client/v3/logout", "POST", {}, token=user_token)
        except Exception:
            pass
    log(f"{mxid} auto-joined {joined} bridged rooms")


def auto_join_safe(mxid, user_token):
    try:
        auto_join(mxid, user_token)
    except Exception as e:
        log("ERROR auto-joining", mxid, repr(e))


def register(body, ip):
    if body.get("website"):  # honeypot: humans never see this field
        log("honeypot hit from", ip)
        return 400, "Registration failed."
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    if not USERNAME_RE.match(username):
        return 400, ("Username must be 2-32 characters: lowercase letters, "
                     "digits, dots, dashes or underscores.")
    if username in RESERVED or username.startswith("discord_"):
        return 400, "This username is reserved."
    if len(password) < PASSWORD_MIN:
        return 400, f"Password must be at least {PASSWORD_MIN} characters."
    challenge = verify_captcha(body.get("altcha") or "")
    if not challenge:
        return 400, ("Anti-bot check failed or expired. Reload the page "
                     "and try again.")
    if not allowed("created", ip):
        return 429, "Too many accounts created recently. Try again later."
    try:
        created = synapse_register(username, password)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except Exception:
            err = {}
        if err.get("errcode") == "M_USER_IN_USE":
            return 400, "This username is already taken."
        log("synapse register error:", e.code, err)
        return 502, err.get("error") or "Account creation failed. Try again later."
    with lock:
        used_challenges[challenge] = time.time() + CHALLENGE_TTL
    mxid = f"@{username}:{DOMAIN}"
    log(f"account created: {mxid} (ip {ip})")
    if (AS_TOKEN and ADMIN_TOKEN and DISCORD_TOKEN and GUILD
            and created.get("access_token")):
        threading.Thread(target=auto_join_safe,
                         args=(mxid, created["access_token"]),
                         daemon=True).start()
    return 200, None


class Handler(BaseHTTPRequestHandler):
    server_version = "CosmosJoin/1.0"

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() or self.client_address[0]

    def reply(self, code, body, ctype="application/json", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/join"):
            self.reply(200, INDEX, "text/html; charset=utf-8")
        elif path == "/join/altcha.js":
            self.reply(200, WIDGET, "application/javascript", cache=True)
        elif path == "/join/challenge":
            if not allowed("challenge", self.client_ip()):
                self.reply(429, b'{"error":"rate limited"}')
            else:
                self.reply(200, json.dumps(make_challenge()).encode())
        else:
            self.reply(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/join/register":
            return self.reply(404, b"not found", "text/plain")
        ip = self.client_ip()
        if not allowed("attempt", ip):
            return self.reply(429, json.dumps(
                {"error": "Too many attempts. Try again later."}).encode())
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 65536)
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self.reply(400, b'{"error":"bad request"}')
        try:
            code, error = register(body, ip)
        except Exception as e:
            log("ERROR:", repr(e))
            code, error = 502, "Server error. Try again later."
        self.reply(code, json.dumps({"error": error} if error
                                    else {"ok": True}).encode())

    def log_message(self, fmt, *args):  # requests are logged where useful
        pass


if not REG_SECRET:
    log("REGISTRATION_SHARED_SECRET not set - idling")
    while True:
        time.sleep(3600)

if not (AS_TOKEN and ADMIN_TOKEN and DISCORD_TOKEN and GUILD):
    log("BRIDGE_AS_TOKEN / ONBOARD_ADMIN_TOKEN / DISCORD_BOT_TOKEN / GUILD_ID "
        "not all set - auto-join disabled")
log(f"join page up on :8080 for {DOMAIN} (synapse: {SYNAPSE})")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
