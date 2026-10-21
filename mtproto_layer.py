"""mtproto_layer.py — v4.2 REALTIME POWER LAYER (MTProto via Telethon).

Owner ka brief: 'API ID/Hash use karo taki agent ki power badh jaye —
sabhi channel/group ke messages khud realtime me dekh paaye, old + new
sb kuch, aur ek real agent ki tarah respond kare.'

Telegram Bot API 3 cheezein NAHI deta (isliye stale-data leak, dumb
group answers aur history-gaps the):
  1. deleted-post events      -> purane/delete hue posts users tak jate the
  2. channel history access   -> bot add hone se pehle ke posts kabhi nahi
  3. message fetch by ID     -> group answers bina real context ke

MTProto — SAME bot token, owner ke API ID/Hash ke saath (BotFather ka
user-account nahi, bot hi hai) — ye SAB deta hai. LIVE-VERIFIED
is service par (probe ke saath, @rkgamersbgmi channel + RK GAMERS TEAM
group dono par):
  - channels.getMessages          -> channel AND supergroup messages
                                      ID se, batches of 100, full text
  - UpdateDeleteChannelMessages   -> delete events REALTIME (channel +
                                      supergroup) — Bot API me exist hi
                                      nahi karta
  - UpdateEditChannelMessage      -> edit events REALTIME
  - UpdateNewChannelMessage       -> naye posts (Bot API backup stream)

Bots ke liye BLOCKED (BotMethodInvalidError, live-tested): GetHistory,
GetDialogs, Search. Isliye history-backfill = ID-RANGE SCAN: stored max
post-id ke baad ke IDs [max+1 .. max+SCAN_WINDOW] batch-fetch karke jo
mile ingest. Bot offline tha ya add hone se pehle ke posts — sab aa
jate hain, deleted IDs waise bhi khud-verify ho jaate hain.

Threading model (stdlib-only existing bot ko zero-impact rakhta hai):
  - Telethon asyncio loop apne daemon thread me chalta hai.
  - Sync Bot API code (polling/webhook handlers) thread-safe request
    API se call karta hai (asyncio.run_coroutine_threadsafe).
  - Updates ek thread-safe deque me push hote hain; bot.py ka sync-thread
    unhe drain karke apply karta hai (purge/ingest).
  - Agar telethon install nahi hai / API ID missing / Telegram MTProto
    unreachable — layer gracefully DISABLED rehti hai; bot v4.1 jaisa
    100% kaam karta rehta hai (sirf realtime powers nahi).
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

import config

# telethon optional hai — bot bina iske bhi full working (graceful).
try:  # pragma: no cover - environment-dependent
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import GetMessagesRequest
    _TELETHON_OK = True
except Exception:  # noqa: BLE001
    _TELETHON_OK = False

# ---------------------------------------------------------------------------
# state (layer-internal; zero shared mutable state with the rest of the bot)
# ---------------------------------------------------------------------------

_LOOP: asyncio.AbstractEventLoop | None = None     # telethon thread ka loop
_CLIENT: Any = None                                # TelegramClient
_THREAD: threading.Thread | None = None            # telethon runner thread
_SYNC_THREAD: threading.Thread | None = None       # event-apply thread (bot.py)
_START_LOCK = threading.Lock()
_STARTED = False

_CONNECTED = False
_DISABLED_REASON = ""
_LAST_ERROR = ""
_STARTED_AT = 0.0
_LAST_EVENT_TS = 0.0
_EVENTS_SEEN = 0
_STATS_LOCK = threading.Lock()

_FETCH_CHUNK = 100          # channels.getMessages per-request ID limit
_REQUEST_TIMEOUT = 25.0     # sync caller side timeout (seconds)

# ("delete", chat_id, [ids]) | ("edit", chat_id, mid) | ("new", chat_id, parsed)
_EVENTS: deque = deque(maxlen=2000)
_EVENTS_LOCK = threading.Lock()

# chat_id -> resolved entity ( Telethon entity resolution cache —
# har fetch par network roundtrip save hoti hai)
_ENTITY_CACHE: dict[int, Any] = {}
_ENTITY_LOCK = threading.Lock()


def _log(*args) -> None:
    import sys
    sys.stderr.write("[rk-agent][mtproto] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


def _set_state(connected: bool | None = None, error: str | None = None,
               event: bool = False) -> None:
    global _CONNECTED, _LAST_ERROR, _LAST_EVENT_TS, _EVENTS_SEEN
    with _STATS_LOCK:
        if connected is not None:
            _CONNECTED = connected
        if error is not None:
            _LAST_ERROR = error[:300]
        if event:
            _LAST_EVENT_TS = time.time()
            _EVENTS_SEEN += 1


# ---------------------------------------------------------------------------
# status (healthz / owner panel / status command)
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Config side: API ID+HASH set hain aur telethon import hua."""
    return bool(getattr(config, "MTPROTO_ENABLED", 1)
                and getattr(config, "MTPROTO_API_ID", 0)
                and getattr(config, "MTPROTO_API_HASH", "")
                and _TELETHON_OK)


def is_connected() -> bool:
    return bool(_CONNECTED and _LOOP is not None and _CLIENT is not None)


def status() -> dict:
    """healthz?debug=1 + /status dono ise dikhate hain."""
    with _STATS_LOCK:
        return {
            "enabled": enabled(),
            "connected": is_connected(),
            "disabled_reason": _DISABLED_REASON,
            "last_error": _LAST_ERROR,
            "events_seen": _EVENTS_SEEN,
            "last_event_ts": int(_LAST_EVENT_TS),
            "uptime_s": int(time.time() - _STARTED_AT) if _STARTED_AT else 0,
        }


# ---------------------------------------------------------------------------
# session persistence — Render ka disk ephemeral hai, isliye session string
# Mongo `state` me save hoti hai (restart par re-auth nahi lagta).
# ---------------------------------------------------------------------------

def _load_session_string() -> str:
    """Env override > Mongo state > fresh (login ke baad save)."""
    env_ss = (getattr(config, "MTPROTO_SESSION", "") or "").strip()
    if env_ss:
        return env_ss
    try:
        from db import get_state
        ss = str(get_state("mt_session_string") or "").strip()
        if ss:
            return ss
    except Exception:
        pass
    return ""


def _save_session_string(ss: str) -> None:
    try:
        from db import save_state
        save_state("mt_session_string", ss)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# message parsing: Telethon message -> simple dict (sync world ka format)
# ---------------------------------------------------------------------------

def _parse_message(m: Any) -> dict | None:
    """Telethon Message -> plain dict. Deleted/empty posts alive=False.

    Return shape:
      {"id", "alive", "date" (unix), "text", "file_name", "file_type",
       "grouped_id", "author"}
    alive=False = ye ID Telegram par delete/empty hai (MessageEmpty —
    date None hota hai, live-verified)."""
    try:
        if m is None or not hasattr(m, "id"):
            return None
        if getattr(m, "date", None) is None:
            return {"id": int(m.id), "alive": False, "date": 0,
                    "text": "", "file_name": None, "file_type": None,
                    "grouped_id": None, "author": ""}
        text = (m.message or "").strip()
        file_name = None
        file_type = None
        media = getattr(m, "media", None)
        if media is not None:
            # DUCK-TYPING (Telethon classes bhi, tests ke fakes bhi):
            #   MessageMediaDocument -> .document
            #   MessageMediaPhoto    -> .photo
            #   MessageMediaContact  -> .first_name
            #   MessageMediaGeo      -> .geo
            doc = getattr(media, "document", None)
            if doc is not None:
                file_type = "document"
                for attr in getattr(doc, "attributes", None) or []:
                    a_name = type(attr).__name__
                    if a_name == "DocumentAttributeFilename" \
                            or hasattr(attr, "file_name"):
                        fn = getattr(attr, "file_name", None)
                        if fn:
                            file_name = str(fn)
                            low = file_name.lower()
                            if low.endswith((".mp4", ".mkv", ".avi", ".mov")):
                                file_type = "video"
                            elif low.endswith((".mp3", ".m4a", ".flac",
                                               ".wav", ".ogg")):
                                file_type = "audio"
                            elif low.endswith(".gif"):
                                file_type = "animation"
                    if a_name == "DocumentAttributeAudio" \
                            or hasattr(attr, "voice"):
                        if getattr(attr, "voice", False):
                            file_type = "voice"
                        elif file_type == "document":
                            file_type = "audio"
                    if a_name == "DocumentAttributeVideo" \
                            or (hasattr(attr, "round_message")
                                and getattr(attr, "round_message", None)
                                is not None):
                        if getattr(attr, "round_message", False):
                            file_type = "video_note"
                        elif file_type == "document":
                            file_type = "video"
                if file_type == "document":
                    mt = str(getattr(doc, "mime_type", "") or "")
                    if mt.startswith("video/"):
                        file_type = "video"
                    elif mt.startswith("audio/"):
                        file_type = "audio"
            elif getattr(media, "photo", None) is not None:
                file_type = "photo"
            elif getattr(media, "first_name", None) is not None:
                file_type = "contact"
            elif getattr(media, "geo", None) is not None:
                file_type = "geo"
        try:
            date_unix = int(m.date.timestamp())
        except Exception:
            date_unix = 0
        return {
            "id": int(m.id),
            "alive": True,
            "date": date_unix,
            "text": text,
            "file_name": file_name,
            "file_type": file_type,
            "grouped_id": (str(m.grouped_id) if getattr(m, "grouped_id", None) else None),
            "author": (m.post_author or "") if getattr(m, "post_author", None) else "",
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# internal async helpers (telethon loop ke andar chalte hain)
# ---------------------------------------------------------------------------

async def _resolve_entity(chat_id: int) -> Any:
    """chat_id -> Telethon entity, in-process cache ke saath."""
    with _ENTITY_LOCK:
        if chat_id in _ENTITY_CACHE:
            return _ENTITY_CACHE[chat_id]
    entity = await _CLIENT.get_entity(int(chat_id))
    with _ENTITY_LOCK:
        _ENTITY_CACHE[chat_id] = entity
    return entity


async def _fetch_ids(chat_id: int, ids: list[int]) -> dict[int, dict | None]:
    """channels.getMessages in <=100-ID chunks. {mid: parsed|None}."""
    out: dict[int, dict | None] = {}
    entity = await _resolve_entity(chat_id)
    for i in range(0, len(ids), _FETCH_CHUNK):
        chunk = [int(x) for x in ids[i:i + _FETCH_CHUNK]]
        if not chunk:
            continue
        r = await _CLIENT(GetMessagesRequest(channel=entity, id=chunk))
        for m in (getattr(r, "messages", None) or []):
            parsed = _parse_message(m)
            if parsed is None:
                continue
            out[parsed["id"]] = parsed
        # jo IDs response me nahi aaye wo bhi unknown mark karo
        for x in chunk:
            out.setdefault(x, None)
    return out


async def _apply_session(client: Any, fresh_needed: bool) -> None:
    """Login (bot token) + session save. fresh_needed=True par pehli baar."""
    await client.sign_in(bot_token=config.TELEGRAM_BOT_TOKEN)
    ss = client.session.save()
    if ss:
        _save_session_string(ss)


# ---------------------------------------------------------------------------
# sync-world request API (polling thread / handlers in se call karte hain)
# ---------------------------------------------------------------------------

def fetch_messages(chat_id: int, ids: list[int]) -> dict[int, dict | None]:
    """Messages live-fetch by ID (channel ya supergroup). Blocking, safe.

    Returns {message_id: parsed|None}; {} = layer down (caller falls back).
    Parsed dict: alive/date/text/file_name/file_type/grouped_id/author.
    Deleted posts alive=False ke saath milte hain (stale-data truth)."""
    if not is_connected() or not ids or not chat_id:
        return {}
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _fetch_ids(int(chat_id), [int(x) for x in ids][:400]), _LOOP)
        return fut.result(timeout=_REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        _set_state(error=f"fetch: {exc!r}")
        return {}


def verify_posts(channel_id: int, ids: list[int]) -> dict[int, bool | None]:
    """Post IDs ka LIVE verdict: True=zinda, False=deleted, None=unknown.

    Ye verify.py ka new AUTHORITATIVE layer hai — private channels bhi
    cover (HTTP t.me probe sirf public ke liye tha). Unknown = network/
    layer problem; caller par destructive action kabhi nahi hota unknown
    par. Layer down hone par {} (no verdicts at all)."""
    raw = fetch_messages(channel_id, ids)
    if not raw:
        return {}
    out: dict[int, bool | None] = {}
    for mid in ids:
        p = raw.get(int(mid))
        if p is None:
            out[int(mid)] = None
        else:
            out[int(mid)] = bool(p.get("alive"))
    return out


def scan_channel_window(channel_id: int, from_id: int, count: int) -> list[dict]:
    """[from_id .. from_id+count-1] scan -> sirf ALIVE parsed posts.

    History backfill ka engine: GetHistory bots ke liye blocked hai,
    par ID-range scan se bhi wahi result (channel post IDs ~contiguous
    hote hain; deleted IDs alive=False me filter ho jaate hain)."""
    count = max(1, min(int(count), int(getattr(config, "MTPROTO_SCAN_WINDOW", 200))))
    ids = list(range(int(from_id), int(from_id) + count))
    raw = fetch_messages(channel_id, ids)
    out = []
    for mid in ids:
        p = raw.get(mid)
        if p and p.get("alive"):
            p["chat_id"] = int(channel_id)
            out.append(p)
    return out


def fetch_group_context(chat_id: int, around_id: int, before: int = 12,
                        after: int = 0) -> list[dict]:
    """Group answer context: around_id se pehle ke `before` messages.

    Privacy mode ON ho tab bhi (updates filter ho sakte hain) ID-fetch
    direct hota hai — isliye group answers REAL context ke saath ban
    sakte hain ('dumb answers' ka fix). after>0 abhi support nahi —
    future-message fetch bina naye update ke miss ho sakta hai; safe
    rehta hai before-only."""
    if around_id <= 0:
        return []
    lo = max(1, int(around_id) - max(1, int(before)))
    hi = int(around_id) + max(0, int(after))
    ids = list(range(lo, hi + 1))
    raw = fetch_messages(chat_id, ids)
    out = []
    for mid in ids:
        p = raw.get(mid)
        if p and p.get("alive"):
            p["chat_id"] = int(chat_id)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# event queue — telethon thread push karta hai, bot.py drain karta hai
# ---------------------------------------------------------------------------

def _push_event(kind: str, chat_id, payload) -> None:
    with _EVENTS_LOCK:
        _EVENTS.append((kind, chat_id, payload))
    _set_state(event=True)


def drain_events(limit: int = 200) -> list[tuple]:
    """Bot-side sync thread inhe apply karta hai (bot.py)."""
    out: list[tuple] = []
    with _EVENTS_LOCK:
        while _EVENTS and len(out) < limit:
            out.append(_EVENTS.popleft())
    return out


def _install_handlers(client: Any) -> None:
    """Updates: deletes (channel+supergroup), edits, naye channel posts.

    NOTE — group 'new' messages yahan PUSH NAHI hote: moderation Bot API
    path par pehle chalta hai (abusive msg delete->phir memory me nahi
    jaana chahiye); MTProto stream se seedha ingest moderation bypass
    kar deta. Channels me moderation nahi hai (sab admin posts), wahan
    backup ingestion safe hai. Deletes/edits dono chats ke liye hamesha."""
    if not _TELETHON_OK:
        return

    async def _on_delete(event):
        try:
            ids = [int(x) for x in (event.deleted_ids or [])]
            cid = event.chat_id
            if ids and cid:
                _push_event("delete", int(cid), ids)
        except Exception:
            pass

    async def _on_edit(event):
        try:
            p = _parse_message(event.message)
            if p and event.chat_id:
                _push_event("edit", int(event.chat_id), p)
        except Exception:
            pass

    async def _on_new(event):
        try:
            chat = await event.get_chat()
            # sirf CHANNELS (supergroup nahi — wo Bot API+moderation ka ilaka hai)
            if chat is not None and getattr(chat, "broadcast", False):
                p = _parse_message(event.message)
                if p and p.get("alive"):
                    _push_event("new", int(event.chat_id), p)
        except Exception:
            pass

    client.add_event_handler(_on_delete, events.MessageDeleted())
    client.add_event_handler(_on_edit, events.MessageEdited())
    client.add_event_handler(_on_new, events.NewMessage())


# ---------------------------------------------------------------------------
# telethon runner thread
# ---------------------------------------------------------------------------

def _runner() -> None:  # pragma: no cover - network thread
    global _LOOP, _CLIENT, _DISABLED_REASON
    if not enabled():
        _DISABLED_REASON = ("API ID/HASH missing" if _TELETHON_OK
                            else "telethon not installed")
        _log("disabled:", _DISABLED_REASON)
        return
    api_id = int(config.MTPROTO_API_ID)
    api_hash = str(config.MTPROTO_API_HASH)

    while True:
        try:
            _LOOP = asyncio.new_event_loop()
            asyncio.set_event_loop(_LOOP)
            ss = _load_session_string()
            _CLIENT = TelegramClient(
                StringSession(ss) if ss else StringSession(),
                api_id, api_hash,
                connection_retries=5, retry_delay=3, timeout=25,
                auto_reconnect=True,
            )
            _LOOP.run_until_complete(_CLIENT.connect())
            if not _LOOP.run_until_complete(_CLIENT.is_user_authorized()):
                _LOOP.run_until_complete(_apply_session(_CLIENT, True))
                _log("bot signed in via MTProto (session saved to Mongo)")
            _install_handlers(_CLIENT)
            _set_state(connected=True, error="")
            _log("connected — realtime delete/edit/new streams ON")
            _LOOP.run_until_complete(_CLIENT.run_until_disconnected())
        except Exception as exc:  # noqa: BLE001
            _set_state(connected=False, error=f"runner: {exc!r}")
            _log("runner error:", repr(exc), "— retry in 20s")
            try:
                if _CLIENT is not None:
                    _LOOP.run_until_complete(_CLIENT.disconnect())
            except Exception:
                pass
        finally:
            _set_state(connected=False)
        time.sleep(20)  # reconnect backoff (auto-restart forever)


def start() -> bool:
    """Layer start (idempotent). False = disabled by config/env."""
    global _THREAD, _STARTED, _STARTED_AT
    with _START_LOCK:
        if _STARTED:
            return True
        _STARTED = True
        _STARTED_AT = time.time()
        if not enabled():
            global _DISABLED_REASON
            _DISABLED_REASON = ("API ID/HASH missing" if _TELETHON_OK
                                else "telethon not installed")
            _log("disabled:", _DISABLED_REASON)
            return False
        _THREAD = threading.Thread(target=_runner, daemon=True,
                                   name="rk-mtproto")
        _THREAD.start()
        _log("thread starting (api_id set, telethon present)")
        return True
