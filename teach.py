"""Owner-taught auto-actions: 'bot ko sikhao'.

The owner teaches the bot WHAT TO DO when a user says something:

    /sikhao <trigger> :: <reply text>          -> auto-reply with the text
    /sikhao <trigger> :: file <channel-file>   -> auto-send channel file
    /sikhao <trigger> :: forward               -> forward that channel post

Teachings fire on ANY member's matching group message (word-boundary,
case-insensitive). Owner-only commands manage them.
"""
from __future__ import annotations

import re
import time

import config
import knowledge
import tg_api
from db import get_state, save_state
from moderation import _say_reply

_KEY = "teach_rules"
_MAX_TEACH = 50
_MAX_TRIGGER = 60
_MAX_REPLY = 800

_TEACH_COMMANDS = {"sikhao", "sikhai", "bhulo"}


def get_teachings() -> list[dict]:
    value = get_state(_KEY, [])
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and str(item.get("trigger") or "").strip():
            out.append(item)
    return out


def _save(teachings: list[dict]) -> None:
    save_state(_KEY, teachings)


def parse_teach(text: str) -> tuple[str | None, str | None, str | None]:
    """Parse '<trigger> :: <action>' where action is a reply or 'file <name>'.

    Returns (trigger, reply, file_query). Only one of reply/file_query set.
    """
    body = text.strip()
    if "::" not in body:
        return None, None, None
    trigger, action = body.split("::", 1)
    trigger = trigger.strip().strip('"').lower()
    action = action.strip()
    if not trigger or not action or len(trigger) > _MAX_TRIGGER:
        return None, None, None
    low = action.lower()
    if low.startswith("file "):
        return trigger, None, action[5:].strip()
    if low == "forward" or low.startswith("forward "):
        return trigger, None, (action[7:].strip() or trigger)
    return trigger, action[:_MAX_REPLY], None


def add_teaching(trigger: str, reply: str | None, file_query: str | None,
                 by: int) -> str:
    teachings = get_teachings()
    teachings = [t for t in teachings if t.get("trigger") != trigger]
    if len(teachings) >= _MAX_TEACH:
        teachings = teachings[1:]
    entry = {"trigger": trigger, "by": by, "ts": time.time()}
    if reply:
        entry["reply"] = reply
    if file_query:
        entry["file"] = file_query
    teachings.append(entry)
    _save(teachings)
    return "ok"


def remove_teaching(trigger: str) -> str | None:
    trigger = (trigger or "").strip().strip('"').lower()
    teachings = get_teachings()
    for t in teachings:
        if t.get("trigger") == trigger:
            teachings.remove(t)
            _save(teachings)
            return trigger
    # partial match fallback
    for t in teachings:
        if trigger and trigger in str(t.get("trigger") or ""):
            teachings.remove(t)
            _save(teachings)
            return t.get("trigger")
    return None


def handle_teach_command(msg: dict, uid: int, chat_id: int) -> bool:
    """/sikhao /sikhai /bhulo — owner-only teaching commands in the group."""
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower().lstrip("/")
    if cmd not in _TEACH_COMMANDS:
        return False
    args = parts[1].strip() if len(parts) > 1 else ""
    mid = msg.get("message_id")
    is_owner = bool(uid) and uid == config.OWNER_CHAT_ID

    if not is_owner:
        _say_reply(chat_id, "❌ Bot ko sirf owner sikha sakta hai.", mid)
        return True

    if cmd == "sikhao":
        trigger, reply, file_query = parse_teach(args)
        if not trigger:
            _say_reply(chat_id, "Usage: /sikhao <trigger> :: <reply>\n"
                       "   ya: /sikhao <trigger> :: file <channel file ka naam>\n\n"
                       "Example:\n"
                       "/sikhao mod config :: file mod config\n"
                       "/sikhao hi bot :: Namaste bhai! Kya help chahiye?",
                       mid)
            return True
        add_teaching(trigger, reply, file_query, uid)
        if file_query:
            _say_reply(chat_id, f"✅ Sikh liya: jab koi '{trigger}' bolega, "
                                "main channel se matching file bhej dunga.", mid)
        else:
            preview = (reply or "")[:60]
            _say_reply(chat_id, f"✅ Sikh liya: jab koi '{trigger}' bolega, "
                                f"main bologa: {preview}", mid)
        return True

    if cmd == "sikhai":
        teachings = get_teachings()
        if not teachings:
            _say_reply(chat_id, "Abhi kuch nahi sikhaya gaya. /sikhao se "
                                "sikhao mujhe!", mid)
            return True
        lines = ["🧠 Sikhaaye gaye tasks:"]
        for i, t in enumerate(teachings, 1):
            if t.get("file"):
                lines.append(f"{i}. '{t['trigger']}' → file: {t['file']}")
            else:
                r = (t.get("reply") or "")[:40]
                lines.append(f"{i}. '{t['trigger']}' → {r}")
        _say_reply(chat_id, "\n".join(lines), mid)
        return True

    if cmd == "bhulo":
        if not args:
            _say_reply(chat_id, "Usage: /bhulo <trigger> — jo sikhaya tha "
                                "use bhul jaunga.", mid)
            return True
        removed = remove_teaching(args)
        if removed:
            _say_reply(chat_id, f"🗑 Bhool gaya: '{removed}'", mid)
        else:
            _say_reply(chat_id, "Ye trigger sikhaya hi nahi tha.", mid)
        return True

    return False


def match_and_act(msg: dict, chat_id: int) -> bool:
    """Fire a teaching on a normal group message. True = handled.

    Matching: word-boundary, case-insensitive. The owner's OWN messages
    also fire teachings (owner may test them), but commands never do.
    """
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text or text.startswith("/"):
        return False
    from_user = msg.get("from") or {}
    if from_user.get("is_bot"):
        return False
    low = text.casefold()
    teachings = get_teachings()
    if not teachings:
        return False
    mid = msg.get("message_id")
    for t in teachings:
        trigger = str(t.get("trigger") or "").casefold().strip()
        if not trigger:
            continue
        pattern = rf"(?<!\w){re.escape(trigger)}(?!\w)"
        if not re.search(pattern, low):
            continue
        # file delivery from channel
        if t.get("file"):
            hit = knowledge.find_file_for(str(t["file"]))
            if hit:
                cap = (hit.get("caption") or hit.get("text")
                       or "Channel se file")[:1000]
                r = tg_api.send_any_file(chat_id, hit["file_id"],
                                         hit.get("file_type") or "document",
                                         caption=cap)
                if r.get("ok"):
                    return True
            _say_reply(chat_id, "⚠️ File channel me mil nahi rahi — owner "
                                "ko bolo channel me daale.", mid)
            return True
        # plain auto-reply
        reply = str(t.get("reply") or "").strip()
        if reply:
            _say_reply(chat_id, reply, mid)
            return True
    return False
