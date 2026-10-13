"""Knowledge store: ingestion, search, importance scoring, self-cleanup.

v3.5 MEMORY MANAGEMENT (owner ka live concern: 'chats ka data store hoga
to ek din bahot bada data ho jayega aur agent ko response dene me bahot
time ya stuck ho sakta hai'):
  - HARD CAP per chat (MEMORY_MAX_MESSAGES_PER_CHAT) — raw messages
    kabhi cap se zyada store nahi honge.
  - Compaction purane messages ko ek chhote LLM DIGEST (short summary)
    me badal deta hai (industry 'conversation summary memory' pattern):
    community facts bache rehte hain, size bounded rehta hai.
  - Q&A per user ka bhi hard cap (MEMORY_MAX_QA_PER_USER).
  - Search HAMESHA bounded hai (fetch cap 80, return limit) — data jitna
    bada ho jaye, answer latency utni hi speed ki rahegi, kabhi stuck nahi.
"""
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

# v3.5: in-process per-chat message counters — ingest-time cap check ko
# sasta banata hai (har message par Mongo count_documents nahi).
_CHAT_MSG_COUNT: dict[int, int] = {}
_COUNT_CHECK_EVERY = 100  # real count har ~100 messages par


def ingest_message(msg: dict) -> None:
    """Store one group/supergroup message. Never raises.

    v3.5: insert ke baad ek sasta in-memory counter badhta hai; har
    ~100 messages par REAL count check hota hai aur chat cap se zyada
    hone par compact_chat() purane messages ko digest me badal deta hai
    (raw delete + chhota summary save). Isse data size bounded rehta hai
    aur search speed waisi hi — agent kabhi slow/stuck nahi hoga.
    """
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
        # v3.5 memory guard: sasta counter, periodic real check
        try:
            cid = int(doc["chat_id"])
            _CHAT_MSG_COUNT[cid] = _CHAT_MSG_COUNT.get(cid, 0) + 1
            if _CHAT_MSG_COUNT[cid] % _COUNT_CHECK_EVERY == 0:
                compact_chat(cid)
        except Exception:
            pass
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


def search(query: str, limit: int = 10, user_id: int | None = None,
           chat_id: int | None = None) -> list[dict]:
    """Search the user's/group's history plus shared channel posts.

    Scope (v3.5):
      - chat_id diya (group context) -> us GROUP ki saari messages search
        hoti hain. Telegram group me waise bhi har member sab padh sakta
        hai — isliye ye privacy boundary nahi todti, sirf grounding ko
        'channel ke hisab se' REAL banati hai ('key kha milega' ka jawab
        group/channel ke actual data se aayega).
      - warna user_id scope (DM / per-user history) — pehle jaisa.
    Q&A is always scoped to the asking user (uski personal track record).

    SPEED: ONE combined $or query per collection (3 total) instead of the
    old 5-queries-per-token loop (up to 30 Atlas roundtrips — measured
    ~4s per search on the M0 free tier; now ~0.4s). Multi-token relevance
    is scored locally after the fetch. v3.5: digests (compact summaries)
    bhi search me aate hain — purani community info raw data delete hone
    ke baad bhi grounding me milti hai.
    """
    tokens = _tokenize(query)
    if not tokens:
        return []
    db = get_db()
    pattern = "|".join(re.escape(t) for t in tokens)
    rx = {"$regex": pattern, "$options": "i"}

    def _fetch(coll, flt, cap=80, recency=False):
        try:
            cur = coll.find(flt)
            if hasattr(cur, "limit"):  # pymongo cursor
                if recency and hasattr(cur, "sort"):
                    # v4.0: newest-first fetch — 'latest channel data ke
                    # hisab se response' guarantee: purane dead/mismatched
                    # posts kabhi latest posts ko candidate set se nahi
                    # dhakelte (match_score tie par date sort already hai).
                    cur = cur.sort("date", -1)
                return list(cur.limit(cap))
            docs = list(cur)[:cap]     # in-memory fallback
            if recency:
                docs.sort(key=lambda d: (d.get("date") or 0), reverse=True)
            return docs
        except Exception:
            return []

    msg_flt: dict = {"text": rx}
    if chat_id is not None:
        msg_flt["chat_id"] = chat_id
    elif user_id is not None:
        msg_flt["user_id"] = user_id
    qa_flt: dict = {"$or": [{"question": rx}, {"answer": rx}]}
    if user_id is not None:
        qa_flt["user_id"] = user_id

    msgs = _fetch(db["messages"], msg_flt, recency=True)
    posts = _fetch(db["channel_posts"], {
        "$or": [{"text": rx}, {"caption": rx}, {"file_name": rx},
                {"links": rx}]}, recency=True)
    qas = _fetch(db["qa"], qa_flt)
    digests = _fetch(db["digests"], {"summary": rx})

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
    for d in digests:
        add(d, "digest",
            f"[community digest of {d.get('msg_count', '?')} older messages] "
            + str(d.get("summary", "")),
            {"chat_id": d.get("chat_id")})

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


# --- v3.5 memory compaction: hard caps + LLM digests (short summaries) ---

def summarize_batch(texts: list[str]) -> str | None:
    """Ek chhota community-digest banane ke liye LLM call (best-effort).

    Industry 'conversation summary memory' pattern ka core: purani
    conversations ko ek compact summary me compact karo, raw data drop
    karo. Summary me sirf COMMUNITY FACTS rehte hain (key ka process,
    popular apps/loaders, rules, events) — usernames/PII nahi. LLM fail
    ho jaye to None — caller raw delete SKIP kar deta hai (data kabhi
    silently loss nahi hota).
    """
    if not texts:
        return None
    if not getattr(config, "DIGEST_ENABLED", 1):
        return None
    try:
        import ai_engine  # local import — circular safety
        sample = "\n".join("- " + t[:200] for t in texts[:80])
        out = ai_engine.get_router().generate(
            "Neeche ek Telegram gaming group ke purane messages hain. "
            "Inka ek COMPACT digest banao (3-6 chhoti lines, Hinglish):\n"
            "- sirf COMMUNITY ke kaam ki facts: keys/apps/loaders/files ka "
            "process, rules, events, common problems + solutions\n"
            "- usernames / personal chatter / PII KABHI mat likho\n"
            "- har line ek alag fact ho\n\n"
            "Messages:\n" + sample,
            system="You compress group-chat history into short factual "
                   "community-memory digests. Output only the digest lines.",
            max_tokens=220,
        )
        out = (out or "").strip()
        return out or None
    except Exception:
        return None


def compact_chat(chat_id: int, cap: int | None = None) -> dict:
    """v3.5: ek chat ke raw messages ko MEMORY_MAX_MESSAGES_PER_CHAT par
    hard-cap karo, aur hataye gaye purane messages ka LLM DIGEST banao.

    Policy:
      - importance >= 0.5 wale messages (lambe factual posts, owner rules)
        KABHI raw delete nahi hote — wo hamesha searchable rehte hain.
      - bakki oldest messages cap se jyada hone par digest me compact hote
        hain; LLM digest ban jaye tabhi raw delete hota hai (fail = data
        rehne do, agli cycle me phir try).
      - digest 'digests' collection me chhota doc hai — search me
        kind='digest' milta hai, isliye purani community info grounding
        me bane rehti hai (bina size badaye).

    Atlas M0 (512MB) + bounded search dono safe rehte hain: data growth
    O(cap) hai, latency O(fetched docs) hai — agent slow/stuck kabhi
    nahi hoga (owner ka live concern)."""
    try:
        db = get_db()
        cap = cap if cap is not None else config.MEMORY_MAX_MESSAGES_PER_CHAT
        if cap <= 0:
            return {"total": 0, "removed": 0, "digest": False}
        docs = list(db["messages"].find({"chat_id": chat_id}))
        total = len(docs)
        if total <= cap:
            _CHAT_MSG_COUNT[chat_id] = total
            return {"total": total, "removed": 0, "digest": False}
        excess = total - cap
        # high-importance ko hamesha rakho; victims = oldest low-importance
        victims = [d for d in docs if (d.get("importance") or 0) < 0.5]
        survivors = total - len(victims)
        if survivors < cap:
            # victims me se utne hi hatao jitna cap allow kare
            need = excess
        else:
            # survivors already cap cover karte hain — jitna extra hai utna
            # hi hatana hai, but never more than len(victims)
            need = min(excess, len(victims))
        if need <= 0:
            _CHAT_MSG_COUNT[chat_id] = total
            return {"total": total, "removed": 0, "digest": False}
        victims.sort(key=lambda d: (d.get("date") or 0, str(d.get("_id"))))
        drop = victims[:need]
        digest = summarize_batch([str(d.get("text") or "") for d in drop])
        if digest:
            db["digests"].insert_one({
                "chat_id": chat_id,
                "summary": digest[:2000],
                "msg_count": len(drop),
                "ts_from": drop[0].get("date") or 0,
                "ts_to": drop[-1].get("date") or 0,
                "created_at": time.time(),
            })
        else:
            # LLM digest fail — raw delete SKIP (silent data loss nahi)
            _CHAT_MSG_COUNT[chat_id] = total
            return {"total": total, "removed": 0, "digest": False}
        for d in drop:
            db["messages"].delete_many({"_id": d["_id"]})
        _CHAT_MSG_COUNT[chat_id] = total - len(drop)
        return {"total": total, "removed": len(drop), "digest": True}
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] compact_chat failed: {exc!r}",
              file=__import__("sys").stderr)
        return {"total": 0, "removed": 0, "digest": False}


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
    """Run one cleanup cycle. Returns counts dict.

    v3.5: ab HAR cycle me chat-level hard caps + digests + Q&A caps bhi
    enforce hote hain — data growth hamesha bounded rehta hai (owner ka
    'ek din bahot bada data ho jayega' concern ka structural fix).
    """
    db = get_db()
    now = time.time()
    counts = {
        "messages": 0,
        "posts": 0,
        "qa_old": 0,
        "escalations_resolved": 0,
        "inactive_users": 0,
        "compacted": 0,
        "digests": 0,
        "qa_capped": 0,
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
    # v3.5 — CHAT HARD CAPS + DIGESTS: har chat ko cap par laao (purane
    # messages compact summaries ban jaate hain, community facts bache
    # rehte hain). Restart ke baad bhi guarantee — ye cycle 12h chalti hai.
    chat_ids: set[int] = set()
    for d in db["messages"].find():
        try:
            chat_ids.add(int(d.get("chat_id")))
        except (TypeError, ValueError):
            continue
    for cid in chat_ids:
        res = compact_chat(cid)
        counts["compacted"] += res.get("removed") or 0
        if res.get("digest"):
            counts["digests"] += 1
    # v3.5 — Q&A HARD CAP per user: bot ki di hui purani jawabon ki
    # track-record unbounded grow na kare.
    qa_by_user: dict[int, list[dict]] = {}
    for d in db["qa"].find():
        try:
            qa_by_user.setdefault(int(d.get("user_id")), []).append(d)
        except (TypeError, ValueError):
            continue
    qa_cap = config.MEMORY_MAX_QA_PER_USER
    if qa_cap > 0:
        for u, docs in qa_by_user.items():
            if len(docs) <= qa_cap:
                continue
            docs.sort(key=lambda d: (d.get("date") or 0, str(d.get("_id"))))
            for d in docs[: len(docs) - qa_cap]:
                db["qa"].delete_many({"_id": d["_id"]})
                counts["qa_capped"] += 1
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
        "digests": db["digests"].count_documents({}),
        "escalations_open": db["escalations"].count_documents({"status": {"$ne": "resolved"}}),
        "links": db["links"].count_documents({}),
    }
