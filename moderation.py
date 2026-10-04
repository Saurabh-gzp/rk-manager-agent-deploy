"""Owner-controlled, no-ban group moderation."""
from __future__ import annotations

import re
import time
import unicodedata

import config
from db import get_state, save_state
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
    if not term:
        return False

    now = int(time.time())
    key = f"moderation_strike:{chat_id}:{user_id}"
    previous = get_state(key, {}) or {}
    count = int(previous.get("count", 0) or 0)
    last_ts = int(previous.get("last_ts", 0) or 0)
    count = count + 1 if now - last_ts <= STRIKE_WINDOW_SECONDS else 1
    save_state(key, {"count": count, "last_ts": now})

    delete_result = tg_api.delete_message(chat_id, msg.get("message_id"))
    delete_failed = not delete_result.get("ok")
    name = (user.get("first_name") or user.get("username") or "Member").strip()
    if count == 1:
        notice = f"⚠️ {name}, gaali wala message remove kiya. Repeat par 10-minute mute hoga; ban nahi."
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


def manual_mute(chat_id: int, user_id: int, minutes: int) -> dict:
    """Owner-only command handler calls this after validating the target/time."""
    if minutes < 1 or minutes > MAX_MANUAL_MUTE_MINUTES:
        return {"ok": False, "description": "Mute duration must be 1–1440 minutes."}
    return _mute(chat_id, user_id, minutes)