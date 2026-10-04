"""Owner-controlled group moderation with Rose-style mod tools.

Auto-moderation (profanity + filters) deletes/warns/mutes but intentionally
never bans. Bans happen only via an explicit owner /ban command.
"""
from __future__ import annotations

import re
import time
import unicodedata

import config
from db import get_db, get_state, save_state
import tg_api


DEFAULT_TERMS = [
    "madarchod", "behenchod", "chutiya", "gandu", "bkl", "bc", "mc",
    "fuck", "shit", "bitch", "asshole",
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


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


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
    normalized = _normalize(text)
    for term in terms if terms is not None else get_terms():
        term = _normalize(term)
        if not term:
            continue
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized, re.UNICODE):
            return term
    return None


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
    """Delete a configured profanity match; warn once, then temporary mute.

    This intentionally never bans. It only operates when called for the
    configured managed group.
    """
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    chat_id = chat.get("id")
    user_id = user.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if (
        not is_enabled()
        or not text
        or text.startswith("/")
        or user.get("is_bot")
        or not chat_id
        or not user_id
        or user_id == config.OWNER_CHAT_ID
    ):
        return False

    # Do not moderate or mute group admins; fail open if Telegram cannot
    # establish the member's current role.
    member = tg_api.get_chat_member(chat_id, user_id)
    if member.get("status") in ("administrator", "creator") or not member.get("status"):
        return False

    term = matches_term(text)
    if term:
        count = _strike("moderation_strike", chat_id, user_id)

        delete_result = tg_api.delete_message(chat_id, msg.get("message_id"))
        delete_failed = not delete_result.get("ok")
        name = (user.get("first_name") or user.get("username") or "Member").strip()
        if count == 1:
            notice = f"⚠️ {name}, gaali wala message uda diya. Pehli aur aakhri warning — Repeat kiya to seedha 10-minute mute; ban nahi karunga."
        else:
            mute_result = _mute(chat_id, user_id, MUTE_MINUTES)
            if mute_result.get("ok"):
                notice = f"⏱ {name} ko repeat violation par {MUTE_MINUTES} minute mute kiya. Ban nahi kiya."
            else:
                notice = f"⚠️ {name}, repeat violation. Mute apply nahi hua—bot ko Restrict Members permission chahiye."
        if delete_failed:
            notice += " Message delete nahi hua—bot ko Delete Messages permission chahiye."
        tg_api.send_message(chat_id, notice)
        return True

    # Owner-configured filters (Rose-style). Same guards as above apply:
    # owner/admins/commands/bots are never touched here.
    filt = matches_filter(text)
    if filt:
        word, entry = filt
        name = (user.get("first_name") or user.get("username") or "Member").strip()
        reply_text = (entry or {}).get("reply")
        if reply_text:
            tg_api.send_message(
                chat_id, reply_text, reply_to_message_id=msg.get("message_id")
            )
            log_action("filter_reply", chat_id, target_id=user_id,
                       target_name=name, reason=f"filter: {word}")
            return True
        # Bare filter = blocklist: delete + warn, repeat -> 10-min mute.
        # Never bans.
        count = _strike("filter_strike", chat_id, user_id)
        delete_result = tg_api.delete_message(chat_id, msg.get("message_id"))
        delete_failed = not delete_result.get("ok")
        if count == 1:
            notice = f"⚠️ {name}, ye word filter me hai — message delete kiya."
        else:
            mute_result = _mute(chat_id, user_id, MUTE_MINUTES)
            if mute_result.get("ok"):
                notice = f"⏱ {name} ko repeat filter violation par {MUTE_MINUTES} minute mute kiya. Ban nahi kiya."
            else:
                notice = f"⚠️ {name}, repeat filter violation. Mute apply nahi hua—bot ko Restrict Members permission chahiye."
        if delete_failed:
            notice += " Message delete nahi hua—bot ko Delete Messages permission chahiye."
        tg_api.send_message(chat_id, notice)
        log_action("filter_delete", chat_id, target_id=user_id,
                   target_name=name, reason=f"filter: {word}")
        if count > 1:
            log_action("filter_automute", chat_id, target_id=user_id,
                       target_name=name, reason=f"filter: {word}",
                       until=int(time.time()) + MUTE_MINUTES * 60)
        return True

    return False


def manual_mute(chat_id: int, user_id: int, minutes: int) -> dict:
    """Owner-only command handler calls this after validating the target/time."""
    if minutes < 1 or minutes > MAX_MANUAL_MUTE_MINUTES:
        return {"ok": False, "description": "Mute duration must be 1–1440 minutes."}
    return _mute(chat_id, user_id, minutes)


# ---------------------------------------------------------------------------
# Rose-style mod tools: /filter /filters /stop /stopall /mute /tmute /unmute
# /ban /unban /delete /del /modlog
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


# ---------- filter store ----------

def get_filters() -> dict:
    value = get_state("mod_filters", {})
    return dict(value) if isinstance(value, dict) else {}


def add_filter(word: str, reply: str | None, by_uid: int) -> str | None:
    """Save a filter. Returns the normalized word, or None when invalid."""
    word = _normalize(word)
    if not word or len(word) > 64:
        return None
    filters = get_filters()
    filters[word] = {
        "reply": (reply or "").strip() or None,
        "by": int(by_uid or 0),
        "ts": time.time(),
    }
    save_state("mod_filters", filters)
    return word


def remove_filter(word: str) -> bool:
    word = _normalize(word)
    filters = get_filters()
    if word not in filters:
        return False
    del filters[word]
    save_state("mod_filters", filters)
    return True


def clear_filters() -> None:
    save_state("mod_filters", {})


def matches_filter(text: str) -> tuple[str, dict] | None:
    """Return (word, entry) for the first matching filter, else None."""
    normalized = _normalize(text)
    for word, entry in get_filters().items():
        if not word:
            continue
        if " " in word:
            if word in normalized:
                return word, entry
        elif re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized, re.UNICODE):
            return word, entry
    return None


# ---------- action log ----------

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


def _is_privileged(uid: int) -> bool:
    """Owner, or the configured secondary admin (when set)."""
    if _is_owner(uid):
        return True
    admin_id = int(getattr(config, "ADMIN_CHAT_ID", 0) or 0)
    return bool(admin_id) and uid == admin_id


def _display_name(user: dict, fallback: str = "") -> str:
    name = (user.get("first_name") or user.get("username") or "").strip()
    return name or fallback


def _resolve_target(msg: dict, arg: str | None) -> tuple[int | None, str]:
    """Resolve a mod-command target: reply -> numeric id -> @username lookup."""
    reply_from = (msg.get("reply_to_message") or {}).get("from") or {}
    if reply_from.get("id"):
        return reply_from["id"], _display_name(reply_from, f"user {reply_from['id']}")
    arg = (arg or "").strip()
    if re.fullmatch(r"\d+", arg):
        return int(arg), f"user {arg}"
    if arg.startswith("@") and len(arg) > 1:
        wanted = arg[1:].casefold()
        best = None
        try:
            docs = get_db()["messages"].find({})
        except Exception:
            docs = []
        for doc in docs:
            username = doc.get("username") or ""
            if username and username.casefold() == wanted:
                if best is None or (doc.get("date") or 0) > (best.get("date") or 0):
                    best = doc
        if best and best.get("user_id"):
            return best["user_id"], "@" + (best.get("username") or wanted)
        return None, ""
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

_MOD_CMD_RE = re.compile(r"^/(\w+?)(?:@\w+)?(?:\s+|$)", re.IGNORECASE)
_MOD_COMMANDS = {
    "filter", "filters", "stop", "stopall",
    "mute", "tmute", "unmute",
    "ban", "unban",
    "delete", "del",
    "modlog",
}
_OWNER_ONLY_COMMANDS = {"ban", "unban", "stopall"}

_DENY_PRIVILEGED = "❌ Ye command sirf owner/admin ke liye hai."
_DENY_OWNER = "❌ Ye command sirf owner ke liye hai."
_NEED_RESTRICT = ("❌ Bot ke paas Restrict Members permission nahi hai. "
                  "Bot ko group admin banao with ban/restrict rights.")
_NEED_DELETE = ("❌ Bot ke paas Delete Messages permission nahi hai. "
                "Bot ko group admin banao with delete rights.")


def _split_filter_args(args: str) -> tuple[str | None, str]:
    """Split '/filter' args into (word, reply). Supports quoted phrases."""
    args = args.strip()
    if args.startswith('"'):
        end = args.find('"', 1)
        if end == -1:
            return None, ""
        return args[1:end], args[end + 1:].strip()
    parts = args.split(None, 1)
    if not parts:
        return None, ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


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

    def say(reply_text: str) -> bool:
        tg_api.send_message(chat_id, reply_text)
        return True

    # ---- permission gates ----
    if cmd in _OWNER_ONLY_COMMANDS:
        if not _is_owner(uid):
            return say(_DENY_OWNER)
    elif not _is_privileged(uid):
        return say(_DENY_PRIVILEGED)

    # ---- /filter ----
    if cmd == "filter":
        word, reply = _split_filter_args(args)
        if not word:
            return say("Usage: /filter <word> [reply text]\n"
                       "Reply text diya to us word par auto-reply hoga; "
                       "nahi diya to message auto-delete hoga (blocklist).")
        saved = add_filter(word, reply, uid)
        if not saved:
            return say("❌ Filter word invalid hai (khali ya bahut lamba).")
        log_action("filter_add", chat_id, by_id=uid, by_name=by_name,
                   reason=f"filter: {saved}")
        if reply:
            return say(f'✅ Filter save ho gaya: "{saved}"\n'
                       f'Ab is word par bot auto-reply karega.')
        return say(f'✅ Filter save ho gaya: "{saved}"\n'
                   f'Ab is word wala message auto-delete hoga.')

    # ---- /filters ----
    if cmd == "filters":
        filters = get_filters()
        if not filters:
            return say("Koi filter nahi hai. /filter <word> [reply] se add karo.")
        lines = ["📋 Group filters:"]
        for word, entry in sorted(filters.items()):
            reply_text = (entry or {}).get("reply")
            if reply_text:
                preview = reply_text if len(reply_text) <= 40 else reply_text[:37] + "..."
                lines.append(f"• {word} → reply: {preview}")
            else:
                lines.append(f"• {word} → auto-delete")
        return say("\n".join(lines))

    # ---- /stop ----
    if cmd == "stop":
        word = _split_filter_args(args)[0]
        word = _normalize(word or "")
        if not word:
            return say("Usage: /stop <word>")
        if remove_filter(word):
            log_action("filter_remove", chat_id, by_id=uid, by_name=by_name,
                       reason=f"filter: {word}")
            return say(f'🗑 Filter "{word}" hata diya.')
        return say(f'❌ "{word}" naam ka filter nahi mila.')

    # ---- /stopall (owner only) ----
    if cmd == "stopall":
        clear_filters()
        log_action("filter_clear", chat_id, by_id=uid, by_name=by_name)
        return say("🗑 Saare filters clear kar diye.")

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

    target_id, target_name = _resolve_target(msg, target_arg)
    if not target_id:
        hint = {
            "mute": "Usage: /mute <reply|user_id|@username> [reason]",
            "unmute": "Usage: /unmute <reply|user_id|@username>",
            "ban": "Usage: /ban <reply|user_id|@username> [reason]",
            "unban": "Usage: /unban <user_id|@username>",
        }[cmd]
        return say("❌ User nahi mila. Reply karke ya user ID deke try karo.\n" + hint)

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