"""Deterministic channel-content delivery — REAL forwards, zero LLM.

Why this module exists: members asked 'manager ye wala message forward karna'
with a channel-post link, and the LLM could only TALK ('nikal kar pahuncha
deta hu') or paste the link back — it never actually forwarded anything.
The owner called it out: 'link nahi, MESSAGE forward karo'.

This handler runs BEFORE the LLM ever sees the message. It parses the
request deterministically and performs a real forwardMessage from the
linked channel. The LLM can therefore never claim, fake, or fail this
action — it simply never gets the chance.

Supported request shapes (group or owner DM):
  1. '... forward karo : https://t.me/rkgamersbgmi/245'   (public link)
  2. '... bhejo https://t.me/c/3956726834/245'            (private link)
  3. '/get 245' or '/get latest' or '/get <link>'         (command form)
  4. 'vo wala message bhejo / de do'                      (last link in chat)
  5. 'latest / aakhri post dikhao'                        (newest known post)
  6. '<number> wala message bhejo'                        (post by number)
  7. 'channel se <naam> bhejo'                            (search ingested
     posts by name — works for posts the bot has seen)

Every outcome replies HONESTLY: success notice only after the forward
actually succeeded; a clear failure reason when it did not.
"""
from __future__ import annotations

import re

import links
import tg_api
from db import get_db, get_state
from moderation import _say_reply

# The last channel-post link seen per chat — powers 'vo wala message bhejo'
# follow-ups (the owner does exactly this in DM).
_LAST_LINK: dict[int, tuple[int, int]] = {}

# Public channel-post link: t.me/<username>/<message_id>
_LINK_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{1,63})/(\d{1,10})",
    re.IGNORECASE,
)
# Private channel-post link: t.me/c/<internal_id>/<message_id>
_LINK_PRIVATE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d{1,15})/(\d{1,10})",
    re.IGNORECASE,
)

# Forward/deliver intent words (Hinglish + English).
_INTENT_RE = re.compile(
    r"\b(forward|forword|forwrd|bhej|bhejo|bhejna|bhejdo|bhej\s?do|bhej\s?dena|"
    r"dedo|de\s?do|dena|dikhao|dikha|dikhana|dikha\s?do|send|nikal|nikaal|"
    r"nikal\s?do|chahiye|mujhe|mail|mila\s?do|lana|la\s?do|do\s?na)\b",
    re.IGNORECASE,
)
_LATEST_RE = re.compile(
    r"\b(latest|last|aakhri|akhri|aakhir|naya|nayi|new|sabse\s+nay[ai])\b"
    r".{0,30}\b(post|message|msg|file|cheez|wala|waala|upload)\b"
    r"|\b(post|message|msg|file|cheez)\b.{0,20}\b(latest|last|aakhri|akhri|naya|nayi|new)\b",
    re.IGNORECASE,
)
_NUM_POST_RE = re.compile(
    r"#?(\d{1,6})\s*(?:wala|waala|wala hi|number\s?wala|no\.?\s?wala)?",
    re.IGNORECASE,
)


def _managed_channel_username() -> str:
    """@username of the linked channel ('' when unknown/private)."""
    cached = get_state("managed_channel_username")
    if cached:
        return str(cached).strip().lower()
    cid = links.get_channel_id()
    if not cid:
        return ""
    try:
        info = tg_api.get_chat(cid) or {}
        uname = (info.get("username") or "").strip()
        if uname:
            from db import save_state
            save_state("managed_channel_username", uname)
            return uname.lower()
    except Exception:
        pass
    return ""


def _remember_link(chat_id: int, channel_id: int, message_id: int) -> None:
    if chat_id and channel_id and message_id:
        _LAST_LINK[chat_id] = (channel_id, int(message_id))


def _forward_post(chat_id: int, from_chat_id: int, message_id: int,
                  reply_to: int | None, label: str = "") -> bool:
    """Actually forward a channel post; always answer honestly.

    Returns True when the request is fully handled (caller must stop).
    """
    r = tg_api.forward_message(chat_id, from_chat_id, message_id)
    if r.get("ok"):
        note = "✅ Forward kar diya 📎"
        if label:
            note += f" ({label})"
        _say_reply(chat_id, note, reply_to)
        return True
    desc = str(r.get("description") or "")
    # Some channels/posts can't be forwarded (restrictions) — try a copy,
    # which preserves the content even without the 'Forwarded from' header.
    if "restrictions" in desc.lower() or "not found" not in desc.lower():
        c = tg_api.copy_message(chat_id, from_chat_id, message_id,
                                reply_to_message_id=reply_to)
        if c.get("ok"):
            return True
    if "not found" in desc.lower() or "message id" in desc.lower():
        _say_reply(chat_id, "❌ Ye post channel me nahi mila — delete ho chuka "
                            "hoga ya number galat hai.", reply_to)
    else:
        _say_reply(chat_id, f"❌ Forward nahi ho paya: {desc or 'unknown error'} "
                            "— bot channel ka admin hona chahiye.", reply_to)
    return True


def _resolve_link_source(username: str) -> int | None:
    """Map a link's @username to our managed channel id (None if foreign)."""
    cid = links.get_channel_id()
    if not cid:
        return None
    managed = _managed_channel_username()
    if managed and username.strip().lower().lstrip("@") == managed:
        return cid
    # No public username (or cache cold): compare via getChat.
    try:
        info = tg_api.get_chat(cid) or {}
        if (info.get("username") or "").strip().lower() == username.strip().lower():
            return cid
    except Exception:
        pass
    return None


def _latest_known_post(channel_id: int) -> int | None:
    """Newest ingested post id for the channel (None when nothing seen)."""
    try:
        docs = list(get_db()["channel_posts"].find(
            {"channel_id": channel_id}).sort("message_id", -1).limit(1))
        if docs and docs[0].get("message_id"):
            return int(docs[0]["message_id"])
    except Exception:
        pass
    return None


def _search_post_by_name(query: str, channel_id: int) -> int | None:
    """Find an ingested channel post whose text/caption matches the query."""
    tokens = [w for w in re.findall(r"[A-Za-z0-9_.-]{3,}", query)
              if w.lower() not in {
                  "channel", "se", "bhejo", "bhej", "dedo", "mujhe",
                  "forward", "karo", "karna", "dikhao", "message", "post",
                  "msg", "wala", "manager", "bot", "bhai"}]
    if not tokens or not channel_id:
        return None
    try:
        rgx = {"$regex": re.escape(tokens[0]), "$options": "i"}
        docs = list(get_db()["channel_posts"].find(
            {"$or": [{"text": rgx}, {"caption": rgx}],
             "channel_id": channel_id}).sort("message_id", -1).limit(3))
        if len(docs) == 1 and docs[0].get("message_id"):
            return int(docs[0]["message_id"])
        for d in docs:
            text = ((d.get("text") or "") + " " + (d.get("caption") or "")).lower()
            if all(t.lower() in text for t in tokens):
                return int(d.get("message_id"))
    except Exception:
        pass
    return None


def _strip_intent_words(text: str) -> str:
    """Remove intent/filler words so name-search only sees real keywords."""
    words = re.findall(r"[A-Za-z0-9_.@#-]+", text)
    drop = {
        "forward", "forword", "bhejo", "bhej", "bhejna", "dedo", "dikhao",
        "dikha", "send", "nikal", "chahiye", "mujhe", "karo", "karna", "krna",
        "krdo", "kardo", "manager", "bot", "bhai", "boss", "bro", "channel",
        "se", "ka", "ki", "ke", "wala", "waala", "message", "post", "msg",
        "file", "ye", "yeh", "vo", "woh", "hai", "ha", "na", "to", "aur",
        "and", "the", "a", "do", "de", "dena", "latest", "aakhri", "plz",
        "please", "number", "no",
    }
    return " ".join(w for w in words if w.lower() not in drop)


def handle_forward_request(msg: dict, chat_id: int, uid: int) -> bool:
    """Entry point (group + owner DM). True = fully handled, stop pipeline.

    Fires only when the message genuinely asks for channel content —
    a link, a post number, 'latest', or the last link seen in this chat.
    Chatter that merely mentions the channel never triggers a forward.
    """
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return False
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text or text.startswith("/"):
        return False
    # Replies addressed to another member are not for the bot; only an
    # explicit 'forward...' opener counts there.
    mid = msg.get("message_id")
    cid = links.get_channel_id()
    low = text.lower()

    # --- 1. explicit channel-post link in the message ---
    m = _LINK_PRIVATE_RE.search(text)
    if m:
        internal, post_id = int(m.group(1)), int(m.group(2))
        # Normalize: t.me/c/<X>/<N> -> chat id -100<X>
        source = int(f"-100{internal}")
        if cid and source == cid:
            _remember_link(chat_id, source, post_id)
            # Group me intent chahiye (koi bhi link paste karke auto-forward
            # nahi karwa sakta); owner DM me bare link hi delivery hai.
            if _INTENT_RE.search(low) or chat_id > 0:
                return _forward_post(chat_id, source, post_id, mid,
                                     f"post #{post_id}")
            return False
        # Foreign private channel — honest refusal, LLM never sees it.
        if _INTENT_RE.search(low):
            _say_reply(chat_id, "❌ Main sirf apne linked channel se forward "
                                "kar sakta hu — ye kisi aur channel ka link "
                                "hai.", mid)
            return True
        return False
    for m in _LINK_PUBLIC_RE.finditer(text):
        username, post_id = m.group(1), int(m.group(2))
        source = _resolve_link_source(username)
        if source:
            _remember_link(chat_id, source, post_id)
            # Group: forward only on intent ('forward karna', 'bhejo',
            # 'mujhe', ...). Owner DM: bare deep-link = delivery request.
            if _INTENT_RE.search(low) or chat_id > 0:
                return _forward_post(chat_id, source, post_id, mid,
                                     f"post #{post_id}")
            return False
        # A link with a post id that is NOT our channel, with forward
        # intent — own the answer honestly so the LLM can't fake it.
        if _INTENT_RE.search(low) and cid:
            _say_reply(chat_id, "❌ Ye hamare linked channel ka post nahi "
                                "hai — main sirf apne channel se forward "
                                "karta hu.", mid)
            return True
        return False

    # --- command-style '/get' is handled elsewhere; natural requests only ---
    wants = bool(_INTENT_RE.search(low))
    if not wants and not _LATEST_RE.search(low):
        return False

    # --- 2. 'vo wala message bhejo' — last link seen in this chat ---
    if _LAST_LINK.get(chat_id) and re.search(
            r"\b(vo|woh|wo|vahi|wahi|us|usi|pichla|pichli|last|aakhri|akhri)\b",
            low):
        ch, post_id = _LAST_LINK[chat_id]
        if cid and ch == cid:
            return _forward_post(chat_id, ch, post_id, mid, f"post #{post_id}")

    # --- 3. latest/aakhri post ---
    if cid and _LATEST_RE.search(low):
        post_id = _latest_known_post(cid)
        if post_id:
            return _forward_post(chat_id, cid, post_id, mid, "latest post")
        _say_reply(chat_id, "Abhi tak channel ka koi naya post memory me "
                            "nahi aaya — link de do (t.me/channel/123 jaisa) "
                            "to turant forward kar dunga.", mid)
        return True

    # --- 4. '<number> wala' post ---
    if cid and wants:
        num = _NUM_POST_RE.search(low)
        if num and re.search(r"wala|waala|post|message|msg|number|no\.|id",
                             low):
            # Avoid forwarding when the number is actually a duration etc.
            if not re.search(r"\b(min|minute|ghant|hour|din|day|week|hafte)\b",
                             low):
                return _forward_post(chat_id, cid, int(num.group(1)), mid,
                                    f"post #{num.group(1)}")

    # --- 5. by name ('channel se beastcrown bhejo') ---
    if cid and wants and ("channel" in low or "post" in low):
        post_id = _search_post_by_name(_strip_intent_words(text), cid)
        if post_id:
            return _forward_post(chat_id, cid, post_id, mid)

    # Nothing deterministic matched — let the normal flow answer (the LLM's
    # honesty rules forbid it from claiming a forward it cannot make).
    return False


def handle_get_command(msg: dict, chat_id: int, uid: int) -> bool:
    """/get <link|number|latest> — explicit channel content delivery."""
    text = (msg.get("text") or "").strip()
    if not text.startswith("/get"):
        return False
    parts = text.split()
    if not parts or parts[0].split("@", 1)[0].lower() != "/get":
        return False
    arg = parts[1] if len(parts) > 1 else ""
    mid = msg.get("message_id")
    cid = links.get_channel_id()
    if not cid:
        _say_reply(chat_id, "❌ Abhi koi channel link nahi hai.", mid)
        return True
    if not arg:
        _say_reply(chat_id, "Usage: /get <post number | t.me link | latest>\n"
                            "Example: /get 245  ya  /get latest", mid)
        return True
    if arg.lower() in ("latest", "last", "new", "naya"):
        post_id = _latest_known_post(cid)
        if post_id:
            return _forward_post(chat_id, cid, post_id, mid, "latest post")
        _say_reply(chat_id, "Channel ka koi post abhi memory me nahi — naye "
                            "post aate hi dikha dunga. Tab tak /get <number> "
                            "ya link use karo.", mid)
        return True
    m = _LINK_PUBLIC_RE.search(arg) or _LINK_PRIVATE_RE.search(arg)
    if m:
        if _LINK_PRIVATE_RE.fullmatch(arg) or _LINK_PUBLIC_RE.fullmatch(arg):
            # /get <link> — explicit delivery command: intent guaranteed,
            # handle_forward_request ko intent word ke sath bhejo.
            return handle_forward_request(
                {**msg, "text": "forward karo " + arg}, chat_id, uid)
    if re.fullmatch(r"\d{1,6}", arg):
        return _forward_post(chat_id, cid, int(arg), mid, f"post #{arg}")
    _say_reply(chat_id, "Samajh nahi aaya — post ka number do, t.me link do, "
                        "ya '/get latest' likho.", mid)
    return True


def note_link_in_text(chat_id: int, text: str) -> None:
    """Remember any channel-post link seen in a chat (even unhandled) so
    later 'vo wala message bhejo' requests can resolve it."""
    try:
        cid = links.get_channel_id()
        if not cid:
            return
        m = _LINK_PUBLIC_RE.search(text or "")
        if m and _resolve_link_source(m.group(1)) == cid:
            _remember_link(chat_id, cid, int(m.group(2)))
            return
        m = _LINK_PRIVATE_RE.search(text or "")
        if m and int(f"-100{int(m.group(1))}") == cid:
            _remember_link(chat_id, cid, int(m.group(2)))
    except Exception:
        pass
