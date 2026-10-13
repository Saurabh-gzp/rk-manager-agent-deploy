"""Owner-controlled group moderation with Rose-style mod tools.

Auto-moderation (profanity blocklist) deletes/warns/mutes but intentionally
never bans. Bans happen only via an explicit owner /ban command.

Custom FILTERS are Rose-style content filters now (see filters.py): they
DELIVER saved content when the keyword is used — they never delete.
"""
from __future__ import annotations

import re
import time
import unicodedata

import config
from db import get_db, get_state, save_state
import tg_api
import filters
from filters import (  # re-exported: tests patch these on `moderation`
    get_filters,
    add_filter,
    add_text_filter,
    remove_filter,
    clear_filters,
    matches_filter,
    deliver_filter,
    remember_media,
)


DEFAULT_TERMS = [
    # -- Hindi abuses (with common spelling variants) --
    "madarchod", "madharchod", "maderchod", "madar chod", "madarchood",
    "behenchod", "behnchod", "bhenchod", "behen chod",
    "bhosda", "bhosdi", "bhosdike", "bhosdiwale", "bhosri", "bhonsdi",
    "bsdk", "bkl", "bc", "mc",
    "chutiya", "chutiye", "chut", "choot", "chootiya",
    "gandu", "gand", "gaand", "gandfat",
    "sala", "saala", "sale", "suvar", "suar",
    "kutta", "kutti", "kutte", "kutto", "kuttey",
    "harami", "haramkhor", "haramzada", "haramzyada",
    "kamina", "kamine", "kameena", "kamini",
    "bhadwa", "bhadwe", "bhadvi",
    "randi", "rundi", "randa", "chinal", "chhinal",
    "loda", "lodu", "lauda", "lavda", "lawda", "lund", "lulli",
    "tatti", "chodu", "chudwa", "chudai", "chud",
    "teri maa ka", "teri maa ki", "maa chuda", "maa ki chut",
    # -- English abuses --
    "fuck", "fucking", "fucker", "motherfucker", "mf", "shit", "bitch",
    "asshole", "bastard", "dick", "pussy", "cunt", "whore", "slut",
]
STRIKE_WINDOW_SECONDS = 24 * 60 * 60
MUTE_MINUTES = 10
MAX_MANUAL_MUTE_MINUTES = 24 * 60
_MUTE_PERMISSIONS = {
    "can_send_messages": False,
    "can_send_audios": False,
    "can_send_documents": False,
    "can_send_photos": False,
    "can_send_videos": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
}


_LEET_MAP = str.maketrans({
    "@": "a", "0": "o", "3": "e", "1": "i", "4": "a",
    "5": "s", "$": "s", "7": "t", "!": "i", "€": "e", "£": "l",
})


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _squeeze(value: str) -> str:
    """Collapse consecutive duplicate characters: 'madharchood' -> 'madharchod'."""
    out = []
    for ch in value:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def _fuzzy_form(value: str) -> str:
    """Aggressive matching form: leet-decode + squeeze + strip separators.

    Catches evasive spellings like 'madharchood', 'm@darch0d', 'b.h.o.s.d.a'.
    """
    step = _normalize(value).translate(_LEET_MAP)
    step = re.sub(r"[^\w]+", "", step, flags=re.UNICODE)
    return _squeeze(step)


def get_terms() -> list[str]:
    value = get_state("moderation_terms", DEFAULT_TERMS)
    if not isinstance(value, list):
        return list(DEFAULT_TERMS)
    return [term for term in (_normalize(v) for v in value) if term]


def set_terms(terms: list[str]) -> None:
    clean = []
    for value in terms:
        term = _normalize(value)
        if term and len(term) <= 64 and term not in clean:
            clean.append(term)
    save_state("moderation_terms", clean)


def is_enabled() -> bool:
    return bool(get_state("moderation_enabled", True))


def set_enabled(enabled: bool) -> None:
    save_state("moderation_enabled", bool(enabled))


def matches_term(text: str, terms: list[str] | None = None) -> str | None:
    """Two-pass abuse matching.

    Pass 1: exact word-boundary match on the normalized text.
    Pass 2: word-level fuzzy match (leet-decode + repeated-char squeeze +
    separator strip) so evasive spellings like 'madharchood', 'm@darch0d'
    or 'b.h.o.s.d.a' are still caught — WITHOUT false positives on words
    like 'gandhi' or 'wholesale' (short terms never substring-match).

    Returns the matched term, or None.
    """
    normalized = _normalize(text)
    term_list = terms if terms is not None else get_terms()
    for term in term_list:
        term = _normalize(term)
        if not term:
            continue
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized, re.UNICODE):
            return term
    # Fuzzy pass — word-level: whole-word equality, or substring only for
    # long terms (>=5 chars after squeeze) where false positives are unlikely.
    words = [_fuzzy_form(w) for w in re.split(r"\s+", normalized) if w]
    words = [w for w in words if w]
    if words:
        for term in term_list:
            fuzzy_term = _fuzzy_form(term)
            if not fuzzy_term:
                continue
            for word in words:
                if word == fuzzy_term or (len(fuzzy_term) >= 5 and fuzzy_term in word):
                    return term
    return None


# ---------------------------------------------------------------------------
# Anti-promotion: members may not advertise their own channels/groups.
# The linked (managed) channel is allowlisted; everything else is deleted.
# ---------------------------------------------------------------------------
_PROMO_URL_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(\+?[A-Za-z0-9_][A-Za-z0-9_+/-]*)",
    re.IGNORECASE,
)


def _managed_channel_username() -> str | None:
    """Cached @username of the linked channel (None if unknown)."""
    import links
    cid = links.get_channel_id()
    if not cid:
        return None
    cached = get_state("managed_channel_username")
    if cached:
        return str(cached)
    try:
        info = tg_api.get_chat(cid) or {}
        username = (info.get("username") or "").strip().lstrip("@")
        if username:
            save_state("managed_channel_username", username)
            return username
    except Exception:
        pass
    return None


def find_external_promo(text: str, entities: list | None = None) -> str | None:
    """Return a promo link if text advertises an external channel/group.

    Returns None when there is no t.me/telegram.me link, or when every link
    points at the managed (allowlisted) channel.
    """
    text = text or ""
    candidates: list[str] = []
    for ent in entities or []:
        if ent.get("type") == "url":
            start, length = ent.get("offset", 0), ent.get("length", 0)
            m = _PROMO_URL_RE.search(text[start:start + length])
            if m and m.group(1) not in candidates:
                candidates.append(m.group(1))
    for m in _PROMO_URL_RE.finditer(text):
        if m.group(1) not in candidates:
            candidates.append(m.group(1))
    if not candidates:
        return None
    allowed = (_managed_channel_username() or "").casefold()
    for cand in candidates:
        low = cand.casefold()
        # Invite links (+hash / joinchat) can't be mapped to the managed
        # channel -> treat as external promotion.
        if low.startswith("+") or low.startswith("joinchat/"):
            return "t.me/" + cand
        uname = low.split("/")[0]
        if not allowed or uname != allowed:
            return "t.me/" + cand
    return None


def _say_reply(chat_id: int, text: str, reply_to_message_id) -> None:
    """Send a notice as a reply to a message; fall back to a plain send."""
    r = tg_api.send_message(chat_id, text,
                            reply_to_message_id=reply_to_message_id)
    if not r.get("ok"):
        tg_api.send_message(chat_id, text)


def _mute(chat_id: int, user_id: int, minutes: int) -> dict:
    until_date = int(time.time()) + int(minutes) * 60
    return tg_api.restrict_chat_member(
        chat_id, user_id, _MUTE_PERMISSIONS, until_date=until_date
    )


def _strike(namespace: str, chat_id: int, user_id: int) -> int:
    """Increment a 24h-window strike counter; return the new count."""
    now = int(time.time())
    key = f"{namespace}:{chat_id}:{user_id}"
    previous = get_state(key, {}) or {}
    count = int(previous.get("count", 0) or 0)
    last_ts = int(previous.get("last_ts", 0) or 0)
    count = count + 1 if now - last_ts <= STRIKE_WINDOW_SECONDS else 1
    save_state(key, {"count": count, "last_ts": now})
    return count


def handle_group_message(msg: dict) -> bool:
    """Group message gate: abuse/promo moderation + Rose-style filters.

    - Profanity blocklist + external-promo links: delete + warn +
      temp-mute on repeat (owner/admins/bots exempt, never bans).
    - Rose-style content filters: when ANYONE (owner, admin, member) uses
      a saved keyword, the saved content is DELIVERED as-is as a reply —
      the triggering message is NEVER deleted. Exactly like @MissRose_bot.

    Returns True when the message was fully handled (stop processing).
    """
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    chat_id = chat.get("id")
    user_id = user.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Cache album parts (in-memory, cheap) so /filter can save whole
    # media groups later. Never raises, no API calls.
    remember_media(chat_id, msg)

    if (
        not is_enabled()
        or not text
        or text.startswith("/")
        or user.get("is_bot")
        or not chat_id
        or not user_id
    ):
        return False

    is_owner_msg = user_id == config.OWNER_CHAT_ID

    # SPEED: saare cheap regex checks PEHLE karo — getChatMember API call
    # (100-300ms network) sirf tab karo jab kuch match ho. Normal messages
    # par zero Telegram API calls lagti hain. Owner gaali/promo se exempt hai.
    term = None if is_owner_msg else matches_term(text)
    promo = None if (term or is_owner_msg) else find_external_promo(
        text, msg.get("entities") or msg.get("caption_entities"))

    # Rose-style content filters fire for EVERYONE — owner, admins and
    # members alike — and DELIVER content; they never delete anything.
    filt = None if (term or promo) else matches_filter(text)
    if filt:
        word, entry = filt
        name = (user.get("first_name") or user.get("username")
                or "Member").strip()
        ok = filters.deliver_filter(chat_id, word, entry,
                                    reply_to_message_id=msg.get("message_id"))
        log_action("filter_reply" if ok else "filter_failed", chat_id,
                   target_id=user_id, target_name=name,
                   reason=f"filter: {word}")
        return True

    if is_owner_msg:
        return False  # owner is never auto-moderated
    if not term and not promo:
        return False  # koi match nahi — zero API calls, fast path

    # Do not moderate or mute group admins; fail open if Telegram cannot
    # establish the member's current role.
    member = tg_api.get_chat_member(chat_id, user_id)
    if member.get("status") in ("administrator", "creator") or not member.get("status"):
        return False

    if term:
        count = _strike("moderation_strike", chat_id, user_id)
        name = (user.get("first_name") or user.get("username") or "Member").strip()
        mid = msg.get("message_id")
        # DELETE FIRST, announce AFTER — the notice claims deletion only
        # when the Telegram API actually confirmed it (never a fake claim).
        delete_result = tg_api.delete_message(chat_id, mid)
        deleted = bool(delete_result.get("ok"))
        if count == 1:
            notice = (
                f"⚠️ {name}, gaali wala message uda diya. Pehli aur aakhri "
                "warning — Repeat kiya to seedha 10-minute mute; ban nahi "
                "karunga.") if deleted else (
                f"⚠️ {name}, aisi language mat use karo. Message delete nahi "
                "ho paya — bot ko Delete Messages permission chahiye.")
        else:
            mute_result = _mute(chat_id, user_id, MUTE_MINUTES)
            if mute_result.get("ok"):
                notice = f"⏱ {name} ko repeat violation par {MUTE_MINUTES} minute mute kiya. Ban nahi kiya."
            else:
                notice = f"⚠️ {name}, repeat violation. Mute apply nahi hua—bot ko Restrict Members permission chahiye."
        # Owner rule: notice offender ke message ka REPLY hai (proof thread).
        # Jab message delete ho chuka ho to reply-target gayab ho sakta hai —
        # _say_reply ka plain-send fallback us case me sambhalta hai.
        _say_reply(chat_id, notice, mid)
        log_action("abuse_delete" if deleted else "abuse_delete_failed",
                   chat_id, target_id=user_id,
                   target_name=name, reason=term)
        if count > 1:
            log_action("abuse_automute", chat_id, target_id=user_id,
                       target_name=name, reason=term,
                       until=int(time.time()) + MUTE_MINUTES * 60)
        return True

    # External channel/group promotion: delete + warn, repeat -> mute.
    # The linked channel is allowlisted; owner/admins/bots are never touched.
    if promo:
        count = _strike("promo_strike", chat_id, user_id)
        name = (user.get("first_name") or user.get("username") or "Member").strip()
        mid = msg.get("message_id")
        # Delete first, then announce the REAL outcome (never a fake claim).
        delete_result = tg_api.delete_message(chat_id, mid)
        deleted = bool(delete_result.get("ok"))
        if count == 1:
            if deleted:
                notice = (
                    f"🚫 {name}, group me apne channel/group ka promotion allowed nahi hai. "
                    f"Message delete kar diya ({promo}). Dobara kiya to mute kar dunga."
                )
            else:
                notice = (
                    f"🚫 {name}, promotion allowed nahi hai ({promo}). Message delete "
                    "nahi ho paya — bot ko Delete Messages permission chahiye."
                )
        else:
            mute_result = _mute(chat_id, user_id, MUTE_MINUTES)
            if mute_result.get("ok"):
                notice = (
                    f"⏱ {name} ko bar-bar promotion karne par {MUTE_MINUTES} minute "
                    f"mute kiya. Real manager ki tarah — pehle samjhaya, phir action."
                )
            else:
                notice = (
                    f"⚠️ {name}, repeat promotion. Mute apply nahi hua—bot ko "
                    f"Restrict Members permission chahiye."
                )
        _say_reply(chat_id, notice, mid)
        log_action("promo_delete" if deleted else "promo_delete_failed",
                   chat_id, target_id=user_id,
                   target_name=name, reason=promo)
        if count > 1:
            log_action("promo_automute", chat_id, target_id=user_id,
                       target_name=name, reason=promo,
                       until=int(time.time()) + MUTE_MINUTES * 60)
        return True

    # (Rose-style content filters were already handled ABOVE — they deliver
    # content for everyone and never delete, exactly like @MissRose_bot.)

    return False


def manual_mute(chat_id: int, user_id: int, minutes: int) -> dict:
    """Owner-only command handler calls this after validating the target/time."""
    if minutes < 1 or minutes > MAX_MANUAL_MUTE_MINUTES:
        return {"ok": False, "description": "Mute duration must be 1–1440 minutes."}
    return _mute(chat_id, user_id, minutes)


# ---------------------------------------------------------------------------
# Rose-style mod tools: /filter /filters /stop /stopall /mute /tmute /unmute
# /ban /unban /delete /del /modlog
# (filter store + delivery live in filters.py)
# ---------------------------------------------------------------------------

_UNMUTE_PERMISSIONS = {key: True for key in _MUTE_PERMISSIONS}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_MAX_DURATION_SECONDS = 364 * 86400  # Telegram until_date limit

_BOT_ID: int | None = None


def parse_duration(value: str) -> int | None:
    """Parse Rose-style durations like '45s', '30m', '2h', '1d', '1w'.

    Returns seconds, or None when invalid / zero / longer than 364 days.
    """
    match = _DURATION_RE.match(str(value or ""))
    if not match:
        return None
    seconds = int(match.group(1)) * _DURATION_MULTIPLIERS[match.group(2).lower()]
    if seconds <= 0 or seconds > _MAX_DURATION_SECONDS:
        return None
    return seconds


def _human_duration_token(token: str) -> str:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", str(token or ""), re.IGNORECASE)
    if not match:
        return str(token or "").strip()
    words = {"s": "second", "m": "minute", "h": "ghante", "d": "din", "w": "hafte"}
    return f"{match.group(1)} {words[match.group(2).lower()]}"


def log_action(action: str, chat_id: int, target_id=None, target_name: str = "",
               by_id: int = 0, by_name: str = "auto", reason: str = "",
               until=None) -> None:
    """Persist a moderation action; works on MongoDB and the IMDB fallback."""
    try:
        get_db()["mod_actions"].insert_one({
            "action": action,
            "chat_id": chat_id,
            "target_id": target_id,
            "target_name": target_name or "",
            "by_id": by_id,
            "by_name": by_name or "",
            "reason": reason or "",
            "until": until,
            "ts": time.time(),
        })
    except Exception:
        pass


# ---------- helpers ----------

def _bot_id() -> int | None:
    """Cached bot user id; never caches a failed lookup."""
    global _BOT_ID
    if _BOT_ID is not None:
        return _BOT_ID
    try:
        value = int((tg_api.get_me() or {}).get("id") or 0)
    except Exception:
        return None
    _BOT_ID = value or None
    return _BOT_ID


def _is_owner(uid: int) -> bool:
    return bool(uid) and uid == config.OWNER_CHAT_ID


def _is_privileged(uid: int, chat_id: int | None = None) -> bool:
    """Owner, configured secondary admin, or a live Telegram group admin."""
    if _is_owner(uid):
        return True
    admin_id = int(getattr(config, "ADMIN_CHAT_ID", 0) or 0)
    if admin_id and uid == admin_id:
        return True
    # Live Telegram group admins may also use basic mod tools.
    if chat_id and uid:
        try:
            member = tg_api.get_chat_member(chat_id, uid) or {}
            if member.get("status") in ("administrator", "creator"):
                return True
        except Exception:
            pass
    return False


def _display_name(user: dict, fallback: str = "") -> str:
    name = (user.get("first_name") or user.get("username") or "").strip()
    return name or fallback


_STOPWORDS_TARGET = {
    "bro", "bhai", "boss", "bhaiya", "yaar", "dost", "mr", "mrs",
    "kr", "kar", "karo", "krdo", "kardo", "kijiye", "karna", "karni",
    "de", "do", "dena", "dedo", "dijiye", "plz", "please", "jara",
    "isko", "usko", "inko", "ise", "use", "unhe", "unko",
    "ko", "se", "ke", "liye", "and", "the", "this", "that",
    "min", "mins", "minute", "minutes", "ghanta", "ghante", "ghanty",
    "hour", "hours", "din", "dino", "days", "day", "week", "hafte",
    "sec", "secs", "second", "seconds", "time", "thoda",
}


def _member_in_group(chat_id: int, user_id: int) -> bool:
    """True when Telegram confirms the user is currently in this group.

    Name-based lookups MUST be verified — an old memory hit (user left,
    or a stale partial match) would otherwise unmute/ban the WRONG person.
    """
    if not chat_id or not user_id:
        return False
    try:
        member = tg_api.get_chat_member(chat_id, user_id) or {}
        return member.get("status") in ("member", "administrator",
                                        "creator", "restricted")
    except Exception:
        return False


def _resolve_target(msg: dict, arg: str | None,
                    chat_id: int | None = None) -> tuple[int | None, str]:
    """Resolve a mod-command target.

    Order: reply -> numeric id -> @username -> FIRST-NAME/NAME lookup.
    Name lookup searches stored messages for a matching username/first_name
    (partial, case-insensitive), most recent first — so the owner can simply
    say 'unmute rich ko' instead of tagging. Every name-based hit is then
    VERIFIED live (getChatMember) so a stale/wrong match can never receive
    the action.
    """
    reply_from = (msg.get("reply_to_message") or {}).get("from") or {}
    if reply_from.get("id"):
        return reply_from["id"], _display_name(reply_from, f"user {reply_from['id']}")
    arg = (arg or "").strip()
    if not arg:
        return None, ""
    if re.fullmatch(r"\d+", arg):
        return int(arg), f"user {arg}"
    wanted = arg[1:] if arg.startswith("@") else arg
    wanted = wanted.casefold().strip()
    if not wanted:
        return None, ""
    exact_uname = arg.startswith("@")
    try:
        docs = list(get_db()["messages"].find({}))
    except Exception:
        docs = []
    if chat_id:
        same_chat = [d for d in docs if d.get("chat_id") == chat_id]
        docs = same_chat or docs
    docs.sort(key=lambda d: d.get("date") or 0, reverse=True)
    # Collect candidates: exact matches first, then partials.
    exact, partial = [], []
    for doc in docs:
        if not doc.get("user_id"):
            continue
        username = (doc.get("username") or "").casefold()
        first = (doc.get("first_name") or "").casefold()
        if exact_uname:
            if username == wanted:
                exact.append(doc)
            continue
        if first == wanted or username == wanted:
            exact.append(doc)
        elif len(wanted) >= 3 and (wanted in username or wanted in first):
            partial.append(doc)
    # Best candidate = exact match verified in-group, else partial verified.
    for pool in (exact, partial):
        for doc in pool:
            uid = doc.get("user_id")
            if _member_in_group(chat_id, uid):
                label = (doc.get("first_name")
                         or "@" + str(doc.get("username") or wanted))
                return uid, label
    return None, ""


def _protect_target(chat_id: int, target_id: int, issuer_id: int) -> str | None:
    """Return a Hinglish refusal when the target must not be touched."""
    if target_id == config.OWNER_CHAT_ID:
        return "❌ Owner ko touch nahi kar sakta."
    bot_id = _bot_id()
    if bot_id and target_id == bot_id:
        return "❌ Khud pe action nahi le sakta — main khud bot hun."
    if issuer_id and target_id == issuer_id:
        return "❌ Khud pe ye action nahi le sakta."
    try:
        member = tg_api.get_chat_member(chat_id, target_id) or {}
    except Exception:
        member = {}
    if member.get("status") in ("administrator", "creator"):
        return "❌ Group admins pe ye action nahi le sakta."
    # Fail-open for explicit commands: an unknown role does not block.
    return None


def _bot_has_right(chat_id: int, right: str) -> bool:
    bot_id = _bot_id()
    if not bot_id:
        return False
    try:
        member = tg_api.get_chat_member(chat_id, bot_id) or {}
    except Exception:
        return False
    if member.get("status") == "creator":
        return True
    return bool(member.get("status") == "administrator" and member.get(right))


# ---------- command dispatcher ----------

# Rose accepts commands with both / and ! prefixes (e.g. !filter loader).
_MOD_CMD_RE = re.compile(r"^[/!](\w+?)(?:@\w+)?(?:\s+|$)", re.IGNORECASE)
_MOD_COMMANDS = {
    "filter", "filters", "stop", "stopall",
    "mute", "tmute", "unmute",
    "ban", "unban",
    "delete", "del",
    "modlog",
    "warn", "warnings", "resetwarns",
    "kick", "purge", "pin", "unpin",
    "purgeuser",
}
_OWNER_ONLY_COMMANDS = {"ban", "unban", "stopall", "kick", "purgeuser"}
_WARN_LIMIT = 3
_WARN_AUTOMUTE_MINUTES = 60

_DENY_PRIVILEGED = "❌ Ye command sirf owner/admin ke liye hai."
_DENY_OWNER = "❌ Ye command sirf owner ke liye hai."
_NEED_RESTRICT = ("❌ Bot ke paas Restrict Members permission nahi hai. "
                  "Bot ko group admin banao with ban/restrict rights.")
_NEED_DELETE = ("❌ Bot ke paas Delete Messages permission nahi hai. "
                "Bot ko group admin banao with delete rights.")
_NEED_PIN = ("❌ Bot ke paas Pin Messages permission nahi hai. "
             "Bot ko group admin banao with pin rights.")


def handle_mod_command(msg: dict, uid: int, chat_id: int) -> bool:
    """Handle Rose-style group mod commands. True = handled, stop processing.

    The caller guarantees this is a managed group chat.
    """
    text = (msg.get("text") or "").strip()
    match = _MOD_CMD_RE.match(text)
    if not match:
        return False
    cmd = match.group(1).lower()
    if cmd not in _MOD_COMMANDS:
        return False
    args = text[match.end():].strip()
    issuer = msg.get("from") or {}
    by_name = _display_name(issuer, f"user {uid}")
    cmd_mid = msg.get("message_id")

    def say(reply_text: str) -> bool:
        # Command responses thread as a reply to the command itself.
        _say_reply(chat_id, reply_text, cmd_mid)
        return True

    # ---- permission gates ----
    if cmd in _OWNER_ONLY_COMMANDS:
        if not _is_owner(uid):
            return say(_DENY_OWNER)
    elif not _is_privileged(uid, chat_id):
        return say(_DENY_PRIVILEGED)

    # ---- /filter /filters /stop /stopall (Rose-style, from filters.py) ----
    if cmd in ("filter", "filters", "stop", "stopall"):
        if cmd == "filter":
            log_action("filter_add", chat_id, by_id=uid, by_name=by_name,
                       reason=f"filter command by {by_name}")
        elif cmd == "stop":
            log_action("filter_remove", chat_id, by_id=uid,
                       by_name=by_name, reason=f"stop: {args[:64]}")
        elif cmd == "stopall":
            log_action("filter_clear", chat_id, by_id=uid, by_name=by_name)
        return filters.handle_filter_command(msg, uid, chat_id, cmd, args,
                                              say)

    # ---- /modlog ----
    if cmd == "modlog":
        try:
            docs = list(get_db()["mod_actions"].find({"chat_id": chat_id}))
        except Exception:
            docs = []
        docs.sort(key=lambda d: d.get("ts") or 0, reverse=True)
        docs = docs[:10]
        if not docs:
            return say("Abhi tak koi mod action log nahi hai.")
        lines = ["📝 Recent mod actions:"]
        for doc in docs:
            target = doc.get("target_name") or (
                f"user {doc.get('target_id')}" if doc.get("target_id") else "—")
            reason = f" ({doc.get('reason')})" if doc.get("reason") else ""
            lines.append(f"• {doc.get('action')} → {target}{reason} — by {doc.get('by_name')}")
        return say("\n".join(lines))

    # ---- /purgeuser (owner only) — full reset for a user ----
    # Owner's spoken form: 'X ka complete data delete kar do aur unmute kro'.
    # Wipes learned memory (messages + Q&A), warnings, strikes, mute state,
    # and lifts any active restriction. Reports exactly what was done.
    if cmd == "purgeuser":
        target_arg = tokens[0] if (tokens := args.split()) else None
        target_id, target_name = _resolve_target(msg, target_arg, chat_id)
        if not target_id:
            return say("Usage: /purgeuser <reply|user_id|@username|naam>\n"
                       "User ka pura data delete + warnings/strikes reset + "
                       "unmute — sab ek command me.")
        if target_id == config.OWNER_CHAT_ID:
            return say("❌ Owner ka data kabhi delete nahi hota.")
        wiped_msgs = wiped_qa = 0
        try:
            wiped_msgs = get_db()["messages"].delete_many(
                {"user_id": target_id}).deleted_count
            wiped_qa = get_db()["qa"].delete_many(
                {"user_id": target_id}).deleted_count
        except Exception:
            pass
        for key in (f"warn_count:{chat_id}:{target_id}",
                    f"moderation_strike:{chat_id}:{target_id}",
                    f"promo_strike:{chat_id}:{target_id}",
                    f"mod_mute:{chat_id}:{target_id}"):
            save_state(key, 0)
        unmuted = False
        if _bot_has_right(chat_id, "can_restrict_members"):
            r = tg_api.restrict_chat_member(
                chat_id, target_id, _UNMUTE_PERMISSIONS, until_date=None)
            unmuted = bool(r.get("ok"))
        log_action("purgeuser", chat_id, target_id=target_id,
                   target_name=target_name, by_id=uid, by_name=by_name,
                   reason=f"msgs:{wiped_msgs} qa:{wiped_qa} unmute:{unmuted}")
        parts = [f"🧹 {target_name} ka pura reset ho gaya:",
                 f"• Memory me se {wiped_msgs} messages aur {wiped_qa} Q&A delete",
                 "• Warnings aur strikes reset"]
        parts.append("• Unmute kar diya ✅" if unmuted else
                     "• Unmute nahi ho paya — Restrict Members permission chahiye")
        return say("\n".join(parts))

    # ---- /warn (privileged; 3 warns = 1h auto-mute + reset) ----
    if cmd == "warn":
        target_arg = args.split()[0] if args.split() else None
        reason = " ".join(args.split()[1:]) if args.split() else ""
        target_id, target_name = _resolve_target(msg, target_arg, chat_id)
        if not target_id:
            return say("Usage: /warn <reply|@user|naam> [reason]\n"
                       "3 warn hone par 1 ghante ka auto-mute hota hai.")
        blocked = _protect_target(chat_id, target_id, uid)
        if blocked:
            return say(blocked)
        key = f"warn_count:{chat_id}:{target_id}"
        count = int(get_state(key, 0) or 0) + 1
        save_state(key, count)
        log_action("warn", chat_id, target_id=target_id, target_name=target_name,
                   by_id=uid, by_name=by_name, reason=reason)
        if count >= _WARN_LIMIT:
            save_state(key, 0)
            if not _bot_has_right(chat_id, "can_restrict_members"):
                return say(f"⚠️ {target_name} ko {_WARN_LIMIT}rd warning mil gayi, "
                           "par mute nahi kar saka — Restrict Members permission chahiye.")
            until = int(time.time()) + _WARN_AUTOMUTE_MINUTES * 60
            result = tg_api.restrict_chat_member(
                chat_id, target_id, _MUTE_PERMISSIONS, until_date=until)
            if result.get("ok"):
                log_action("warn_automute", chat_id, target_id=target_id,
                           target_name=target_name, by_id=uid, by_name=by_name,
                           reason=f"{count} warnings", until=until)
                return say(f"⛔ {target_name} {_WARN_LIMIT} warnings paar kar gaya — "
                           f"{_WARN_AUTOMUTE_MINUTES} minute ka auto-mute + warnings reset.")
            return say(f"❌ Auto-mute fail: {result.get('description') or 'unknown error'}")
        left = _WARN_LIMIT - count
        msg_txt = (f"⚠️ {target_name} ko warning #{count}/{_WARN_LIMIT}"
                   + (f" — {reason}" if reason else "")
                   + f"\nAur {left} warning par auto-mute (1 hour) hoga.")
        return say(msg_txt)

    # ---- /warnings ----
    if cmd == "warnings":
        target_arg = args.split()[0] if args.split() else None
        target_id, target_name = _resolve_target(msg, target_arg, chat_id)
        if not target_id:
            return say("Usage: /warnings <reply|@user|naam>")
        count = int(get_state(f"warn_count:{chat_id}:{target_id}", 0) or 0)
        if count:
            return say(f"📊 {target_name} ke paas {count}/{_WARN_LIMIT} warnings hain.")
        return say(f"✅ {target_name} ke naam par koi warning nahi hai.")

    # ---- /resetwarns ----
    if cmd == "resetwarns":
        target_arg = args.split()[0] if args.split() else None
        target_id, target_name = _resolve_target(msg, target_arg, chat_id)
        if not target_id:
            return say("Usage: /resetwarns <reply|@user|naam>")
        save_state(f"warn_count:{chat_id}:{target_id}", 0)
        log_action("resetwarns", chat_id, target_id=target_id,
                   target_name=target_name, by_id=uid, by_name=by_name)
        return say(f"♻️ {target_name} ki saari warnings reset kar di.")

    # ---- /kick (owner only) — remove member; can rejoin via link ----
    if cmd == "kick":
        target_arg = args.split()[0] if args.split() else None
        reason = " ".join(args.split()[1:]) if args.split() else ""
        target_id, target_name = _resolve_target(msg, target_arg, chat_id)
        if not target_id:
            return say("Usage: /kick <reply|@user|naam> [reason]\n"
                       "Member group se bahar nikalta hai, link se wapas aa sakta hai.")
        blocked = _protect_target(chat_id, target_id, uid)
        if blocked:
            return say(blocked)
        if not _bot_has_right(chat_id, "can_restrict_members"):
            return say(_NEED_RESTRICT)
        ban_r = tg_api.ban_chat_member(chat_id, target_id)
        if not ban_r.get("ok"):
            return say(f"❌ Kick fail: {ban_r.get('description') or 'unknown error'}")
        tg_api.unban_chat_member(chat_id, target_id, only_if_banned=False)
        log_action("kick", chat_id, target_id=target_id, target_name=target_name,
                   by_id=uid, by_name=by_name, reason=reason)
        confirm = f"👢 {target_name} ko group se nikal diya. "
        confirm += "(link se wapas aa sakta hai)"
        if reason:
            confirm += f"\nReason: {reason}"
        return say(confirm)

    # ---- /purge N (reply-based bulk delete, max 100) ----
    if cmd == "purge":
        replied = msg.get("reply_to_message") or {}
        start_id = replied.get("message_id")
        if not start_id:
            return say("Purane message pe reply karke /purge <count> bhejo — "
                       "reply se lekar is command tak ke beech ke saare "
                       "messages delete ho jayenge (max 100).")
        try:
            count = min(int(args.split()[0] or 0) if args.split() else 0, 100)
        except ValueError:
            count = 0
        end_id = cmd_mid
        if count <= 0:
            count = min(end_id - start_id, 100)
        ids = list(range(start_id, min(start_id + count, end_id)))
        if not ids:
            return say("❌ Delete karne layak koi message range nahi mila.")
        if not _bot_has_right(chat_id, "can_delete_messages"):
            return say(_NEED_DELETE)
        deleted = 0
        for mid in ids:
            r = tg_api.delete_message(chat_id, mid)
            if r.get("ok"):
                deleted += 1
        log_action("purge", chat_id, by_id=uid, by_name=by_name,
                   reason=f"{deleted} messages")
        # The /purge command itself bhi clean ho jaye
        tg_api.delete_message(chat_id, cmd_mid)
        return True  # command message deleted — no text reply needed

    # ---- /pin (reply) ----
    if cmd == "pin":
        replied = msg.get("reply_to_message") or {}
        if not replied.get("message_id"):
            return say("Pin karne wale message pe reply karke /pin bhejo.")
        if not _bot_has_right(chat_id, "can_pin_messages"):
            return say(_NEED_PIN)
        r = tg_api.pin_chat_message(chat_id, replied["message_id"], silent=True)
        if r.get("ok"):
            log_action("pin", chat_id, by_id=uid, by_name=by_name)
            return say("📌 Message pin kar diya (silent — sabko notification nahi gaya).")
        return say(f"❌ Pin fail: {r.get('description') or 'unknown error'}")

    # ---- /unpin ----
    if cmd == "unpin":
        if args.strip().casefold() == "all":
            r = tg_api.unpin_all_chat_messages(chat_id)
            if r.get("ok"):
                log_action("unpin_all", chat_id, by_id=uid, by_name=by_name)
                return say("📌 Saare pinned messages unpin kar diye.")
            return say(f"❌ Unpin fail: {r.get('description') or 'unknown error'}")
        r = tg_api.unpin_chat_message(chat_id)
        if r.get("ok"):
            log_action("unpin", chat_id, by_id=uid, by_name=by_name)
            return say("📌 Sabse upar wala pinned message unpin kar diya.")
        return say(f"❌ Unpin fail: {r.get('description') or 'unknown error'}")

    # ---- /delete & /del (reply-only) ----
    if cmd in ("delete", "del"):
        replied = msg.get("reply_to_message") or {}
        if not replied.get("message_id"):
            return say("Kisi message pe reply karke /delete bhejo.")
        if not _bot_has_right(chat_id, "can_delete_messages"):
            return say(_NEED_DELETE)
        result = tg_api.delete_message(chat_id, replied["message_id"])
        if result.get("ok"):
            log_action("delete", chat_id, target_id=(replied.get("from") or {}).get("id"),
                       target_name=_display_name(replied.get("from") or {}),
                       by_id=uid, by_name=by_name)
            return say("🗑 Message delete kar diya.")
        return say(f"❌ Delete fail: {result.get('description') or 'unknown error'}")

    # ---- target-based commands: /mute /tmute /unmute /ban /unban ----
    tokens = args.split()
    duration_token = None
    duration_seconds = None
    target_arg = tokens[0] if tokens else None
    reason = ""

    if cmd == "tmute":
        idx = next((i for i, tok in enumerate(tokens) if parse_duration(tok)), None)
        if idx is None:
            return say("Usage: /tmute <reply|user_id> <30m|2h|1d|1w> [reason]")
        duration_token = tokens[idx]
        duration_seconds = parse_duration(duration_token)
        rest = tokens[:idx] + tokens[idx + 1:]
        has_reply = bool((msg.get("reply_to_message") or {}).get("from", {}).get("id"))
        if has_reply:
            target_arg = None
            reason = " ".join(rest)
        else:
            if not rest:
                return say("Usage: /tmute <reply|user_id> <30m|2h|1d|1w> [reason]")
            target_arg = rest[0]
            reason = " ".join(rest[1:])
    else:
        reply_from = (msg.get("reply_to_message") or {}).get("from") or {}
        if reply_from.get("id"):
            target_arg = None
            reason = args
        elif tokens:
            target_arg = tokens[0]
            reason = " ".join(tokens[1:])

    target_id, target_name = _resolve_target(msg, target_arg, chat_id)
    if not target_id:
        hint = {
            "mute": "Usage: /mute <reply|user_id|@username|naam> [reason]",
            "unmute": "Usage: /unmute <reply|user_id|@username|naam>",
            "ban": "Usage: /ban <reply|user_id|@username|naam> [reason]",
            "unban": "Usage: /unban <user_id|@username|naam>",
        }[cmd]
        return say("❌ User nahi mila. Reply karke, user ID deke, ya uske "
                   "naam se try karo (jaise 'unmute rich ko').\n" + hint)

    blocked = _protect_target(chat_id, target_id, uid)
    if blocked:
        return say(blocked)
    if not _bot_has_right(chat_id, "can_restrict_members"):
        return say(_NEED_RESTRICT)

    # ---- /ban (owner only; never automatic) ----
    if cmd == "ban":
        result = tg_api.ban_chat_member(chat_id, target_id)
        if result.get("ok"):
            log_action("ban", chat_id, target_id=target_id, target_name=target_name,
                       by_id=uid, by_name=by_name, reason=reason)
            confirm = f"🔨 {target_name} ko ban kar diya."
            if reason:
                confirm += f"\nReason: {reason}"
            return say(confirm)
        return say(f"❌ Ban fail: {result.get('description') or 'unknown error'}")

    # ---- /unban (owner only) ----
    if cmd == "unban":
        result = tg_api.unban_chat_member(chat_id, target_id)
        if result.get("ok"):
            log_action("unban", chat_id, target_id=target_id, target_name=target_name,
                       by_id=uid, by_name=by_name)
            return say(f"✅ {target_name} ko unban kar diya.")
        return say(f"❌ Unban fail: {result.get('description') or 'unknown error'}")

    # ---- /mute (indefinite) ----
    if cmd == "mute":
        result = tg_api.restrict_chat_member(
            chat_id, target_id, _MUTE_PERMISSIONS, until_date=None)
        if result.get("ok"):
            save_state(f"mod_mute:{chat_id}:{target_id}",
                       {"until": None, "reason": reason})
            log_action("mute", chat_id, target_id=target_id, target_name=target_name,
                       by_id=uid, by_name=by_name, reason=reason)
            confirm = f"🔇 {target_name} ko mute kar diya."
            if reason:
                confirm += f"\nReason: {reason}"
            return say(confirm)
        return say(f"❌ Mute fail: {result.get('description') or 'unknown error'}")

    # ---- /tmute ----
    if cmd == "tmute":
        until = int(time.time()) + duration_seconds
        result = tg_api.restrict_chat_member(
            chat_id, target_id, _MUTE_PERMISSIONS, until_date=until)
        if result.get("ok"):
            save_state(f"mod_mute:{chat_id}:{target_id}",
                       {"until": until, "reason": reason})
            log_action("tmute", chat_id, target_id=target_id, target_name=target_name,
                       by_id=uid, by_name=by_name, reason=reason, until=until)
            confirm = (f"⏱ {target_name} ko {_human_duration_token(duration_token)} "
                       f"ke liye mute kiya.")
            if reason:
                confirm += f"\nReason: {reason}"
            return say(confirm)
        return say(f"❌ Tempmute fail: {result.get('description') or 'unknown error'}")

    # ---- /unmute ----
    result = tg_api.restrict_chat_member(
        chat_id, target_id, _UNMUTE_PERMISSIONS, until_date=None)
    if result.get("ok"):
        save_state(f"mod_mute:{chat_id}:{target_id}",
                   {"until": None, "reason": "manually unmuted"})
        log_action("unmute", chat_id, target_id=target_id, target_name=target_name,
                   by_id=uid, by_name=by_name)
        return say(f"🔊 {target_name} ko unmute kar diya.")
    return say(f"❌ Unmute fail: {result.get('description') or 'unknown error'}")