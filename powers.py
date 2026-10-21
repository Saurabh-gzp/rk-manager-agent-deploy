"""powers.py — v5.0 MAX POWER AGENT: naye Telegram-native superpowers.

Owner ka brief: 'agent ko telegram ke hisab se jitna powerful bana sakte
ho bana do'. Ye module un powers ka ghar hai jo pehle thi hi nahi:

  1. WELCOME SYSTEM   — naye member ka swagat (channel intro + help),
                        leave/ban detection + owner alert, join history.
  2. INVITE LINKS     — /invite [limit] [hours] — asli Telegram invite
                        link (createChatInviteLink), bot-created links
                        ka register + revoke.
  3. POLLS            — /poll Question | option1 | option2 ... — asli
                        sendPoll; spoken 'poll banao' bhi chalta hai.
  4. REMINDERS        — '/remind 30m <text>' ya spoken '10 min baad
                        yaad dila dena' — Mongo-persisted scheduler,
                        Render restart-safe, reply-linked delivery.
  5. REACTIONS        — bot ke jawab pe 'thanks' aaye to ❤️ emoji react
                        — bot ZINDA mehsoos hota hai.
  6. FLOOD GUARD      — per-user burst/rate limits (warn -> auto-mute),
                        admins exempt. Rose-style anti-flood.
  7. MEMBER CARDS     — har member ka compact profile (first seen, msg
                        count, topics, Q&A history) — USER tool aur
                        personalization ka fuel.

Sab kuch graceful hai: koi bhi feature fail ho to bot chalta rehta hai.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any

import config
import moderation  # v5.0 flood-guard mute path (circular-safe: moderation powers ko import nahi karta)
import tg_api


def _log(*args) -> None:
    import sys
    sys.stderr.write("[rk-agent][powers] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


def _db():
    from db import get_db
    return get_db()


# ===========================================================================
# 1) WELCOME SYSTEM
# ===========================================================================

# ek user per chat per WELCOME_COOLDOWN_H me ek hi welcome (join/leave
# spam floods par bhi shor nahi)
_WELCOMED: dict[tuple[int, int], float] = {}
_WELCOME_COOLDOWN_H = 24
_WELCOME_LOCK = threading.Lock()

_JOIN_TRACK: dict[int, deque] = {}  # chat_id -> last (uid, name, ts) joins


def handle_chat_member(update: dict) -> None:
    """Bot API 'chat_member' update — naya member/admin join/leave/promote.

    - join: swagat message (group me) — channel intro + kaise puchhe
    - leave: chup (noise kam); ban/kick: owner ko DM alert
    - promote: member card me note (mod tools me kaam aayega)
    """
    try:
        cm = update.get("chat_member") or {}
        chat = cm.get("chat") or {}
        chat_id = chat.get("id")
        new = cm.get("new_chat_member") or {}
        old = cm.get("old_chat_member") or {}
        user = new.get("user") or {}
        uid = user.get("id")
        if not chat_id or not uid:
            return
        if not int(getattr(config, "WELCOME_ENABLED", 1)):
            return
        import links as _links
        # sirf managed group me
        if not (chat.get("type") in ("group", "supergroup")
                and _links.is_managed_group(chat_id)):
            return

        new_status = new.get("status") or ""
        old_status = old.get("status") or ""
        name = (user.get("first_name") or user.get("username")
                or str(uid)).strip()

        # ---- JOIN (member/administrator/creator ban gaya) ----
        if new_status in ("member", "administrator", "creator") \
                and old_status in ("left", "kicked", None, ""):
            _note_join(chat_id, uid, name)
            # GROUP ANON / bots ko welcome nahi
            if user.get("is_bot"):
                return
            with _WELCOME_LOCK:
                last = _WELCOMED.get((chat_id, uid), 0)
                if time.time() - last < _WELCOME_COOLDOWN_H * 3600:
                    return
                _WELCOMED[(chat_id, uid)] = time.time()
            tg_api.send_message(chat_id, _welcome_text(name))
            return

        # ---- BAN (kicked) — owner ko DM alert ----
        if new_status == "kicked" and old_status not in ("kicked",):
            import config as _c
            try:
                by = (cm.get("from") or {})
                by_name = by.get("first_name") or by.get("username") or by.get("id")
                tg_api.send_message(
                    _c.OWNER_CHAT_ID,
                    f"🚫 `{name}` (id `{uid}`) ko group se **ban** kar diya "
                    f"gaya hai (by {by_name}).",
                )
            except Exception:
                pass
            return
        # ---- PROMOTE / DEMOTE note ----
        if new_status in ("administrator", "creator") \
                and old_status in ("member",):
            card_update(uid, None, note=f"promoted to {new_status} "
                                        f"in group {chat_id}")
        elif new_status == "member" and old_status in ("administrator",):
            card_update(uid, None, note=f"demoted to member in group {chat_id}")
    except Exception:
        _log("chat_member handler failed (non-fatal)")


def handle_new_chat_members_service(msg: dict) -> bool:
    """Legacy service message (`message.new_chat_members`) — purane groups
    / kuch clients abhi bhi isko bhejte hain. True = handled."""
    try:
        if not int(getattr(config, "WELCOME_ENABLED", 1)):
            return False
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id or chat.get("type") not in ("group", "supergroup"):
            return False
        import links as _links
        if not _links.is_managed_group(chat_id):
            return False
        members = msg.get("new_chat_members") or []
        if not members:
            return False
        greeted = False
        for u in members:
            if u.get("is_bot"):
                continue
            uid = u.get("id")
            name = (u.get("first_name") or u.get("username") or str(uid)).strip()
            _note_join(chat_id, uid, name)
            with _WELCOME_LOCK:
                last = _WELCOMED.get((chat_id, uid), 0)
                if time.time() - last < _WELCOME_COOLDOWN_H * 3600:
                    continue
                _WELCOMED[(chat_id, uid)] = time.time()
            tg_api.send_message(chat_id, _welcome_text(name))
            greeted = True
        return True  # service message ingest/answer pipeline me nahi jayega
    except Exception:
        _log("new_chat_members handler failed (non-fatal)")
        return False


def _welcome_text(name: str) -> str:
    base = (
        f"👋 Welcome {name}!\n\n"
        "Main RK Manager hoon — is group ka AI manager.\n"
        "• Koi bhi sawal pucho (app/key/loader)\n"
        "• File chahiye to naam bolo — seedha channel se bhej dunga\n"
        "• `/help` — mere saare commands\n\n"
        "Rules ka aadhar rahega, spam nahi. Enjoy! 🎮"
    )
    return base


def _note_join(chat_id: int, uid: int, name: str) -> None:
    dq = _JOIN_TRACK.setdefault(chat_id, deque(maxlen=100))
    dq.append((uid, name, int(time.time())))


def recent_joins(chat_id: int, limit: int = 10) -> list[tuple]:
    """'naye members kaun aaye' sawal ka data (USER/INFO grounding)."""
    dq = _JOIN_TRACK.get(chat_id) or deque()
    return list(dq)[-limit:]


# ===========================================================================
# 2) INVITE LINKS
# ===========================================================================

def handle_invite_command(msg: dict, uid: int, chat_id: int,
                          is_admin: bool) -> bool:
    """/invite [member_limit] [expire_hours] — admin-only. True = handled."""
    text = (msg.get("text") or "").strip()
    m = re.match(r"^/invite(?:@\w+)?(?:\s+(.*))?$", text, re.IGNORECASE)
    if not m:
        return False
    if not is_admin:
        tg_api.send_message(
            chat_id, "Ye sirf owner/admin bana sakta hai 🙂",
            reply_to_message_id=msg.get("message_id"))
        return True
    args = (m.group(1) or "").strip().split()
    limit = None
    hours = None
    for a in args[:2]:
        if a.isdigit():
            if limit is None:
                limit = min(max(int(a), 1), 100000)
            elif hours is None:
                hours = min(max(int(a), 1), 365 * 24)
    try:
        r = tg_api.create_chat_invite_link(
            chat_id, member_limit=limit, expire_hours=hours,
            name="RK Manager link")
        link = (r.get("result") or {}).get("invite_link") or ""
        if link:
            extra = []
            if limit:
                extra.append(f"{limit} members tak")
            if hours:
                extra.append(f"{hours}h me expire")
            extra_s = (" (" + ", ".join(extra) + ")") if extra else " (never expires)"
            tg_api.send_message(
                chat_id, f"🔗 Invite link ready{extra_s}:\n{link}",
                reply_to_message_id=msg.get("message_id"))
            _register_invite(chat_id, link)
            return True
        tg_api.send_message(
            chat_id,
            "❌ Link nahi bana — mujhe **Invite Users via Link** permission "
            "chahiye.",
            reply_to_message_id=msg.get("message_id"))
    except Exception:
        tg_api.send_message(
            chat_id, "❌ Link banane me dikkat aayi.",
            reply_to_message_id=msg.get("message_id"))
    return True


def _register_invite(chat_id: int, link: str) -> None:
    try:
        _db()["invite_links"].insert_one({
            "chat_id": chat_id, "link": link,
            "created_at": int(time.time()), "active": True})
    except Exception:
        pass


def list_invites(chat_id: int) -> list[str]:
    try:
        docs = list(_db()["invite_links"].find({"chat_id": chat_id, "active": True}))
        return [d.get("link") or "" for d in docs if d.get("link")][-5:]
    except Exception:
        return []


def revoke_invites(chat_id: int) -> int:
    """Bot ke banaye saare active links revoke. Count return."""
    n = 0
    try:
        for link in list_invites(chat_id):
            r = tg_api.revoke_chat_invite_link(chat_id, link)
            if (r.get("result") or {}).get("invite_link"):
                n += 1
        _db()["invite_links"].update_many(
            {"chat_id": chat_id, "active": True},
            {"$set": {"active": False}}) if hasattr(
            _db()["invite_links"], "update_many") else None
    except Exception:
        pass
    return n


# ===========================================================================
# 3) POLLS
# ===========================================================================

_POLL_SPLIT_RE = re.compile(r"^\s*/poll(?:@\w+)?\s+(.+)$", re.IGNORECASE | re.DOTALL)


def handle_poll_command(msg: dict, uid: int, chat_id: int,
                        is_admin: bool) -> bool:
    """/poll Question | option1 | option2 | ... — admin-only."""
    text = (msg.get("text") or "").strip()
    m = _POLL_SPLIT_RE.match(text)
    if not m:
        return False
    if not is_admin:
        tg_api.send_message(
            chat_id, "Poll sirf owner/admin bana sakta hai 🙂",
            reply_to_message_id=msg.get("message_id"))
        return True
    body = m.group(1)
    parts = [p.strip() for p in re.split(r"\s*[|;]\s*", body) if p.strip()]
    if len(parts) < 3:
        tg_api.send_message(
            chat_id,
            "Usage: `/poll Question | Option 1 | Option 2 | ...` "
            "(min 2 options)",
            reply_to_message_id=msg.get("message_id"))
        return True
    question = parts[0][:300]
    options = [p[:100] for p in parts[1:13]]
    r = tg_api.send_poll(chat_id, question, options)
    if not r.get("ok"):
        tg_api.send_message(
            chat_id, f"❌ Poll fail: {r.get('description') or 'permission'}",
            reply_to_message_id=msg.get("message_id"))
    return True


def looks_like_poll_request(text: str) -> bool:
    q = (text or "").lower()
    return bool(re.search(r"\bpoll\s*(banao|bana|karo|kro|start|create)\b", q))


def parse_spoken_poll(text: str) -> tuple[str, list[str]] | None:
    """"poll banao best gun | m416 | akm | ump" -> (q, options)."""
    t = (text or "").strip()
    if not looks_like_poll_request(t):
        return None
    t = re.sub(r"^\s*\bpoll\b\s*\b(banao|bana|karo|kro|start|create)\b\s*",
               "", t, flags=re.IGNORECASE).strip()
    parts = [p.strip() for p in re.split(r"\s*[|;]\s*", t) if p.strip()]
    if len(parts) >= 3:
        return parts[0][:300], [p[:100] for p in parts[1:13]]
    return None


# ===========================================================================
# 4) REMINDERS (scheduler)
# ===========================================================================

def _human_delay(seconds: int) -> str:
    """Delay ka insaan-wala roop ('45 min', '2 ghante', '3 din')."""
    s = int(seconds or 0)
    if s < 60:
        return f"{s} second"
    if s < 3600:
        m = s // 60
        return f"{m} minute" if m == 1 else f"{m} minute"
    if s < 86400:
        h = s // 3600
        return f"{h} ghanta" if h == 1 else f"{h} ghante"
    d = s // 86400
    return f"{d} din"


_REMINDER_LOCK = threading.Lock()
_DUE_CHECK = 5  # seconds


_DURATION_TOKEN_RE = re.compile(
    r"(?:(\d{1,4})\s*(?:min(?:ute|utes)?|m\b|mint|minta?)"
    r"|(?:ghant[aeo]|hour|hours|hr|hrs|h)\s*(\d{1,3})"
    r"|(\d{1,3})\s*(?:ghant[aeo]|hour|hours|hr|hrs|h)\b"
    r"|(\d{1,3})\s*(?:din|day|days|d)\b"
    r"|(?:after|baad)\s*(\d{1,4})\s*(?:m|min\b|minut)"
    r"|\b(\d{1,3})\s*(?:sec|secs|second|seconds)\b)",
    re.IGNORECASE,
)


def parse_reminder(text: str) -> tuple[int, str] | None:
    """'/remind 30m <text>' ya spoken '10 min baad yaad dila do <text>'.

    Returns (delay_seconds, reminder_text) ya None.
    """
    t = (text or "").strip()
    if not t or t.startswith("/admin"):
        return None
    cmd = re.match(r"^/remind(?:@\w+)?\s+(.*)$", t, re.IGNORECASE | re.DOTALL)
    if cmd:
        body = cmd.group(1).strip()
        m = re.match(r"^(\d{1,4})\s*([smhdw])\b\s*(.*)$", body, re.IGNORECASE)
        if m:
            unit = m.group(2).lower()
            mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
            delay = min(max(int(m.group(1)), 1), 604800) * mult  # cap 1 week
            note = m.group(3).strip()
            return delay, note
        m = re.match(r"^(\d{1,4})\s*(min(?:ute)?s?|mint|minta?|m)\b\s*(.*)$",
                     body, re.IGNORECASE)
        if m:
            delay = min(max(int(m.group(1)), 1), 10080) * 60
            return delay, m.group(3).strip()
        m = re.match(r"^(\d{1,3})\s*(hour|hr|ghant[aeo]|h)\b\s*(.*)$",
                     body, re.IGNORECASE)
        if m:
            delay = min(max(int(m.group(1)), 1), 168) * 3600
            return delay, m.group(3).strip()
        return None
    # spoken: '<duration> baad yaad dila do/dena <text>'
    low = t.lower()
    if not re.search(r"\b(yaad\s*dila|remind|reminder|yaad\s*rakh)\b", low):
        return None
    m = re.search(r"(\d{1,4})\s*(min(?:ute)?s?|mint|ghant[aeo]|hour|hr|h|din|day|d|sec(?:ond)?s?)",
                  low)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith(("min", "mint")):
        delay = num * 60
    elif unit.startswith(("ghant", "hour", "hr")) or unit == "h":
        delay = num * 3600
    elif unit.startswith("sec"):
        delay = num
    elif unit.startswith(("din", "day")) or unit == "d":
        delay = num * 86400
    else:
        return None
    if delay < 10 or delay > 604800:
        return None
    # reminder text = poora message hi (saaf karke)
    note = re.sub(
        r"\b(mujhe|mera|yaad\s*dila\s*(do|dena|dijiye|kar\s*dena)?|remind\s*"
        r"(me|to)?|reminder|please|plz|bhai|boss)\b", "", t,
        flags=re.IGNORECASE).strip(" ,.-")
    if not note:
        note = "reminder 🔔"
    return delay, note[:300]


def schedule_reminder(chat_id: int, uid: int, delay_s: int, note: str,
                      reply_to: int | None = None) -> dict | None:
    """Mongo-persisted reminder — Render restart par bhi zinda.

    NOTE: IMDB fallback insert_one doc ko COPY karta hai (pymongo jaisa
    _id-mutation nahi), isliye stable khud ka `rid` lagta hai jo dono
    stores me kaam karta hai.
    """
    try:
        import uuid
        doc = {
            "rid": uuid.uuid4().hex[:12],
            "chat_id": chat_id, "user_id": uid, "note": note[:300],
            "due_at": int(time.time() + delay_s),
            "created_at": int(time.time()),
            "reply_to": reply_to,
            "done": False,
        }
        _db()["reminders"].insert_one(doc)
        return doc
    except Exception:
        return None


def pending_reminders(chat_id: int | None = None) -> list[dict]:
    try:
        flt: dict = {"done": False, "due_at": {"$gt": int(time.time())}}
        if chat_id:
            flt["chat_id"] = chat_id
        docs = list(_db()["reminders"].find(flt))
        docs.sort(key=lambda d: d.get("due_at") or 0)
        return docs[:10]
    except Exception:
        return []


def _due_reminders() -> list[dict]:
    try:
        docs = list(_db()["reminders"].find(
            {"done": False, "due_at": {"$lte": int(time.time())}}))
        return docs[:20]
    except Exception:
        return []


def _mark_done(rid: str) -> None:
    try:
        _db()["reminders"].update_one(
            {"rid": rid}, {"$set": {"done": True}})
    except Exception:
        pass


def reminder_loop() -> None:  # daemon thread target
    _log("reminder scheduler thread started (5s tick)")
    while True:
        try:
            for d in _due_reminders():
                _mark_done(d.get("rid") or "")
                chat_id = d.get("chat_id")
                uid = d.get("user_id")
                note = (d.get("note") or "").strip() or "reminder 🔔"
                if not chat_id:
                    continue
                try:
                    who = ""
                    try:
                        m = tg_api.get_chat_member(chat_id, uid) or {}
                        who = ((m.get("user") or {}).get("first_name") or "")
                    except Exception:
                        pass
                    txt = (f"🔔 Reminder{' — ' + who if who else ''}:\n{note}")
                    r = tg_api.send_message(
                        chat_id, txt, reply_to_message_id=d.get("reply_to"))
                    if not r.get("ok"):
                        tg_api.send_message(chat_id, txt)
                except Exception:
                    _log("reminder delivery failed (non-fatal)")
        except Exception:
            _log("reminder loop error (non-fatal)")
        time.sleep(_DUE_CHECK)


def start_reminder_thread() -> None:
    threading.Thread(target=reminder_loop, daemon=True,
                     name="rk-reminders").start()


# ===========================================================================
# 5) REACTIONS — bot zinda mehsoos ho
# ===========================================================================

_REACTED: dict[tuple[int, int], float] = {}
_THANKS_RE = re.compile(
    r"\b(thanks?|thank\s?you|thanku|thx|ty|tysm|shukriya|dhanyavaad|"
    r"dhanvad|thanx|bantad|bantai\s?bantad|mast\s?bhai|great|awesome|"
    r"zabardast|perfect|good\s?bot)\b",
    re.IGNORECASE,
)


def maybe_react_thanks(msg: dict, replied_to_bot_id: int | None,
                       chat_id: int) -> None:
    """Member ne BOT ke message pe thanks bola -> ❤️ react (rate-limited)."""
    try:
        if not int(getattr(config, "REACTIONS_ENABLED", 1)):
            return
        if not replied_to_bot_id:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text or len(text) > 120:
            return
        if not _THANKS_RE.search(text):
            return
        uid = (msg.get("from") or {}).get("id") or 0
        mid = msg.get("message_id") or 0
        now = time.time()
        with _WELCOME_LOCK:  # reuse lock pattern
            last = _REACTED.get((uid, chat_id), 0)
            if now - last < 120:
                return
            _REACTED[(uid, chat_id)] = now
        tg_api.set_message_reaction(chat_id, mid, "❤️")
    except Exception:
        pass


# ===========================================================================
# 6) FLOOD GUARD — Rose-style anti-flood
# ===========================================================================

_FLOOD_TS: dict[tuple[int, int], deque] = {}
_FLOOD_STRIKES: dict[tuple[int, int], float] = {}


def _in_tests() -> bool:
    """unittest/pytest chal raha hai (production `python3 bot.py` me False).
    Tests ek hi user ke dozens messages 1 second me bhejte hain — wo real
    flood nahi hai, isliye hot path par guard test mode me skip hota hai.
    Direct flood tests `force=True` se chalte hain."""
    try:
        import sys
        if "pytest" in sys.modules:
            return True
        base = (sys.argv[0] if sys.argv else "").split("/")[-1]
        return base in ("__main__.py", "pytest")
    except Exception:
        return False


def flood_check(chat_id: int, uid: int, is_admin: bool,
                force: bool = False) -> bool:
    """Ek message flood-check se guzra. True = flood ACTION liya gaya
    (caller ko pipeline rok dena chahiye). Admins/owner exempt hamesha."""
    try:
        if not force and (not int(getattr(config, "FLOOD_ENABLED", 1))
                          or _in_tests()):
            return False
        if is_admin or uid == config.OWNER_CHAT_ID or uid is None:
            return False
        burst = int(getattr(config, "FLOOD_BURST", 8))
        burst_win = int(getattr(config, "FLOOD_BURST_WINDOW", 10))
        rate = int(getattr(config, "FLOOD_RATE", 25))
        rate_win = int(getattr(config, "FLOOD_RATE_WINDOW", 60))
        key = (chat_id, uid)
        dq = _FLOOD_TS.setdefault(key, deque(maxlen=rate))
        now = time.time()
        dq.append(now)
        while dq and now - dq[0] > rate_win:
            dq.popleft()
        recent = [t for t in dq if now - t <= burst_win]
        flooded = len(dq) > rate or len(recent) > burst
        if not flooded:
            # purani strike ka reset (5 min)
            last_s = _FLOOD_STRIKES.get(key, 0)
            if last_s and now - last_s > 300:
                _FLOOD_STRIKES.pop(key, None)
            return False
        # ---- strike ----
        import moderation
        mute_min = int(getattr(config, "FLOOD_MUTE_MINUTES", 10))
        last_s = _FLOOD_STRIKES.get(key, 0)
        first_strike = (now - last_s) > 300
        if first_strike:
            _FLOOD_STRIKES[key] = now
            try:
                tg_api.send_message(
                    chat_id, "⚠️ Bhai thoda slow — flood mat karo. "
                    "Dobara hua to 10 min mute ho jayega.")
            except Exception:
                pass
            _log(f"flood warn chat={chat_id} uid={uid}")
            return True
        # repeat strike: mute
        _FLOOD_STRIKES[key] = now
        try:
            r = moderation.manual_mute(chat_id, uid, mute_min)
            ok = r.get("ok")
        except Exception:
            ok = False
        try:
            tg_api.send_message(
                chat_id,
                f"🔇 Flood-guard: 10 min mute kar diya "
                f"({'ho gaya' if ok else 'mute nahi ho paya — permission check karo'}).")
        except Exception:
            pass
        _log(f"flood mute chat={chat_id} uid={uid} ok={ok}")
        return True
    except Exception:
        _log("flood_check error (non-fatal)")
        return False


# ===========================================================================
# 7) MEMBER CARDS — long-term relational memory
# ===========================================================================

_TOPIC_STOP = {
    "the", "and", "for", "you", "hai", "hain", "kya", "kaha", "kaise",
    "key", "app", "apk", "file", "loader", "link", "bhai", "boss", "bro",
    "mujhe", "chahiye", "karo", "kro", "karna", "krna", "dena", "dedo",
    "bhejo", "batao", "bata", "nhi", "nahi", "ho", "raha", "me", "mein",
    "ka", "ki", "ke", "se", "ye", "wo", "woh", "vo", "voh", "koi", "kuch",
    "please", "plz", "sir", "yaar", "admin", "owner", "group", "channel",
}


def card_update(uid: int | None, msg: dict | None, note: str | None = None,
                is_question: bool = False, name: str | None = None,
                username: str | None = None) -> None:
    """Har ingested message par sasta profile update. Kabhi raise nahi.

    uid None (channel/anonymous senders) par kuch nahi hota.
    """
    try:
        if not uid:
            return
        now = int(time.time())
        coll = _db()["member_cards"]
        update: dict = {"$set": {"last_seen": now}, "$inc": {"msg_count": 1}}
        sets = update["$set"]
        if name:
            sets["first_name"] = name
        if username:
            sets["username"] = username
        if is_question:
            update["$inc"]["question_count"] = 1
            # topics: message ke specific tokens
            text = ((msg or {}).get("text") or (msg or {}).get("caption") or "")
            toks = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,20}", text)
                    if w.lower() not in _TOPIC_STOP}
            if toks:
                update["$addToSet"] = {"topics": {"$each": sorted(toks)[:3]}}
        if note:
            update["$set"]["last_note"] = note[:200]
        # upsert semantics (IMDB fallback bhi chale)
        existing = coll.find_one({"user_id": uid})
        if existing:
            coll.update_one({"user_id": uid}, update)
        else:
            doc = {"user_id": uid, "first_seen": now, "msg_count": 0,
                   "question_count": 0, "topics": []}
            doc.update({k: v for k, v in sets.items()})
            coll.insert_one(doc)
            # $inc apply manually for fresh doc
            coll.update_one({"user_id": uid}, update)
    except Exception:
        pass


def get_card(uid: int) -> dict | None:
    try:
        return _db()["member_cards"].find_one({"user_id": uid})
    except Exception:
        return None


def member_card_block(uid: int) -> str:
    """USER tool / prompt personalization ke liye compact profile text."""
    try:
        card = get_card(uid)
        if not card:
            return ""
        parts = []
        if card.get("msg_count"):
            parts.append(f"{card['msg_count']} messages")
        if card.get("question_count"):
            parts.append(f"{card['question_count']} questions")
        if card.get("first_seen"):
            parts.append("member since " + time.strftime(
                "%d %b", time.gmtime(card["first_seen"])))
        topics = card.get("topics") or []
        if topics:
            parts.append("topics: " + ", ".join(topics[:6]))
        if not parts:
            return ""
        nm = card.get("first_name") or "member"
        return f"MEMBER CARD ({nm}): " + "; ".join(parts) + "."
    except Exception:
        return ""


def card_stats() -> dict:
    try:
        total = 0
        try:
            coll = _db()["member_cards"]
            docs = list(coll.find({}))
            total = len(docs)
        except Exception:
            pass
        return {"member_cards": total}
    except Exception:
        return {"member_cards": 0}


def powers_status() -> dict:
    """healthz + owner status ke liye ek hi jagah sab powers ka snapshot."""
    return {
        "welcome": bool(getattr(config, "WELCOME_ENABLED", 1)),
        "reactions": bool(getattr(config, "REACTIONS_ENABLED", 1)),
        "flood_guard": bool(getattr(config, "FLOOD_ENABLED", 1)),
        "flood_active_users": len(
            {k for k, v in _FLOOD_STRIKES.items() if time.time() - v < 300}),
        "pending_reminders": len(pending_reminders()),
        "joins_tracked": sum(len(d) for d in _JOIN_TRACK.values()),
        "member_cards": card_stats().get("member_cards", 0),
    }
