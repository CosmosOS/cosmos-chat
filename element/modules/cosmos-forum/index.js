/*
 * Cosmos forum view: a Discord-style forum for the forum channels that
 * forum/forum.py mirrors (#cosmos-projects, #cosmos-help, #other-projects).
 *
 * Element loads this file at startup (the Element image adds every
 * /modules/<name>/index.js to config.json "modules", see compose.yml) and
 * waits for it before the app starts, so nothing here may throw out of
 * load(): each hook is installed on its own, and a failure only disables
 * that part of the view. React comes from Element (window.React).
 *
 *  - a forum's room (the index of post cards) shows the forum instead of
 *    its timeline, under Element's own room header and next to the room
 *    list: every post of the Discord forum, active or archived, with search,
 *    tag filters, sorting and a New post form
 *  - a post opens as a normal room, in the space you are in: it is joined
 *    on open and left when you move on unless you wrote or reacted there
 *    (see enterPost); posts that have no room yet are brought over from
 *    Discord on the spot by the forum mirror
 *  - post rooms get a "back to the forum" button in their header, forum
 *    rooms a Chat view / Forum view toggle
 *
 * The forum is mounted inside Element's room view (.mx_RoomView_body), over
 * the timeline and composer it hides: plugin pages of their own hide the
 * room list. If Element ever renames that element, the forum room simply
 * shows its timeline of cards again.
 *
 * Data comes from the forum mirror's API at /forum-api/, which checks who is
 * asking with a Matrix OpenID token, the same sign-in widgets use.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

const API = "/forum-api";
const PAGE_SIZE = 40;
const POSTS_TTL = 60 * 1000;
const FORUMS_TTL = 5 * 60 * 1000;
const STYLE_ID = "cosmos-forum-style";

const h = (...args) => window.React.createElement(...args);

// --- data -------------------------------------------------------------------

const store = {
    api: null,
    forums: [],
    byId: new Map(),
    byIndex: new Map(),
    rooms: {},          // post room id -> {forum, post}
    posts: new Map(),   // forum id -> {at, list, error}
    error: null,
    loaded: false,
    classic: null,      // index room the user chose to see as a chat room
    views: new Map(),   // forum id -> {query, tags, sort, shown, scroll}, kept across visits
    joinedToRead: new Set(),   // posts joined on open, see enterPost()
    token: null,        // {value, expires}
    listeners: new Set(),
};

function changed() {
    for (const fn of store.listeners) {
        try {
            fn();
        } catch (e) {
            console.error("cosmos-forum: listener failed", e);
        }
    }
}

function client() {
    const peg = window.mxMatrixClientPeg;
    return peg && typeof peg.get === "function" ? peg.get() : null;
}

async function openidToken() {
    const cli = client();
    if (!cli) throw new Error("Not signed in yet.");
    const user = cli.getUserId();
    if (store.token && store.token.user === user && store.token.expires > Date.now()) return store.token.value;
    const res = await cli.getOpenIdToken();
    store.token = {
        user,
        value: res.access_token,
        expires: Date.now() + Math.max(60, (res.expires_in || 300) - 60) * 1000,
    };
    return store.token.value;
}

async function call(path, body, retry = true) {
    const token = await openidToken();
    let res;
    try {
        res = await fetch(API + path, {
            method: body ? "POST" : "GET",
            headers: body
                ? { Authorization: "Bearer " + token, "Content-Type": "application/json" }
                : { Authorization: "Bearer " + token },
            body: body ? JSON.stringify(body) : undefined,
        });
    } catch (e) {
        throw new Error("The forum service can't be reached right now.");
    }
    if (res.status === 401 && retry) {
        store.token = null;
        return call(path, body, false);
    }
    let data = {};
    try {
        data = await res.json();
    } catch (e) {
        // a proxy error page, not our JSON
    }
    if (!res.ok) {
        if (data.error) throw new Error(data.error);
        if (res.status >= 502) throw new Error("The forum service is restarting, try again in a minute.");
        throw new Error("Request failed (" + res.status + ").");
    }
    return data;
}

let forumsAt = 0;
let forumsLoading = null;

function loadForums(force = false) {
    if (forumsLoading) return forumsLoading;
    if (!force && store.loaded && Date.now() - forumsAt < FORUMS_TTL) return Promise.resolve();
    forumsLoading = call("/forums")
        .then((data) => {
            store.forums = data.forums || [];
            store.byId = new Map(store.forums.map((f) => [f.id, f]));
            store.byIndex = new Map(store.forums.map((f) => [f.index, f]));
            store.rooms = data.rooms || {};
            store.error = null;
            store.loaded = true;
            forumsAt = Date.now();
        })
        .catch((e) => {
            store.error = e.message;
        })
        .finally(() => {
            forumsLoading = null;
            changed();
        });
    return forumsLoading;
}

function loadPosts(fid, force = false) {
    const cached = store.posts.get(fid);
    if (cached && cached.loading) return cached.loading;
    if (!force && cached && cached.list && Date.now() - cached.at < POSTS_TTL) return Promise.resolve();
    const entry = cached || { at: 0, list: null, error: null };
    entry.loading = call("/posts?forum=" + encodeURIComponent(fid))
        .then((data) => {
            entry.list = data.posts || [];
            entry.at = Date.now();
            entry.error = null;
        })
        .catch((e) => {
            entry.error = e.message;
        })
        .finally(() => {
            entry.loading = null;
            changed();
        });
    store.posts.set(fid, entry);
    changed();
    return entry.loading;
}

// --- navigation ---------------------------------------------------------------

/**
 * Open a room without leaving the current space. Element moves you to a
 * space that holds the room you open, and a room just created and not
 * synced yet is in none of yours. A context switch, which is how Element
 * itself opens a space's last room, keeps the space; the module API's
 * openRoom has no such option, hence Element's dispatcher, with openRoom as
 * the fallback.
 */
function showRoom(roomId) {
    const server = roomId.includes(":") ? roomId.slice(roomId.indexOf(":") + 1) : null;
    const dispatcher = window.mxDispatcher;
    if (dispatcher && typeof dispatcher.dispatch === "function") {
        dispatcher.dispatch({
            action: "view_room",
            room_id: roomId,
            via_servers: server ? [server] : undefined,
            context_switch: true,
            metricsTrigger: undefined,
        });
        return;
    }
    store.api.navigation.openRoom(roomId, server ? { viaServers: [server] } : {});
}

/**
 * Resolves once this client has synced its own join to roomId (or after
 * timeoutMs). Element re-picks the space when the room you are viewing gets
 * joined, and a post just created is not in your space's list yet: opening
 * it only after the join keeps you where you are.
 */
function waitForJoin(roomId, timeoutMs = 10000) {
    return new Promise((resolve) => {
        const deadline = Date.now() + timeoutMs;
        const tick = () => {
            const cli = client();
            const room = cli && cli.getRoom(roomId);
            if ((room && room.getMyMembership() === "join") || Date.now() > deadline) return resolve();
            setTimeout(tick, 250);
        };
        tick();
    });
}

/**
 * Open a post. Element keeps you in a space only for rooms you have joined:
 * a post you merely preview belongs to none of your spaces (Element ignores
 * a room's own m.space.parent unless you may manage that space), so the
 * next room list update sends you to Home. Posts are therefore joined on
 * open, which also lets you reply at once, and left again when you move on
 * without having written or reacted there (see leaveIfOnlyRead), so reading
 * a post does not follow it, like on Discord.
 */
async function enterPost(roomId) {
    const cli = client();
    const room = cli && cli.getRoom(roomId);
    if (cli && (!room || room.getMyMembership() !== "join")) {
        try {
            const server = roomId.includes(":") ? roomId.slice(roomId.indexOf(":") + 1) : null;
            await cli.joinRoom(roomId, server ? { viaServers: [server] } : {});
            store.joinedToRead.add(roomId);
            await waitForJoin(roomId);
        } catch (e) {
            // not allowed to join (not in the CosmosOS space): preview it instead
            console.warn("cosmos-forum: could not join " + roomId + ", previewing it", e);
        }
    }
    showRoom(roomId);
}

/** A post joined only to read it, left once the user is elsewhere. */
function leaveIfOnlyRead(roomId) {
    store.joinedToRead.delete(roomId);
    const cli = client();
    const room = cli && cli.getRoom(roomId);
    if (!room || room.getMyMembership() !== "join") return;
    const me = cli.getUserId();
    const wrote = room.getLiveTimeline().getEvents().some((ev) => ev.getSender() === me && !ev.isState());
    if (!wrote) cli.leave(roomId).catch((e) => console.warn("cosmos-forum: could not leave " + roomId, e));
}

function onRoomChange() {
    const match = /^#\/room\/([^/?]+)/.exec(location.hash);
    const current = match ? decodeURIComponent(match[1]) : null;
    for (const roomId of [...store.joinedToRead]) {
        if (roomId !== current) leaveIfOnlyRead(roomId);
    }
}

function showForum(forum) {
    store.classic = null;
    changed();
    showRoom(forum.index);
}

/** The forum whose index room Element is showing, from the URL. */
function viewedForum() {
    const match = /^#\/room\/([^/?]+)/.exec(location.hash);
    if (!match) return null;
    const target = decodeURIComponent(match[1]);
    if (store.byIndex.has(target)) return store.byIndex.get(target);
    if (!target.startsWith("#")) return null;
    const cli = client();
    for (const forum of store.forums) {
        const room = cli && cli.getRoom(forum.index);
        if (room && (room.getCanonicalAlias() === target || room.getAltAliases().includes(target))) return forum;
    }
    return null;
}

// --- the forum, mounted in the index room's view ---------------------------------

let mounted = null;   // {forum, el, root}
let syncQueued = false;

function queueSync() {
    if (syncQueued) return;
    syncQueued = true;
    requestAnimationFrame(() => {
        syncQueued = false;
        try {
            syncForum();
        } catch (e) {
            console.error("cosmos-forum: could not show the forum", e);
        }
    });
}

function syncForum() {
    const forum = viewedForum();
    // the chat view lasts until the user leaves the room
    if (store.classic && (!forum || forum.index !== store.classic)) store.classic = null;
    const wanted = forum && store.classic !== forum.index ? forum : null;
    if (mounted && (!wanted || mounted.forum.id !== wanted.id || !mounted.el.isConnected)) {
        const old = mounted;
        mounted = null;
        old.root.unmount();
        old.el.remove();
    }
    if (!wanted || mounted) return;
    const body = document.querySelector(".mx_RoomView_wrapper .mx_RoomView_body");
    if (!body || !body.querySelector(":scope > .mx_RoomHeader")) return;  // not rendered yet
    const el = document.createElement("div");
    el.className = "cf_mount";
    // Element sends printable keys typed outside an input to the composer,
    // hidden under the forum: keep them here (shortcuts with Ctrl/Cmd still work)
    el.addEventListener("keydown", (ev) => {
        if (!ev.ctrlKey && !ev.metaKey) ev.stopPropagation();
    });
    body.appendChild(el);
    const root = store.api.createRoot(el);
    root.render(h(ForumView, { forum: wanted }));
    mounted = { forum: wanted, el, root };
    markIndexRead(wanted.index);
}

function markIndexRead(roomId) {
    try {
        const cli = client();
        const room = cli && cli.getRoom(roomId);
        if (!room) return;
        const events = room.getLiveTimeline().getEvents();
        for (let i = events.length - 1; i >= 0; i--) {
            const ev = events[i];
            if (ev.isState() || !ev.getId() || !ev.getId().startsWith("$")) continue;
            if (!room.hasUserReadEvent(cli.getUserId(), ev.getId())) cli.sendReadReceipt(ev).catch(() => {});
            return;
        }
    } catch (e) {
        // unread badge stays: harmless
    }
}

// --- helpers ------------------------------------------------------------------

const RELATIVE = typeof Intl !== "undefined" && Intl.RelativeTimeFormat
    ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
    : null;

function ago(ms) {
    const seconds = (ms - Date.now()) / 1000;
    const units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
    for (const [unit, size] of units) {
        if (Math.abs(seconds) >= size) {
            const n = Math.round(seconds / size);
            return RELATIVE ? RELATIVE.format(n, unit) : `${Math.abs(n)} ${unit}(s) ago`;
        }
    }
    return RELATIVE ? RELATIVE.format(0, "minute") : "just now";
}

function tagLabel(tag) {
    return (tag.emoji ? tag.emoji + " " : "") + tag.name;
}

const SORTS = {
    activity: { label: "Recent activity", key: (p) => -p.activity },
    created: { label: "Newest posts", key: (p) => -p.created },
    replies: { label: "Most replies", key: (p) => -p.count },
};

// --- components ---------------------------------------------------------------

function useStore() {
    const { useState, useEffect } = window.React;
    const [, setTick] = useState(0);
    useEffect(() => {
        const fn = () => setTick((n) => n + 1);
        store.listeners.add(fn);
        return () => store.listeners.delete(fn);
    }, []);
}

function ForumView({ forum }) {
    useStore();
    const current = store.byId.get(forum.id) || forum;
    return h("div", { className: "cf_page" }, h(Forum, { forum: current }));
}

function Forum({ forum }) {
    const { useState, useEffect, useMemo, useRef } = window.React;
    const saved = store.views.get(forum.id) || {};
    const [query, setQuery] = useState(saved.query || "");
    const [tags, setTags] = useState(saved.tags || []);
    const [sort, setSort] = useState(saved.sort || "activity");
    const [shown, setShown] = useState(saved.shown || PAGE_SIZE);
    const [composing, setComposing] = useState(false);
    const [opening, setOpening] = useState(null);   // post id being brought over
    const [failed, setFailed] = useState({});       // post id -> error
    const page = useRef(null);
    const first = useRef(true);
    const alive = useRef(true);   // still on this forum when a slow request ends
    useEffect(() => () => {
        alive.current = false;
    }, []);

    useEffect(() => {
        loadPosts(forum.id);
        const timer = setInterval(() => loadPosts(forum.id), POSTS_TTL);
        return () => clearInterval(timer);
    }, [forum.id]);
    useEffect(() => {
        if (first.current) return;
        setShown(PAGE_SIZE);
    }, [query, tags, sort]);
    useEffect(() => {
        store.views.set(forum.id, { ...(store.views.get(forum.id) || {}), query, tags, sort, shown });
    }, [query, tags, sort, shown]);

    const entry = store.posts.get(forum.id);
    const all = (entry && entry.list) || [];
    // back from a post: restore the scroll position once the list is there
    useEffect(() => {
        const scroller = page.current && page.current.closest(".cf_page");
        if (!scroller || !all.length) return;
        if (first.current) {
            first.current = false;
            scroller.scrollTop = saved.scroll || 0;
        }
        const onScroll = () => {
            store.views.set(forum.id, { ...(store.views.get(forum.id) || {}), scroll: scroller.scrollTop });
        };
        scroller.addEventListener("scroll", onScroll, { passive: true });
        return () => scroller.removeEventListener("scroll", onScroll);
    }, [all.length > 0]);

    const tagById = useMemo(() => new Map(forum.tags.map((t) => [t.id, t])), [forum]);
    const visible = useMemo(() => {
        const words = query.toLowerCase().split(/\s+/).filter(Boolean);
        const list = all.filter((p) => {
            if (tags.length && !tags.some((t) => p.tags.includes(t))) return false;
            if (!words.length) return true;
            const text = (p.title + " " + p.excerpt + " " + p.author).toLowerCase();
            return words.every((w) => text.includes(w));
        });
        const key = SORTS[sort].key;
        return list.sort((a, b) => key(a) - key(b));
    }, [all, query, tags, sort]);

    const open = async (post) => {
        setFailed((f) => ({ ...f, [post.id]: null }));
        setOpening(post.id);
        try {
            if (!post.room) {
                const { room } = await call("/open", { post: post.id });
                post.room = room;
                store.rooms[room] = { forum: forum.id, post: post.id };
            }
            if (alive.current) await enterPost(post.room);
        } catch (e) {
            if (alive.current) setFailed((f) => ({ ...f, [post.id]: e.message }));
        } finally {
            if (alive.current) setOpening(null);
        }
    };

    const toggleTag = (id) => setTags((t) => (t.includes(id) ? t.filter((x) => x !== id) : [...t, id]));

    return h("div", { className: "cf_inner", ref: page },
        h("div", { className: "cf_toolbar" },
            h("input", {
                className: "cf_search", type: "search", value: query, placeholder: "Search posts",
                "aria-label": "Search posts", onChange: (e) => setQuery(e.target.value),
            }),
            h("select", {
                className: "cf_sort", value: sort, "aria-label": "Sort posts",
                onChange: (e) => setSort(e.target.value),
            }, Object.entries(SORTS).map(([id, s]) => h("option", { key: id, value: id }, s.label))),
            !composing && h("button", { className: "cf_button cf_primary", onClick: () => setComposing(true) },
                "New post")),
        composing && h(NewPost, {
            forum,
            onCancel: () => setComposing(false),
            onDone: (room) => {
                loadPosts(forum.id, true);
                loadForums(true);
                if (!alive.current) return;
                setComposing(false);
                showRoom(room);
            },
        }),
        forum.tags.length > 0 && h("div", { className: "cf_tags", role: "group", "aria-label": "Filter by tag" },
            forum.tags.map((t) => h("button", {
                key: t.id, className: "cf_tag" + (tags.includes(t.id) ? " cf_tag_on" : ""),
                "aria-pressed": tags.includes(t.id), onClick: () => toggleTag(t.id),
            }, tagLabel(t)))),
        entry && entry.error && h("div", { className: "cf_error" }, entry.error),
        !entry || (!entry.list && !entry.error)
            ? h("div", { className: "cf_empty" }, "Loading posts…")
            : h(window.React.Fragment, null,
                h("div", { className: "cf_count" },
                    visible.length === all.length
                        ? `${all.length} post${all.length === 1 ? "" : "s"}`
                        : `${visible.length} of ${all.length} posts`),
                visible.length === 0 && h("div", { className: "cf_empty" }, "No post matches."),
                h("ul", { className: "cf_list" }, visible.slice(0, shown).map((post) => h(PostCard, {
                    key: post.id, post, tagById, busy: opening === post.id, error: failed[post.id],
                    disabled: opening !== null && opening !== post.id, onOpen: () => open(post),
                }))),
                visible.length > shown && h("button", {
                    className: "cf_button cf_more", onClick: () => setShown(shown + PAGE_SIZE),
                }, `Show more (${visible.length - shown} left)`)));
}

function PostCard({ post, tagById, busy, error, disabled, onOpen }) {
    const onKey = (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onOpen();
        }
    };
    const tags = post.tags.map((id) => tagById.get(id)).filter(Boolean);
    return h("li", {
        className: "cf_card" + (busy ? " cf_busy" : "") + (disabled ? " cf_disabled" : ""),
        role: "button", tabIndex: 0, "aria-busy": busy,
        onClick: disabled || busy ? undefined : onOpen, onKeyDown: disabled || busy ? undefined : onKey,
    },
        h("div", { className: "cf_card_title" }, post.locked ? "🔒 " : "", post.title),
        tags.length > 0 && h("div", { className: "cf_card_tags" },
            tags.map((t) => h("span", { key: t.id, className: "cf_chip" }, tagLabel(t)))),
        (post.author || post.excerpt) && h("div", { className: "cf_card_excerpt" },
            post.author && h("strong", null, post.author), post.author && post.excerpt ? ": " : "", post.excerpt),
        h("div", { className: "cf_card_meta" },
            h("span", null, "💬 " + post.count),
            h("span", null, ago(post.activity)),
            busy && h("span", { className: "cf_note" }, post.room ? "Opening…" : "Bringing this post over from Discord…")),
        error && h("div", { className: "cf_error" }, error));
}

function NewPost({ forum, onCancel, onDone }) {
    const { useState } = window.React;
    const [title, setTitle] = useState("");
    const [body, setBody] = useState("");
    const [tags, setTags] = useState([]);
    const [sending, setSending] = useState(false);
    const [error, setError] = useState(null);
    const postable = forum.tags.filter((t) => t.postable);
    const missingTag = forum.require_tag && tags.length === 0;
    const ready = title.trim() && !missingTag && !sending;

    const submit = async (e) => {
        e.preventDefault();
        if (!ready) return;
        setSending(true);
        setError(null);
        try {
            const { room } = await call("/new", { forum: forum.id, title, body, tags });
            await waitForJoin(room);
            onDone(room);
        } catch (err) {
            setError(err.message);
            setSending(false);
        }
    };
    const toggle = (id) => setTags((t) => (t.includes(id) ? t.filter((x) => x !== id) : t.length < 5 ? [...t, id] : t));

    return h("form", { className: "cf_new", onSubmit: submit },
        h("input", {
            className: "cf_input", value: title, maxLength: 100, placeholder: "Title", "aria-label": "Title",
            autoFocus: true, onChange: (e) => setTitle(e.target.value),
        }),
        h("textarea", {
            className: "cf_input cf_textarea", value: body, maxLength: 2000, rows: 5,
            placeholder: "What is your post about? You can add screenshots in its room once it is created.",
            "aria-label": "Description", onChange: (e) => setBody(e.target.value),
        }),
        postable.length > 0 && h("div", { className: "cf_tags", role: "group", "aria-label": "Tags" },
            postable.map((t) => h("button", {
                key: t.id, type: "button", className: "cf_tag" + (tags.includes(t.id) ? " cf_tag_on" : ""),
                "aria-pressed": tags.includes(t.id), onClick: () => toggle(t.id),
            }, tagLabel(t)))),
        h("div", { className: "cf_new_footer" },
            h("span", { className: "cf_note" },
                missingTag ? "Pick at least one tag." : "Posted on Discord too, under your Matrix name."),
            h("div", { className: "cf_actions" },
                h("button", { type: "button", className: "cf_button", onClick: onCancel }, "Cancel"),
                h("button", { type: "submit", className: "cf_button cf_primary", disabled: !ready },
                    sending ? "Posting…" : "Post"))),
        error && h("div", { className: "cf_error" }, error));
}

function HeaderButton({ roomId }) {
    useStore();
    const link = store.rooms[roomId];
    const back = link && store.byId.get(link.forum);
    if (back) {
        return h("button", {
            className: "cf_header_button", title: "Back to the forum",
            onClick: (e) => {
                e.stopPropagation();
                showForum(back);
            },
        }, "← #" + back.name);
    }
    const forum = store.byIndex.get(roomId);
    if (!forum) return null;
    const chat = store.classic === forum.index;
    return h("button", {
        className: "cf_header_button",
        title: chat ? "Show the forum: posts, search and tags" : "Show this room's timeline of post cards",
        onClick: (e) => {
            e.stopPropagation();
            store.classic = chat ? null : forum.index;
            changed();
        },
    }, chat ? "Forum view" : "Chat view");
}

// --- styles --------------------------------------------------------------------

const CSS = `
.mx_RoomView_body:has(> .cf_mount) > :not(.cf_mount):not(.mx_RoomHeader) { display: none !important; }
.cf_mount { flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; }
.cf_page { flex: 1; min-height: 0; overflow-y: auto; background: var(--cpd-color-bg-canvas-default);
  color: var(--cpd-color-text-primary); font: var(--cpd-font-body-md-regular); }
.cf_inner { max-width: 960px; margin: 0 auto; padding: 16px 24px 48px; box-sizing: border-box; }
.cf_actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.cf_button { font: var(--cpd-font-body-md-semibold); border-radius: 99px; padding: 6px 16px; cursor: pointer;
  border: 1px solid var(--cpd-color-border-interactive-secondary); background: var(--cpd-color-bg-canvas-default);
  color: var(--cpd-color-text-primary); }
.cf_button:hover { background: var(--cpd-color-bg-subtle-secondary); }
.cf_button:disabled { opacity: 0.5; cursor: default; }
.cf_primary { background: var(--cpd-color-bg-action-primary-rest); border-color: transparent;
  color: var(--cpd-color-text-on-solid-primary); }
.cf_primary:hover { background: var(--cpd-color-bg-action-primary-hovered); }
.cf_toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.cf_search, .cf_sort, .cf_input { font: var(--cpd-font-body-md-regular); color: var(--cpd-color-text-primary);
  background: var(--cpd-color-bg-canvas-default); border: 1px solid var(--cpd-color-border-interactive-primary);
  border-radius: 8px; padding: 8px 12px; box-sizing: border-box; }
.cf_search { flex: 1 1 240px; min-width: 0; }
.cf_search:focus, .cf_sort:focus, .cf_input:focus { outline: 2px solid var(--cpd-color-border-focused); outline-offset: -1px; }
.cf_tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px; }
.cf_tag { font: var(--cpd-font-body-sm-semibold); border-radius: 99px; padding: 4px 12px; cursor: pointer;
  border: 1px solid var(--cpd-color-border-interactive-secondary); background: transparent;
  color: var(--cpd-color-text-secondary); }
.cf_tag:hover { color: var(--cpd-color-text-primary); }
.cf_tag_on { background: var(--cpd-color-bg-action-primary-rest); color: var(--cpd-color-text-on-solid-primary);
  border-color: transparent; }
.cf_tag_on:hover { color: var(--cpd-color-text-on-solid-primary); }
.cf_count { margin: 16px 0 8px; color: var(--cpd-color-text-secondary); font: var(--cpd-font-body-sm-regular); }
.cf_list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.cf_card { padding: 12px 16px; border-radius: 12px; cursor: pointer; background: var(--cpd-color-bg-subtle-secondary);
  border: 1px solid transparent; min-width: 0; }
.cf_card:hover, .cf_card:focus-visible { border-color: var(--cpd-color-border-interactive-secondary); outline: none; }
.cf_busy { cursor: progress; opacity: 0.8; }
.cf_disabled { cursor: default; opacity: 0.6; }
.cf_card_title { font: var(--cpd-font-body-lg-semibold); overflow-wrap: anywhere; }
.cf_card_tags { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px; }
.cf_chip { font: var(--cpd-font-body-sm-regular); padding: 1px 8px; border-radius: 99px;
  background: var(--cpd-color-bg-subtle-primary); color: var(--cpd-color-text-secondary); }
.cf_card_excerpt { margin-top: 6px; color: var(--cpd-color-text-secondary); overflow-wrap: anywhere;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cf_card_excerpt strong { color: var(--cpd-color-text-primary); font-weight: 600; }
.cf_card_meta { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px; color: var(--cpd-color-text-secondary);
  font: var(--cpd-font-body-sm-regular); }
.cf_note { color: var(--cpd-color-text-secondary); font: var(--cpd-font-body-sm-regular); }
.cf_error { margin-top: 8px; color: var(--cpd-color-text-critical-primary); font: var(--cpd-font-body-sm-regular); }
.cf_empty { padding: 32px 0; text-align: center; color: var(--cpd-color-text-secondary); }
.cf_more { display: block; margin: 16px auto 0; }
.cf_new { margin-top: 16px; padding: 16px; border-radius: 12px; background: var(--cpd-color-bg-subtle-secondary);
  display: flex; flex-direction: column; gap: 10px; }
.cf_textarea { resize: vertical; min-height: 96px; }
.cf_new .cf_tags { margin-top: 0; }
.cf_new_footer { display: flex; gap: 8px; justify-content: space-between; align-items: center; flex-wrap: wrap; }
.cf_header_button { font: var(--cpd-font-body-sm-semibold); border-radius: 99px; padding: 4px 12px; cursor: pointer;
  border: 1px solid var(--cpd-color-border-interactive-secondary); background: transparent;
  color: var(--cpd-color-text-primary); white-space: nowrap; max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
.cf_header_button:hover { background: var(--cpd-color-bg-subtle-secondary); }
@media (max-width: 600px) { .cf_inner { padding: 16px 16px 32px; } .cf_header_button { max-width: 110px; } }
`;

function addStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
}

// --- module ---------------------------------------------------------------------

function attempt(label, fn) {
    try {
        fn();
    } catch (e) {
        console.error("cosmos-forum: " + label + " disabled", e);
    }
}

function start() {
    // the forum list needs a signed-in client: retry until there is one
    if (!client()) {
        setTimeout(start, 3000);
        return;
    }
    loadForums(true);
    setInterval(() => loadForums(true), FORUMS_TTL);
}

export default class CosmosForumModule {
    // A range, not a caret: an Element upgrade bumping the module API's major
    // version must not stop the app from starting; hooks that changed simply
    // fail on their own (see attempt())
    static moduleApiVersion = ">=1.15.0";

    constructor(api) {
        this.api = api;
    }

    async load() {
        attempt("setup", () => {
            store.api = this.api;
            attempt("styles", addStyle);
            attempt("header buttons", () => this.api.extras.addRoomHeaderButtonCallback((roomId) => {
                if (!store.rooms[roomId] && !store.byIndex.has(roomId)) return undefined;
                return h(HeaderButton, { key: "cosmos-forum", roomId });
            }));
            attempt("forum view", () => {
                store.listeners.add(queueSync);
                window.addEventListener("hashchange", queueSync);
                window.addEventListener("hashchange", () => attempt("leave read posts", onRoomChange));
                new MutationObserver(queueSync).observe(this.api.rootNode || document.body,
                    { childList: true, subtree: true });
            });
            attempt("forum list", start);
        });
    }
}
