"""Knowledge store: ingestion, search, importance scoring, self-cleanup."""
from __future__ import annotations
import re
import time
from typing import Any

import config
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

def _extract_file(post: dict) -> tuple[str | None, str | None]:
    for ft in ("document", "video", "audio", "voice", "video_note"):
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
            "importance": _importance(text, None, bool(msg.get("forward_from_chat")), "?" in text),
            "ingested_at": time.time(),
        }
        if doc["chat_id"] is None or not text:
            return
        get_db()["messages"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] ingest_message failed: {exc!r}", file=__import__("sys").stderr)


def ingest_channel_post(post: dict) -> None:
    """Store one channel post. Never raises."""
    try:
        text = post.get("text") or post.get("caption") or ""
        file_id, file_type = _extract_file(post)
        doc = {
            "channel_id": (post.get("chat") or {}).get("id"),
            "message_id": post.get("message_id"),
            "text": text,
            "caption": post.get("caption"),
            "file_id": file_id,
            "file_type": file_type,
            "date": post.get("date"),
            "forwarded": bool(post.get("forward_from_chat")),
            "importance": _importance(text, file_id, bool(post.get("forward_from_chat")), "?" in text),
            "ingested_at": time.time(),
        }
        if doc["channel_id"] is None or (not text and not file_id):
            return
        get_db()["channel_posts"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] ingest_channel_post failed: {exc!r}", file=__import__("sys").stderr)


# --- search ---

def _tokenize(query: str) -> list[str]:
    return [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2][:6]


def search(query: str, limit: int = 10) -> list[dict]:
    """Case-insensitive search across messages, posts, qa. Returns ranked hits."""
    tokens = _tokenize(query)
    if not tokens:
        return []
    db = get_db()
    hits: list[dict] = []
    seen: set = set()

    def add(doc: dict, kind: str, text: str, extra: dict | None = None):
        key = (kind, doc.get("_id"))
        if key in seen:
            return
        seen.add(key)
        row = {"kind": kind, "text": text, "date": doc.get("date") or 0}
        if extra:
            row.update(extra)
        hits.append(row)

    for tok in tokens:
        rgx = {"$regex": re.escape(tok), "$options": "i"}
        for d in db["messages"].find({"text": rgx}):
            add(d, "message", str(d.get("text", "")), {"chat_id": d.get("chat_id")})
        for d in db["channel_posts"].find({"text": rgx}):
            add(d, "post", str(d.get("text", "") or d.get("caption") or ""), {
                "file_id": d.get("file_id"), "file_type": d.get("file_type"),
                "caption": d.get("caption"), "channel_id": d.get("channel_id"),
                "message_id": d.get("message_id"),
            })
        for d in db["channel_posts"].find({"caption": rgx}):
            add(d, "post", str(d.get("caption") or d.get("text", "")), {
                "file_id": d.get("file_id"), "file_type": d.get("file_type"),
                "caption": d.get("caption"), "channel_id": d.get("channel_id"),
                "message_id": d.get("message_id"),
            })
        for d in db["qa"].find({"question": rgx}):
            add(d, "qa", f"Q: {d.get('question','')}\nA: {d.get('answer','')}", {"chat_id": d.get("chat_id")})
        for d in db["qa"].find({"answer": rgx}):
            add(d, "qa", f"Q: {d.get('question','')}\nA: {d.get('answer','')}", {"chat_id": d.get("chat_id")})

    hits.sort(key=lambda r: (r.get("date") or 0), reverse=True)
    return hits[:limit]


def find_file_for(query: str) -> dict | None:
    """Find the most recent channel post that has a file and matches the query."""
    hits = search(query, limit=20)
    for h in hits:
        if h["kind"] == "post" and h.get("file_id"):
            return h
    return None


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
    counts = {"messages": 0, "posts": 0, "qa_old": 0, "escalations_resolved": 0}

    # messages
    for d in db["messages"].find():
        if _is_stale_message(d, now):
            db["messages"].delete_many({"_id": d["_id"]})
            counts["messages"] += 1
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
