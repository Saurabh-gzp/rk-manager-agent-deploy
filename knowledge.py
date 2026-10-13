"""Knowledge store: ingestion, search, importance scoring, self-cleanup."""
from __future__ import annotations
import re
import time
from typing import Any

import config
import tg_api
from db import get_db, get_state, save_state


# --- importance (lightweight heuristic) ---

def _importance(text: str, file_id: str | None, forwarded: bool, is_question: bool) -> float:
    score = 0.0
    t = text or ""
    if len(t) > 200:
        score += 0.25
    if file_id:
        score += 0.4
    if is_question:
        score += 0.2
    if forwarded:
        score += 0.15
    if 30 <= len(t) <= 300:
        score += 0.1
    return min(1.0, score)


# --- file detection ---

_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)


def _extract_file(post: dict) -> tuple[str | None, str | None]:
    for ft in ("document", "video", "audio", "voice", "video_note",
               "animation", "sticker"):
        m = post.get(ft)
        if isinstance(m, dict) and m.get("file_id"):
            return m["file_id"], ft
    photos = post.get("photo")
    if isinstance(photos, list) and photos:
        biggest = max(photos, key=lambda p: (
            p.get("file_size") or 0, p.get("width") or 0, p.get("height") or 0
        ))
        if biggest.get("file_id"):
            return biggest["file_id"], "photo"
    return None, None


def _extract_contact(post: dict) -> dict | None:
    """Channel contacts (vCards) — no file_id, delivered by forwarding."""
    c = post.get("contact")
    if isinstance(c, dict) and (c.get("phone_number") or c.get("vcard")):
        return {
            "phone_number": c.get("phone_number") or "",
            "first_name": c.get("first_name") or "Contact",
            "last_name": c.get("last_name") or "",
            "vcard": c.get("vcard") or "",
        }
    return None


# --- ingest ---

def ingest_message(msg: dict) -> None:
    """Store one group/supergroup message. Never raises."""
    try:
        frm = msg.get("from") or {}
        if frm.get("is_bot"):
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text.startswith("/"):
            return
        doc = {
            "chat_id": (msg.get("chat") or {}).get("id"),
            "chat_title": (msg.get("chat") or {}).get("title"),
            "user_id": frm.get("id"),
            "username": frm.get("username"),
            "first_name": frm.get("first_name"),
            "text": text,
            "date": msg.get("date"),
            "importance": _importance(
                text, None,
                bool(msg.get("forward_from_chat") or msg.get("forward_origin")),
                "?" in text,
            ),
            "ingested_at": time.time(),
        }
        if doc["chat_id"] is None or not text:
            return
        get_db()["messages"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] ingest_message failed: {exc!r}", file=__import__("sys").stderr)


def _compact_entities(post: dict) -> list[dict]:
    """Entities of a post (caption_entities for media, entities for text)
    compacted to {type, offset, length, url?}. The text_link URLs are the
    REAL targets behind labels like 'KEY: Click me' — without storing them
    the key is unfixable dead text on any re-delivery. UTF-16 offsets kept
    as-is (Telegram semantics)."""
    ents = post.get("caption_entities") or post.get("entities") or []
    out: list[dict] = []
    for e in ents:
        if not isinstance(e, dict) or not e.get("type"):
            continue
        item = {"type": e["type"], "offset": e.get("offset") or 0,
                "length": e.get("length") or 0}
        if e.get("url"):
            item["url"] = e["url"]
        if e.get("user"):
            item["user"] = {"id": (e["user"] or {}).get("id"),
                            "username": (e["user"] or {}).get("username")}
        out.append(item)
    return out


def _entity_link_urls(ents: list[dict]) -> list[str]:
    """URLs hidden behind text_link labels (e.g. 'Click me' -> key page)."""
    urls = []
    for e in ents or []:
        if isinstance(e, dict) and e.get("type") == "text_link" and e.get("url"):
            urls.append(str(e["url"]))
    return urls


def ingest_channel_post(post: dict) -> None:
    """Store one channel post. Never raises."""
    try:
        text = post.get("text") or post.get("caption") or ""
        file_id, file_type = _extract_file(post)
        contact = _extract_contact(post)
        if contact:
            # Contact posts: label from the contact's name so search works.
            cname = " ".join(x for x in (contact.get("first_name"),
                                         contact.get("last_name")) if x)
            if cname and not text:
                text = f"[contact] {cname} {contact.get('phone_number', '')}".strip()
            elif cname:
                text = f"{text} [contact: {cname}]".strip()
            file_type = "contact"
        # file_name (e.g. BEASTCROWN~JAVA.apk) + links for name-search
        file_name = None
        for ft in ("document", "video", "audio", "animation"):
            m = post.get(ft)
            if isinstance(m, dict) and m.get("file_name"):
                file_name = m["file_name"]
                break
        ents = _compact_entities(post)
        links = _URL_RE.findall(text or "")
        # v3.3: text_link URLs (KEY: Click me -> real key page) join links[]
        # so search + LLM context can show the REAL clickable target.
        for u in _entity_link_urls(ents):
            if u not in links:
                links.append(u)
        doc = {
            "channel_id": (post.get("chat") or {}).get("id"),
            "message_id": post.get("message_id"),
            "text": text,
            "caption": post.get("caption"),
            "file_id": file_id,
            "file_type": file_type,
            "file_name": file_name,
            "links": links,
            "entities": ents,
            "media_group_id": post.get("media_group_id"),
            "contact": contact,
            "date": post.get("date"),
            "forwarded": bool(post.get("forward_from_chat") or post.get("forward_origin")),
            "importance": _importance(
                text, file_id or ("yes" if contact else None),
                bool(post.get("forward_from_chat") or post.get("forward_origin")),
                "?" in text,
            ),
            "ingested_at": time.time(),
        }
        if doc["channel_id"] is None or (not text and not file_id and not contact):
            return
        posts = get_db()["channel_posts"]
        existing = posts.find_one({
            "channel_id": doc["channel_id"],
            "message_id": doc["message_id"],
        })
        if existing:
            posts.update_one({"_id": existing["_id"]}, {"$set": doc})
        else:
            posts.insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] ingest_channel_post failed: {exc!r}", file=__import__("sys").stderr)


# --- search ---

def _tokenize(query: str) -> list[str]:
    return [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2][:6]


def search(query: str, limit: int = 10, user_id: int | None = None) -> list[dict]:
    """Search the asking user's own history plus shared channel posts.

    Group messages and Q&A stay scoped to the current Telegram user so one
    member's personal history is not surfaced in another member's answer.

    SPEED: ONE combined $or query per collection (3 total) instead of the
    old 5-queries-per-token loop (up to 30 Atlas roundtrips — measured
    ~4s per search on the M0 free tier; now ~0.4s). Multi-token relevance
    is scored locally after the fetch.
    """
    tokens = _tokenize(query)
    if not tokens:
        return []
    db = get_db()
    pattern = "|".join(re.escape(t) for t in tokens)
    rx = {"$regex": pattern, "$options": "i"}

    def _fetch(coll, flt, cap=80):
        try:
            cur = coll.find(flt)
            if hasattr(cur, "limit"):  # pymongo cursor
                return list(cur.limit(cap))
            return list(cur)[:cap]     # in-memory fallback
        except Exception:
            return []

    msg_flt: dict = {"text": rx}
    if user_id is not None:
        msg_flt["user_id"] = user_id
    qa_flt: dict = {"$or": [{"question": rx}, {"answer": rx}]}
    if user_id is not None:
        qa_flt["user_id"] = user_id

    msgs = _fetch(db["messages"], msg_flt)
    posts = _fetch(db["channel_posts"], {
        "$or": [{"text": rx}, {"caption": rx}, {"file_name": rx},
                {"links": rx}]})
    qas = _fetch(db["qa"], qa_flt)

    lows = [t.casefold() for t in tokens]

    def _score(text: str) -> int:
        tl = (text or "").casefold()
        return sum(1 for t in lows if t in tl)

    hits: list[dict] = []
    by_key: dict[tuple, dict] = {}

    def add(doc: dict, kind: str, text: str, extra: dict | None = None):
        s = _score(text)
        if s <= 0:
            return
        key = (kind, doc.get("_id"))
        if key in by_key:
            by_key[key]["match_score"] += s
            return
        row = {
            "kind": kind,
            "text": text,
            "date": doc.get("date") or 0,
            "match_score": s,
        }
        if extra:
            row.update(extra)
        by_key[key] = row
        hits.append(row)

    for d in msgs:
        add(d, "message", str(d.get("text", "")), {"chat_id": d.get("chat_id")})
    for d in posts:
        row_extra = {
            "file_id": d.get("file_id"), "file_type": d.get("file_type"),
            "caption": d.get("caption"), "channel_id": d.get("channel_id"),
            "message_id": d.get("message_id"), "contact": d.get("contact"),
            "file_name": d.get("file_name"),
            "links": d.get("links") or [],
            "entities": d.get("entities") or [],
        }
        add(d, "post",
            " ".join(filter(None, [
                str(d.get("text", "") or d.get("caption") or ""),
                str(d.get("file_name") or ""),
            ])), row_extra)
    for d in qas:
        add(d, "qa", f"Q: {d.get('question','')}\nA: {d.get('answer','')}", {"chat_id": d.get("chat_id")})

    hits.sort(
        key=lambda r: (r.get("match_score") or 0, r.get("date") or 0),
        reverse=True,
    )
    return hits[:limit]


def find_file_for(query: str) -> dict | None:
    """Find the most recent channel post that has a file and matches the query."""
    hits = search(query, limit=20)
    for h in hits:
        if h["kind"] == "post" and (h.get("file_id") or h.get("file_type") == "contact"):
            return h
    return None


def post_exists(channel_id: int, message_id: int) -> bool:
    """True when a channel post with this number is really in our records."""
    if not channel_id or not message_id:
        return False
    try:
        return get_db()["channel_posts"].find_one({
            "channel_id": channel_id, "message_id": int(message_id)}) is not None
    except Exception:
        return False


def get_post(channel_id: int, message_id: int) -> dict | None:
    """Full stored doc for a channel post (or None)."""
    if not channel_id or not message_id:
        return None
    try:
        return get_db()["channel_posts"].find_one({
            "channel_id": channel_id, "message_id": int(message_id)})
    except Exception:
        return None


def purge_post(channel_id: int, message_id: int) -> bool:
    """v3.4: post ko records se hata do — live channel me wo DELETE ho
    chuka hai (forward/copy 'not found' se confirm hua).

    Owner ka live issue: 'jo channel se delete ho chuka hai vo file bhi
    bhej deta hai'. Bot API deletion event nahi deta, isliye delivery
    ke waqt detection + removal hota hai. Purge ke baad wo post phir
    search/context/file-offer me kabhi nahi aayega. Returns True if removed."""
    if not channel_id or not message_id:
        return False
    try:
        r = get_db()["channel_posts"].delete_many({
            "channel_id": int(channel_id), "message_id": int(message_id)})
        return bool(getattr(r, "deleted_count", 0))
    except Exception:
        return False


# --- prune / cleanup ---

def _is_stale_message(d: dict, now: float) -> bool:
    date = d.get("date") or 0
    if now - date < config.MESSAGE_TTL_DAYS * 86400:
        return False
    imp = d.get("importance") or 0
    return imp < config.PRUNE_IMPORTANCE_FLOOR


def _is_stale_post(d: dict, now: float) -> bool:
    date = d.get("date") or 0
    has_file = bool(d.get("file_id"))
    if has_file:
        return now - date > config.FILE_POST_TTL_DAYS * 86400
    if now - date < config.POST_TTL_DAYS * 86400:
        return False
    imp = d.get("importance") or 0
    return imp < config.PRUNE_IMPORTANCE_FLOOR


def prune_once() -> dict:
    """Run one cleanup cycle. Returns counts dict."""
    db = get_db()
    now = time.time()
    counts = {
        "messages": 0,
        "posts": 0,
        "qa_old": 0,
        "escalations_resolved": 0,
        "inactive_users": 0,
    }

    # Keep each member's complete history while they remain active. Clear that
    # member's history only after a long period with no new messages.
    last_activity: dict[int, float] = {}
    for d in db["messages"].find():
        user_id = d.get("user_id")
        if user_id is None:
            if _is_stale_message(d, now):
                db["messages"].delete_many({"_id": d["_id"]})
                counts["messages"] += 1
            continue
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            continue
        last_activity[user_id] = max(
            last_activity.get(user_id, 0),
            float(d.get("date") or d.get("ingested_at") or 0),
        )
    idle_limit = config.USER_MEMORY_IDLE_DAYS * 86400
    for user_id, last_ts in last_activity.items():
        if now - last_ts <= idle_limit:
            continue
        # User ki identity (naam) delete hone se pehle nikaal lo — notice me
        # likhna hai.
        sample = db["messages"].find_one({"user_id": user_id}) or {}
        u_name = (sample.get("first_name") or sample.get("username")
                  or f"user {user_id}")
        u_msgs = db["messages"].count_documents({"user_id": user_id})
        u_qa = db["qa"].count_documents({"user_id": user_id})
        db["messages"].delete_many({"user_id": user_id})
        db["qa"].delete_many({"user_id": user_id})
        counts["messages"] += u_msgs
        counts["qa_old"] += u_qa
        counts["inactive_users"] += 1
        # OWNER KO NOTICE (user ki marzi): memory delete hone se pehle ek
        # baar DM zaroor jata hai — silent deletion kabhi nahi.
        try:
            idle_days = int((now - last_ts) / 86400)
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                "🧹 Memory cleanup\n\n"
                f"{u_name} (user_id: {user_id}) se {idle_days} din se koi "
                f"baat nahi hui, isliye uski purani yaadein delete kar raha "
                f"hu — {u_msgs} messages, {u_qa} Q&A.\n\n"
                "Wo wapas aaya to main uski nayi baatein phir se seekh "
                "lunga. Chahte ho to /users se dekh sakte ho kaun active "
                "hai.",
            )
        except Exception:
            pass
    # posts
    for d in db["channel_posts"].find():
        if _is_stale_post(d, now):
            db["channel_posts"].delete_many({"_id": d["_id"]})
            counts["posts"] += 1
    # very old resolved escalations
    for d in db["escalations"].find():
        if d.get("status") == "resolved" and now - (d.get("created_ts") or 0) > 30 * 86400:
            db["escalations"].delete_many({"_id": d["_id"]})
            counts["escalations_resolved"] += 1
    return counts


def maybe_prune() -> str:
    try:
        now = time.time()
        last = get_state("last_prune_ts", 0) or 0
        if now - last < config.PRUNE_EVERY_HOURS * 3600:
            return "skipped"
        counts = prune_once()
        save_state("last_prune_ts", now)
        return f"pruned {counts}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc!r}"


# --- channel reconciliation: detect deleted posts ---

def reconcile_channel(channel_id: int) -> dict:
    """Compare DB with what we last saw in the channel; mark/remove orphans.

    Strategy: store a `seen_message_ids` checkpoint on the link record.
    On each reconcile, fetch recent posts via getChat (only new ones via
    updates) — but since bot API doesn't expose post listing, we use a
    soft approach: drop posts older than max-age that are below importance
    floor, AND drop posts with no inbound activity for > FILE_POST_TTL_DAYS.
    For a more aggressive mode, owner can trigger full channel resync.
    """
    db = get_db()
    removed = 0
    now = time.time()
    for d in db["channel_posts"].find({"channel_id": channel_id}):
        # file-posts over max age and low importance: remove
        if d.get("file_id") and now - (d.get("date") or 0) > config.FILE_POST_TTL_DAYS * 86400:
            if (d.get("importance") or 0) < 0.5:
                db["channel_posts"].delete_many({"_id": d["_id"]})
                removed += 1
    return {"removed": removed}


def maybe_reconcile() -> str:
    try:
        now = time.time()
        last = get_state("last_reconcile_ts", 0) or 0
        if now - last < config.CHANNEL_RECONCILE_HOURS * 3600:
            return "skipped"
        db = get_db()
        total = 0
        # find distinct channel ids from links + posts
        chans: set[int] = set()
        for lnk in db["links"].find({"channel_id": {"$exists": True, "$ne": None}}):
            cid = lnk.get("channel_id")
            if isinstance(cid, int):
                chans.add(cid)
        chan_id = get_state("channel_id")
        if isinstance(chan_id, int):
            chans.add(chan_id)
        for cid in chans:
            res = reconcile_channel(cid)
            total += res["removed"]
        save_state("last_reconcile_ts", now)
        return f"reconciled channels={len(chans)} removed={total}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc!r}"


def forget_query(query: str) -> int:
    try:
        db = get_db()
        rgx = {"$regex": query, "$options": "i"}
        total = 0
        total += db["messages"].delete_many({"text": rgx}).deleted_count
        total += db["channel_posts"].delete_many({"text": rgx}).deleted_count
        total += db["channel_posts"].delete_many({"caption": rgx}).deleted_count
        total += db["qa"].delete_many({"question": rgx}).deleted_count
        total += db["qa"].delete_many({"answer": rgx}).deleted_count
        return total
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] forget_query failed: {exc!r}", file=__import__("sys").stderr)
        return 0


# --- stats ---

def stats() -> dict:
    db = get_db()
    return {
        "messages": db["messages"].count_documents({}),
        "posts": db["channel_posts"].count_documents({}),
        "qa": db["qa"].count_documents({}),
        "escalations_open": db["escalations"].count_documents({"status": {"$ne": "resolved"}}),
        "links": db["links"].count_documents({}),
    }
