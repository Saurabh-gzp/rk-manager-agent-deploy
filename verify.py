"""v4.0 TRUTH LAYER — real-time channel verification (the 'old data' killer).

Owner ka live issue: 'jo channel se delete ho chuka hai vo file/link bhi
bhej deta hai ... latest message channel ke latest data ke hisab se
response nhi diya old data se' (ZYROX dead-links case).

Telegram Bot API deleted-channel-post events PUSH NAHI karta (open feature
request, Bot API issue #286). Isliye deletion ko 3 layers me pakadte hain:

  1. LIVE DELIVERY PROBE (already in channel_forward/agent since v3.4):
     forward/copy 'not found' aaye to post purge + honest jawab.
  2. THIS MODULE — public-channel HTTP probe: t.me/<username>/<N> par bina
     login ke padha ja sakta hai. Deleted post -> page par 'Post can't be
     shown' / 'message not found' / 404 aata hai; live post -> uska asli
     text preview. Har probe ka result 10 min cache hota hai, isliye
     answer-latency par asar sirf pehli baar hota hai (~0.3-0.6s per link).
  3. BACKGROUND SWEEP — daemon thread har VERIFY_SWEEP_MINUTES (30) me
     recently-active/ingested posts ka bounded batch (default 60) verify
     karta hai aur dead posts ko channel_posts se purge kar deta hai —
     isliye purane dead links LLM context me kabhi nahi aate.

Private channels (t.me/c/...) bina login nahi khulte — unke liye layer 1
(delivery probe) hi authoritative hai; sweep unhe skip karta hai.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request

import config

# channel_id+message_id -> (ts, alive)   alive: True/False/None(unknown)
_PROBE_CACHE: dict[tuple[int, int], tuple[float, bool | None]] = {}
_CACHE_TTL = 600.0  # 10 min
_PROBE_LOCK = threading.Lock()

# Dead-post markers Telegram's public preview page uses (legacy safety)
_DEAD_MARKERS = (
    "post can't be shown",
    "post cannot be shown",
    "message not found",
    "post not found",
    "message deleted",
    "no such message",
    "this message doesn't exist",
    "this message does not exist",
    "channel doesn't exist",
    "channel does not exist",
)
# v4.0 LIVE-VERIFIED page shapes (real channel @rkgamersbgmi par probe karke
# confirm kiye gaye):
#   LIVE post page  -> contains `data-telegram-post="<ch>/<id>"` (embed
#                      widget) aur "Context Embed / View In Channel" header;
#                      NO tgme_page_description.
#   DEAD post page  -> t.me channel LANDING page fallback: has
#                      tgme_page_description + tgme_page_action, NO embed.
_LIVE_MARKER = b"data-telegram-post"   # bytes: works on raw read
_LANDING_MARKER = b"tgme_page_description"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _enabled() -> bool:
    try:
        return bool(getattr(config, "VERIFY_ENABLED", 1))
    except Exception:
        return True


def _channel_username(channel_id: int | None) -> str:
    """Managed-channel @username via saved state first (no API call)."""
    if not channel_id:
        return ""
    try:
        from db import get_state
        u = str(get_state("managed_channel_username") or "").strip()
        if u:
            return u.lstrip("@")
    except Exception:
        pass
    try:
        import tg_api
        info = tg_api.get_chat(channel_id) or {}
        u = (info.get("username") or "").strip().lstrip("@")
        if u:
            from db import save_state
            save_state("managed_channel_username", u)
            return u
    except Exception:
        pass
    return ""


def _probe_http(username: str, message_id: int) -> bool | None:
    """GET https://t.me/<username>/<N> and classify the page shape.

    LIVE-VERIFIED logic (against the real managed channel):
      - live post  -> page embeds the post widget (`data-telegram-post`)
      - dead post  -> t.me falls back to the channel LANDING page
                      (`tgme_page_description` present, no embed)
      - 404/410    -> dead
    Returns True (alive), False (deleted) or None (inconclusive — network
    error, captcha/interstitial, anything not shaped like t.me — never
    treated as deleted: a flaky probe must never destroy good data).
    """
    url = f"https://t.me/{username}/{int(message_id)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read(65536)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False
        return None  # 403/5xx/anti-bot -> inconclusive
    except Exception:
        return None
    if _LIVE_MARKER in raw:
        return True
    # not the embed shape — is it at least a t.me channel landing page?
    if _LANDING_MARKER in raw:
        # landing page WITHOUT the embed = post not renderable = deleted
        return False
    # anything else (captcha, redirect page, empty) — inconclusive
    try:
        low = raw.decode("utf-8", "replace").lower()
    except Exception:
        return None
    for marker in _DEAD_MARKERS:
        if marker in low:
            return False
    return None


def verify_post(channel_id: int | None, message_id: int | None,
                force: bool = False) -> bool | None:
    """Is this channel post still alive? (True/False/None-unknown).

    v4.2 — do-layer verification, AUTHORITATIVE pehle:
      1. MTProto (channels.getMessages): direct Telegram se live
         verdict — private channels bhi (HTTP probe nahi kar sakta tha
         unhe), ~0.1-0.3s, delete = MessageEmpty (alive=False).
      2. Fallback — public channel ka t.me page probe (purana v4.0
         layer; MTProto down/unreachable hone par hi chalta hai).

    Cached for 10 minutes. Never *deletes* anything by itself — caller
    (purge_if_dead / sweep / answer-gate) decides.
    """
    if not channel_id or not message_id or not _enabled():
        return None
    key = (int(channel_id), int(message_id))
    now = time.time()
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit and not force and now - hit[0] < _CACHE_TTL:
            return hit[1]
    # ---- layer 1: MTProto live fetch (authoritative) ----
    try:
        import mtproto_layer
        if mtproto_layer.is_connected():
            verdicts = mtproto_layer.verify_posts(int(channel_id),
                                                  [int(message_id)])
            alive = verdicts.get(int(message_id))
            if alive is not None:
                with _PROBE_LOCK:
                    _PROBE_CACHE[key] = (time.time(), alive)
                return alive
    except Exception:
        pass
    # ---- layer 2: HTTP t.me probe (public channels only) ----
    uname = _channel_username(channel_id)
    if not uname:
        return None  # private channel — HTTP probe impossible
    alive = _probe_http(uname, int(message_id))
    if alive is not None:
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = (time.time(), alive)
    return alive


def purge_if_dead(channel_id: int | None, message_id: int | None) -> bool:
    """Verify; if CONFIRMED dead, purge from channel_posts. Returns purged."""
    if not channel_id or not message_id:
        return False
    alive = verify_post(channel_id, message_id)
    if alive is False:
        try:
            from knowledge import purge_post
            purge_post(int(channel_id), int(message_id))
            return True
        except Exception:
            return False
    return False


_MANAGED_LINK_RES: list[tuple[re.Pattern, int]] = []
_MANAGED_LINK_TS = 0.0


def _managed_link_regexes() -> list[tuple[re.Pattern, int]]:
    """(regex, channel_id) pairs for OUR channel's post links, cached."""
    global _MANAGED_LINK_RES, _MANAGED_LINK_TS
    now = time.time()
    if _MANAGED_LINK_RES and now - _MANAGED_LINK_TS < 600:
        return _MANAGED_LINK_RES
    try:
        import links
        cid = links.get_channel_id()
    except Exception:
        cid = None
    if not cid:
        return []
    out: list[tuple[re.Pattern, int]] = []
    uname = _channel_username(cid)
    if uname:
        out.append((re.compile(
            rf"(?:https?://)?t\.me/{re.escape(uname)}/(\d{{1,6}})",
            re.IGNORECASE), cid))
    internal = str(abs(cid))
    if internal.startswith("100"):
        internal = internal[3:]
    out.append((re.compile(
        rf"(?:https?://)?t\.me/c/{re.escape(internal)}/(\d{{1,6}})",
        re.IGNORECASE), cid))
    _MANAGED_LINK_RES = out
    _MANAGED_LINK_TS = now
    return out


def verify_answer_links(text: str | None, question: str = "") -> str | None:
    """Answer OUTBOUND gate: every managed-channel post link in a generated
    answer is verified against the LIVE channel before the user sees it.

    - Dead link -> purged from DB, link replaced by an honest note, and a
      suggestion to ask the owner to re-post.
    - Unknown/private or network hiccup -> link untouched (never destroy a
      possibly-good answer on a flaky probe).
    Returns cleaned text (or None if everything is stripped).
    """
    if not text or not _enabled():
        return text
    pats = _managed_link_regexes()
    if not pats:
        return text
    dead_note = (
        "(ye post channel se delete ho chuka hai — link hata diya)"
    )
    for rx, cid in pats:
        for m in list(rx.finditer(text)):
            n = int(m.group(1))
            link = m.group(0)
            alive = verify_post(cid, n)
            if alive is False:
                try:
                    from knowledge import purge_post
                    purge_post(int(cid), n)
                except Exception:
                    pass
                text = text.replace(link, dead_note)
    if dead_note in text:
        # consecutive dead-notes collapse into one
        text = re.sub(rf"(?:{re.escape(dead_note)}\s*){{2,}}",
                      dead_note + " ", text)
        # Only mention the honest caveat once per answer, at the end.
        if question and text.strip() == dead_note.strip():
            text = ("Wo post channel se delete ho chuka hai boss — ab mere "
                    "paas ye nahi hai. Naya post aane par turant bata dunga.")
    return re.sub(r"[ \t]{2,}", " ", text).strip() or None


def sweep_once(limit: int | None = None) -> dict:
    """Verify a bounded batch of recent channel posts; purge confirmed-dead.

    v4.2: MTProto connected hai to BATCH verify (100 IDs per request,
    private channels bhi, fast) — HTTP probing sirf fallback hai.
    Skips posts verified recently. Result db-state me save hota hai
    (`last_verify_sweep`) taaki /healthz se production behavior observe
    ho sake.
    """
    if not _enabled():
        return {"skipped": True}
    try:
        limit = int(limit) if limit is not None else int(
            getattr(config, "VERIFY_SWEEP_BATCH", 60))
    except Exception:
        limit = 60
    try:
        from db import get_db
        coll = get_db()["channel_posts"]
        rows = list(coll.find({}))
        rows.sort(key=lambda d: (d.get("date") or 0), reverse=True)
        docs = rows[: max(limit * 3, limit)]  # headroom for unknowns
    except Exception:
        _remember_sweep({"error": "db"})
        return {"error": "db"}

    # ---- v4.2 fast path: MTProto batch verify (one request per 100 ids) ----
    try:
        import mtproto_layer
        if mtproto_layer.is_connected():
            checked = purged = 0
            pending: list[tuple[int, int]] = []  # (cid, mid) candidates
            for d in docs:
                cid, mid = d.get("channel_id"), d.get("message_id")
                if not cid or not mid:
                    continue
                with _PROBE_LOCK:
                    hit = _PROBE_CACHE.get((int(cid), int(mid)))
                    if hit and hit[1] is False:
                        continue
                    if hit and time.time() - hit[0] < _CACHE_TTL:
                        continue
                pending.append((int(cid), int(mid)))
                if len(pending) >= limit:
                    break
            if not pending:
                res = {"checked": 0, "purged": 0, "skipped": 0,
                       "mode": "mtproto"}
                _remember_sweep(res)
                return res
            by_chan: dict[int, list[int]] = {}
            for cid, mid in pending:
                by_chan.setdefault(cid, []).append(mid)
            for cid, mids in by_chan.items():
                verdicts = mtproto_layer.verify_posts(cid, mids)
                for mid in mids:
                    alive = verdicts.get(mid)
                    if alive is None:
                        continue
                    checked += 1
                    with _PROBE_LOCK:
                        _PROBE_CACHE[(cid, mid)] = (time.time(), alive)
                    if alive is False:
                        try:
                            from knowledge import purge_post
                            purge_post(cid, mid)
                            purged += 1
                        except Exception:
                            pass
            res = {"checked": checked, "purged": purged,
                   "skipped": len(pending) - checked, "mode": "mtproto"}
            _remember_sweep(res)
            return res
    except Exception:
        pass

    # ---- legacy path: HTTP page probe (MTProto down) ----
    checked = purged = skipped = 0
    for d in docs:
        if checked >= limit:
            break
        cid, mid = d.get("channel_id"), d.get("message_id")
        if not cid or not mid:
            continue
        key = (int(cid), int(mid))
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(key)
            if hit and hit[1] is False:
                continue  # already purged / known dead
            if hit and time.time() - hit[0] < _CACHE_TTL:
                skipped += 1
                continue
        alive = verify_post(cid, mid)
        if alive is None:
            skipped += 1
            continue
        checked += 1
        if alive is False:
            try:
                from knowledge import purge_post
                purge_post(int(cid), int(mid))
                purged += 1
            except Exception:
                pass
    res = {"checked": checked, "purged": purged, "skipped": skipped,
           "mode": "http"}
    _remember_sweep(res)
    return res


def _remember_sweep(res: dict) -> None:
    """Persist the last sweep result for /healthz observability."""
    try:
        from db import save_state
        out = dict(res)
        out["ts"] = int(time.time())
        save_state("last_verify_sweep", out)
    except Exception:
        pass


def last_sweep() -> dict | None:
    try:
        from db import get_state
        return get_state("last_verify_sweep")
    except Exception:
        return None


def start_background_sweep() -> threading.Thread | None:
    """Daemon thread: bounded sweep every VERIFY_SWEEP_MINUTES (default 30).

    Webhook + polling dono mode me safe hai (daemon, exceptions swallowed,
    bina lock ke DB reads). Render free tier ke liye halka rakha gaya hai.
    """
    try:
        minutes = max(5, int(getattr(config, "VERIFY_SWEEP_MINUTES", 30)))
    except Exception:
        minutes = 30

    def _loop():
        # pehla sweep startup ke 3 min baad — cold start ke time Mongo/TG
        # connections settle ho jaate hain
        time.sleep(180)
        while True:
            try:
                res = sweep_once()
                if res.get("purged"):
                    import sys
                    sys.stderr.write(
                        f"[rk-agent][verify] sweep {res}\n")
                    sys.stderr.flush()
            except Exception:
                pass
            time.sleep(minutes * 60)

    t = threading.Thread(target=_loop, daemon=True, name="rk-verify-sweep")
    t.start()
    return t


def mark_deleted(channel_id: int | None, message_ids: list[int]) -> None:
    """v4.2: MTProto delete-event ne confirm kar diya — in IDs ko
    dead-mark karo (sweep/probe ab in par dobara network roundtrip
    nahi lagayegi). Fail-safe: sirf cache likhta hai, kuch delete nahi."""
    if not channel_id or not message_ids:
        return
    try:
        now = time.time()
        with _PROBE_LOCK:
            for mid in message_ids:
                _PROBE_CACHE[(int(channel_id), int(mid))] = (now, False)
    except Exception:
        pass


def stats() -> dict:
    with _PROBE_LOCK:
        return {"cached_probes": len(_PROBE_CACHE)}


def invalidate_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()
