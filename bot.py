"""RK Manager Agent — main entry.

Dispatch:
  - my_chat_member   -> auto-detect group/channel + announce
  - channel_post     -> knowledge.ingest_channel_post
  - callback_query   -> owner_panel.handle_callback, escalation.handle_callback
  - message:
      private (owner ONLY): /start -> owner_panel.handle_start
                            /forget -> owner_panel.handle_forget_command
                            /mod_* and /mute -> owner_panel.handle_owner_command
                            forget-mode text -> owner_panel.handle_forget_mode_text
                            DM reply to escalation -> escalation.handle_admin_message
                            other text -> treated as a question, answered (no escalation)
      private admin: escalation replies only.
      private (anyone else): silently ignored.
      group/supergroup:       ALWAYS ingest
                              natural questions/requests, /ask, or @mention -> answer

Run mode: webhook if WEBHOOK_URL set, else long-polling.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import tg_api  # noqa: E402
import db  # noqa: E402
import ai_engine  # noqa: E402
import knowledge  # noqa: E402
import agent  # noqa: E402
import escalation  # noqa: E402
import owner_panel  # noqa: E402
import links  # noqa: E402
import moderation  # noqa: E402
import triggers  # noqa: E402


def log(*args):
    sys.stderr.write("[rk-agent] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


_BOT_USERNAME: str | None = None
_BOT_ID: int | None = None


def _bot_username() -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        me = tg_api.get_me()
        _BOT_USERNAME = (me.get("username") or "").strip()
        global _BOT_ID
        _BOT_ID = me.get("id")
    return _BOT_USERNAME


def _bot_id() -> int:
    global _BOT_ID
    if _BOT_ID is None:
        _bot_username()
    return _BOT_ID or 0


def _mentions_bot(text: str, entities: list) -> bool:
    uname = _bot_username()
    if not uname:
        return False
    if ("@" + uname).lower() in (text or "").lower():
        return True
    for ent in entities or []:
        if ent.get("type") == "mention":
            s, ln = ent.get("offset", 0), ent.get("length", 0)
            chunk = (text or "")[s:s + ln].lstrip("@").lower()
            if chunk == uname.lower():
                return True
    return False


def _mentions_other_user(text: str, entities: list) -> bool:
    """Avoid replying to messages explicitly directed at another member."""
    bot_name = _bot_username().lower()
    for ent in entities or []:
        if ent.get("type") == "mention":
            start, length = ent.get("offset", 0), ent.get("length", 0)
            mentioned = (text or "")[start:start + length].lstrip("@").lower()
            if mentioned and mentioned != bot_name:
                return True
        elif ent.get("type") == "text_mention":
            target_id = (ent.get("user") or {}).get("id")
            if target_id and target_id != _bot_id():
                return True
    return False


def _is_ask_command(text: str) -> re.Match | None:
    return re.match(r"^/ask(?:@\w+)?(?:\s+|$)", (text or "").strip(), re.IGNORECASE)


def _group_scan_answer(chat_id: int) -> dict:
    """Report live Telegram facts; never guess whether the bot is an admin."""
    bot_id = _bot_id()
    member = tg_api.get_chat_member(chat_id, bot_id) if bot_id else {}
    count = tg_api.get_chat_member_count(chat_id)
    if not member:
        return {
            "text": "Telegram se group status verify nahi ho paya. Bot ko group mein admin/member access aur required permissions check karein.",
            "grounded": False,
            "needs_escalation": False,
            "sources": 0,
        }
    status = member.get("status") or "unknown"
    is_admin = status in ("administrator", "creator")
    rights = member.get("can_delete_messages", False), member.get("can_restrict_members", False)
    return {
        "text": (
            f"🔎 Group check (live Telegram status)\n"
            f"Members: {count if count else 'count unavailable'}\n"
            f"Bot status: {status}\n"
            f"Admin: {'yes' if is_admin else 'no'}\n"
            f"Delete messages: {'yes' if rights[0] else 'no'}\n"
            f"Restrict members: {'yes' if rights[1] else 'no'}"
        ),
        "grounded": True,
        "needs_escalation": False,
        "sources": 1,
    }


def _is_private_owner(uid: int) -> bool:
    return bool(uid) and uid == config.OWNER_CHAT_ID


def _is_private_admin(uid: int) -> bool:
    return _is_private_owner(uid) or (bool(uid) and uid == (config.ADMIN_CHAT_ID or 0))


# Recent conversation per chat (member + bot messages), so the reply gate
# can see follow-ups to the bot's own messages. In-memory only.
_GROUP_CONTEXT: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))


# Conversational moderation requests ("isko mute kar do", "ye msg delete kr").
# Members can NEVER trigger actions on their word — owner only. The owner’s
# spoken request is turned into a REAL command and run through the same
# pipeline as /commands (never faked by just saying so).
_MOD_REQUEST_RE = re.compile(r"""
    \b(hatao|udao|mitao)\b
    | \b(delete|del|remove|uda|hata|mita|mute|unmute|ban)\w*\b.{0,25}\b(kar|karo|kr|krdo|kardo|de|do|dijiye)\b
    | \b(kar|karo|kr|krdo|kardo|de|do)\b.{0,25}\b(delete|del|remove|uda|hata|mita|mute|unmute|ban)\w*\b
""", re.IGNORECASE | re.VERBOSE)


def _looks_like_mod_request(text: str) -> bool:
    if not text or "kaise" in text.lower():
        return False
    # Past-tense narration ("delete kar di thi") is not a request.
    if re.search(r"\b(diya\s+(tha|thi|hai)|di\s+(thi|hai)|chuk[aei]\b|liya\s+(tha|thi|hai))\b",
                 text, re.IGNORECASE):
        return False
    return bool(_MOD_REQUEST_RE.search(text))


def _link_chat_id(chat_id: int) -> str:
    """t.me/c/<id> form of a group chat id ('-100123' -> '123')."""
    s = str(abs(chat_id or 0))
    return s[3:] if s.startswith("100") else s


def _spoken_duration(text: str) -> str | None:
    """'10 min ke liye' -> '10m'; None if no parseable duration."""
    m = re.search(
        r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|ghant[aeo]|days?|dins?|weeks?|[smhdw])\b",
        text or "", re.IGNORECASE)
    if not m:
        return None
    unit = m.group(2).lower()
    if unit.startswith("sec") or unit == "s":
        sfx = "s"
    elif unit.startswith("min") or unit == "m":
        sfx = "m"
    elif unit.startswith("h") or unit.startswith("ghant"):
        sfx = "h"
    elif unit.startswith("d"):
        sfx = "d"
    elif unit.startswith("w"):
        sfx = "w"
    else:
        return None
    tok = f"{int(m.group(1))}{sfx}"
    return tok if moderation.parse_duration(tok) else None


def _synthesize_owner_command(text: str, msg: dict, chat_id: int) -> dict | None:
    """Turn the owner's spoken mod request into a real command message.

    Returns a synthetic msg for moderation.handle_mod_command, or None when
    there is no clear action+target (then the LLM handles it conversationally).
    """
    if not _looks_like_mod_request(text):
        return None
    low = text.lower()
    if "unmute" in low:
        cmd = "unmute"
    elif "mute" in low:
        cmd = "mute"
    elif "unban" in low:
        cmd = "unban"
    elif "ban" in low:
        cmd = "ban"
    else:
        cmd = "delete"

    reply = msg.get("reply_to_message") or {}
    synth = {
        "message_id": msg.get("message_id"),
        "date": msg.get("date"),
        "from": msg.get("from"),
        "chat": msg.get("chat"),
    }

    if cmd == "delete":
        link = re.search(r"t\.me/c/(\d+)/(\d+)", text or "")
        if link and link.group(1) == _link_chat_id(chat_id):
            synth["reply_to_message"] = {"message_id": int(link.group(2))}
            synth["text"] = "/delete"
            return synth
        if reply.get("message_id"):
            synth["reply_to_message"] = reply
            synth["text"] = "/delete"
            return synth
        return None

    # User-targeted commands need a reply or an ID/@username token.
    # Never treat the bot's own mention as the target.
    target_token = None
    if (reply.get("from") or {}).get("id"):
        synth["reply_to_message"] = reply
    else:
        uname = _bot_username()
        clean = re.sub(rf"@{re.escape(uname)}\b", "", text or "",
                       flags=re.IGNORECASE).strip() if uname else (text or "")
        m = re.search(r"(?:^|\s)(@\w+|\d{6,})", clean)
        if not m:
            return None
        target_token = m.group(1)

    if cmd == "mute":
        dur = _spoken_duration(text)
        synth["text"] = f"/tmute {dur}" if dur else "/mute"
    elif cmd == "unmute":
        synth["text"] = "/unmute"
    elif cmd == "ban":
        synth["text"] = "/ban"
    else:  # unban
        synth["text"] = "/unban"
    if target_token:
        synth["text"] += f" {target_token}"
    return synth


def _remember(chat_id: int, name: str, text: str,
              is_bot: bool = False, is_owner: bool = False) -> None:
    text = (text or "").strip()
    if chat_id and text:
        _GROUP_CONTEXT[chat_id].append(
            {"name": name, "text": text[:300], "is_bot": is_bot,
             "owner": is_owner})


# Live Telegram role / rights lookups (5-min cache — roles change rarely,
# and group messages must not each cost 2 API calls).
_ROLE_CACHE: dict[tuple[int, int], tuple[str, float]] = {}
_BOTRIGHT_CACHE: dict[int, tuple[dict, float]] = {}
_ROLE_TTL = 300


def _group_role(chat_id: int, user_id: int) -> str:
    """Live role of a user in the group: owner/administrator/member/unknown."""
    if user_id == config.OWNER_CHAT_ID:
        return "owner"
    key = (chat_id, user_id)
    now = time.time()
    hit = _ROLE_CACHE.get(key)
    if hit and now - hit[1] < _ROLE_TTL:
        return hit[0]
    role = "unknown"
    try:
        m = tg_api.get_chat_member(chat_id, user_id) or {}
        status = m.get("status") or ""
        if status == "creator":
            role = "owner"
        elif status == "administrator":
            role = "administrator"
        elif status:
            role = "member"
    except Exception:
        pass
    _ROLE_CACHE[key] = (role, now)
    return role


def _bot_rights(chat_id: int) -> dict:
    """RK Manager's own admin rights in the group (cached)."""
    now = time.time()
    hit = _BOTRIGHT_CACHE.get(chat_id)
    if hit and now - hit[1] < _ROLE_TTL:
        return hit[0]
    info = {"status": "unknown", "can_delete": False, "can_restrict": False}
    try:
        bot_id = _bot_id()
        m = tg_api.get_chat_member(chat_id, bot_id) if bot_id else {}
        status = (m or {}).get("status") or ""
        info["status"] = status or "unknown"
        if status == "creator":
            info["can_delete"] = True
            info["can_restrict"] = True
        elif status == "administrator":
            info["can_delete"] = bool(m.get("can_delete_messages"))
            info["can_restrict"] = bool(m.get("can_restrict_members"))
    except Exception:
        pass
    _BOTRIGHT_CACHE[chat_id] = (info, now)
    return info


_GATE_SYSTEM = (
    "You are the reply-decision module for RK Manager, an AI assistant in a "
    "Telegram gaming group. Answer with exactly one word: YES or NO."
)


def _should_reply_llm(text: str, name: str, chat_id: int,
                      is_owner: bool = False) -> bool:
    """Decide with the LLM whether RK Manager should reply to a group message.

    The gate sees recent conversation context (including the bot's own
    messages), so follow-ups like "tum kya mujhe doge" after the bot asked
    "batao kya help chahiye" are recognised as addressed to the bot.
    Fail-closed: on any error, stay silent.
    """
    recent = list(_GROUP_CONTEXT.get(chat_id, []))
    # The current message was just remembered — exclude it from context.
    recent = recent[:-1][-8:]
    lines = []
    for m in recent:
        if m["is_bot"]:
            who = "RK Manager (you)"
        else:
            who = m["name"] + (" [OWNER]" if m.get("owner") else "")
        lines.append(who + ": " + m["text"])
    convo = "\n".join(lines) if lines else "(no earlier messages)"
    role = " (group OWNER)" if is_owner else ""
    prompt = (
        "Recent group conversation:\n" + convo + "\n\n"
        + "New message from " + name + role + ": " + text + "\n\n"
        + "Should RK Manager (the group's AI assistant -- that's YOU) reply to "
        + "this new message? Answer YES if:\n"
        + "- it is addressed to you (RK Manager / manager / bot, any spelling), or\n"
        + "- it is a follow-up to your previous message, or\n"
        + "- it asks a question or requests something you could help with "
        + "(mods, loaders, APKs, BGMI, downloads, group info), or\n"
        + "- it clearly expects your response.\n"
        + "Answer NO if:\n"
        + "- it is banter, jokes, or chatter between members, or\n"
        + "- it is directed at another member, or\n"
        + "- it is just a greeting, thanks, ok, hmm with nothing else.\n"
        + "Answer with exactly one word: YES or NO."
    )
    try:
        out = ai_engine.get_router().generate(
            prompt, system=_GATE_SYSTEM, max_tokens=8
        )
    except Exception:
        return False
    return out.strip().upper().startswith("YES")



# ---------------- handlers ----------------

def handle_my_chat_member(update) -> None:
    """Strict: only owner can activate the bot in a chat.

    If anyone else adds the bot to a group/channel, the bot says
    'join @rkgamersbgmi' and leaves immediately. If the owner adds it,
    the chat is auto-set as the managed one and owner is notified in DM.
    """
    cm = update.get("my_chat_member") or {}
    chat = cm.get("chat") or {}
    new = cm.get("new_chat_member") or {}
    old = cm.get("old_chat_member") or {}
    by = cm.get("from") or {}

    status = new.get("status")
    old_status = old.get("status")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    title = chat.get("title") or chat.get("username") or "—"
    adder_id = by.get("id")

    # Bot was kicked / left — clear if it was our managed one
    if status in ("kicked", "left"):
        if links.is_managed_group(chat_id):
            links.clear_group()
            log(f"left managed group {chat_id}")
            try:
                tg_api.send_message(
                    config.OWNER_CHAT_ID,
                    f"⚠️ Main group `{title}` se nikal diya gaya. "
                    "Wapas add karoge to auto-set ho jayega.",
                )
            except Exception:
                pass
        elif links.is_managed_channel(chat_id):
            links.clear_channel()
            log(f"left managed channel {chat_id}")
        return

    if status not in ("administrator", "member", "creator"):
        return

    # Only act on first-time addition (not on promotions or status changes)
    if old_status not in ("left", "kicked", None):
        return

    # Owner-less deployment: refuse everything without posting to channels.
    if not config.OWNER_CHAT_ID:
        if chat_type in ("group", "supergroup"):
            try:
                tg_api.send_message(chat_id, "Bot is not configured yet. Owner se contact karo.")
            except Exception:
                pass
        try:
            tg_api.leave_chat(chat_id)
        except Exception:
            pass
        return

    # NON-OWNER added: refuse + leave
    if adder_id != config.OWNER_CHAT_ID:
        join_msg = (
            "🙏 Yeh bot RK Manager Agent hai.\n\n"
            "Ye sirf apne owner ke group/channel me kaam karta hai.\n"
            "Aap isko yahan add karke use nahi kar sakte.\n\n"
            "👉 Hamara official group join karo:\n"
            "@rkgamersbgmi\n\n"
            "Wahan se owner se baat karke apna setup karwa sakte ho."
        )
        if chat_type in ("group", "supergroup"):
            try:
                tg_api.send_message(chat_id, join_msg)
            except Exception:
                pass
        try:
            tg_api.leave_chat(chat_id)
        except Exception:
            pass
        # log + notify owner
        log(f"refused non-owner add to {chat_type} {chat_id} by {adder_id}")
        try:
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                f"⚠️ Kisi ne ({adder_id}) bot ko apne {chat_type} me add kiya: "
                f"{title}. Maine bina channel par post kiye reject karke leave kar diya.",
            )
        except Exception:
            pass
        return

    # OWNER added: auto-set as managed chat
    if chat_type in ("group", "supergroup"):
        prev = links.get_group_id()
        links.set_group(chat_id, title)
        if prev and prev != chat_id:
            try:
                tg_api.leave_chat(prev)
            except Exception:
                pass
        try:
            tg_api.send_message(
                chat_id,
                "👋 Namaste! Main RK Manager hoon.\n\n"
                "✅ Is group ko apna primary group bana liya.\n"
                "Ab messages se seekhunga; sirf sawal ya request aane par jawab dunga.",
            )
        except Exception:
            pass
        try:
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                f"✅ Group auto-set!\n\nTitle: {title}\nID: `{chat_id}`\n\n"
                f"Ab main is group me kaam karunga. Baki kisi aur group me add karne par reject kar dunga.",
            )
        except Exception:
            pass
    elif chat_type == "channel":
        prev = links.get_channel_id()
        links.set_channel(chat_id, title)
        if prev and prev != chat_id:
            try:
                tg_api.leave_chat(prev)
            except Exception:
                pass
        is_admin = status in ("administrator", "creator")
        try:
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                f"✅ Channel auto-set!\n\nTitle: {title}\nID: `{chat_id}`\n"
                f"Read access: {'yes' if is_admin else 'not yet'}\n\n"
                "Channel mein main koi message/post nahi bhejunga. Naye posts padhne ke liye "
                "bot ko admin rakhna hoga; Post Messages permission OFF rakh sakte ho.",
            )
        except Exception:
            pass


def handle_channel_post(post) -> None:
    """Only ingest posts from the managed channel; ignore all others."""
    chat_id = (post.get("chat") or {}).get("id")
    if not links.is_managed_channel(chat_id):
        return
    knowledge.ingest_channel_post(post)


def handle_callback_query(cb) -> None:
    handled = False
    try:
        if owner_panel.handle_callback(cb):
            handled = True
        elif escalation.handle_callback(cb):
            handled = True
    except Exception:
        log("callback handler failed:", traceback.format_exc())
    try:
        tg_api.answer_callback_query(cb.get("id"))
    except Exception:
        pass


def handle_message(msg) -> None:
    chat = msg.get("chat") or {}
    chat_type = chat.get("type", "")
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    uid = user.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # ---------------- private chat ----------------
    # Private DM is OWNER-ONLY. Any other user (including separate admin)
    # sending a DM gets no reply at all — silently ignored.
    if chat_type == "private":
        is_owner = _is_private_owner(uid)
        is_admin = bool(config.ADMIN_CHAT_ID and uid == config.ADMIN_CHAT_ID)
        if not is_owner and not is_admin:
            # non-owner DM: do nothing
            return
        if is_owner:
            if owner_panel.handle_owner_command(msg):
                return
            # /start
            if text == "/start" or text.startswith("/start "):
                owner_panel.handle_start(msg)
                return
        # /cancel: exit ANY pending input mode (forward-set, forget,
        # escalation reply) so the owner is never trapped in a mode.
        if is_owner and text in ("/cancel", "/back"):
            cleared = []
            for _k in ("group_fwd_mode", "channel_fwd_mode", "forget_mode"):
                if db.get_state(f"{uid}:{_k}"):
                    db.save_state(f"{uid}:{_k}", False)
                    cleared.append(_k.replace("_mode", "").replace("_", " "))
            if db.get_state(f"awaiting_reply:{uid}"):
                db.save_state(f"awaiting_reply:{uid}", None)
                cleared.append("reply mode")
            tg_api.send_message(
                chat_id,
                "Cancelled ✅ — ab normal baat kar sakte ho."
                if cleared else "Koi pending mode nahi tha 🙂",
            )
            return
        # /forget
        if is_owner and text.startswith("/forget"):
            if owner_panel.handle_forget_command(msg):
                return
        # forget-mode text (after panel button)
        if is_owner and owner_panel.handle_forget_mode_text(msg):
            return
        # group-fwd mode (after "Set by Forward" panel button)
        if is_owner and owner_panel.handle_group_fwd(msg):
            return
        # channel-fwd mode
        if is_owner and owner_panel.handle_channel_fwd(msg):
            return
        # admin/owner reply to escalation
        if not is_owner and text.startswith("/"):
            return
        if escalation.handle_admin_message(msg):
            return
        if not is_owner:
            return
        # otherwise: treat as a normal question (owner context)
        if not text:
            return
        tg_api.send_chat_action(chat_id, "typing")
        ctx = {"user_id": uid, "chat_id": chat_id, "first_name": user.get("first_name"),
               "username": user.get("username"), "owner": True}
        try:
            res = agent.answer(text, ctx)
        except Exception:
            log("agent.answer failed:", traceback.format_exc())
            res = {"text": None, "grounded": False, "error": "internal"}
        _deliver_answer(chat_id, res)
        return

    # ---------------- group chat ----------------
    if chat_type in ("group", "supergroup"):
        # Only respond in the OWNER's managed group. Other groups = ignored.
        if not links.is_managed_group(chat_id):
            return
        # Moderation runs before ingestion so removed abusive messages are not
        # added to long-term memory.
        if moderation.handle_group_message(msg):
            return
        # Learn from every non-command non-bot message
        knowledge.ingest_message(msg)
        # Remember for the reply gate's conversation context.
        _who = user.get("first_name") or user.get("username") or "member"
        _remember(chat_id, _who, text, is_owner=(uid == config.OWNER_CHAT_ID))

        # Manual escalation: ONLY when a user explicitly asks for a human.
        # The bot never escalates on its own anymore.
        if text.startswith("/admin"):
            note = text[len("/admin"):].strip()
            eid = escalation.open_escalation(
                user_id=uid,
                user_chat_id=chat_id,
                question=(note or "(admin se baat karni hai)")[:400],
                source="group",
            )
            tg_api.send_message(
                chat_id,
                "✅ Admin ko bhej diya hai — jawab milte hi yahin bata dunga. "
                f"(ref: {eid[:8]})",
            )
            return

        # Rose-style mod tools (owner/admin): /filter /mute /tmute /unmute
        # /ban /unban /delete. Handled before the reply gate so commands
        # are never swallowed as chatter.
        if moderation.handle_mod_command(msg, uid, chat_id):
            return

        # The bot reads EVERY message, but replies only when spoken to or
        # needed — using its own judgment (LLM gate), not just /ask.
        # Explicitly directed at the bot: always answer.
        ask_match = _is_ask_command(text)
        is_ask = ask_match is not None
        is_mention = _mentions_bot(text, msg.get("entities") or msg.get("caption_entities") or [])
        reply_to = msg.get("reply_to_message") or {}
        reply_user = (reply_to.get("from") or {}).get("id")
        replied_to_bot = bool(reply_user and reply_user == _bot_id())
        entities = msg.get("entities") or msg.get("caption_entities") or []
        directed = is_ask or is_mention or replied_to_bot

        if not directed:
            # Commands (other than /ask, /admin handled above): ignore.
            if text.startswith("/"):
                return
            # A reply addressed to another member, or mentioning someone
            # else: not for the bot — stay silent.
            if reply_to and not replied_to_bot:
                return
            if _mentions_other_user(text, entities):
                return
            # Obvious question/request: answer directly (cheap path).
            if not triggers.is_question_or_request(text):
                # Plain chatter ("hyy", "ok"): never needs a reply.
                if triggers.is_plain_chatter(text):
                    return
                # Everything else: the bot decides itself with the LLM gate
                # (it sees recent conversation context).
                who = user.get("first_name") or user.get("username") or "member"
                if not _should_reply_llm(text, who, chat_id,
                                         is_owner=(uid == config.OWNER_CHAT_ID)):
                    return

        # Conversational moderation: members can NEVER trigger actions on
        # their word — only the owner can. The owner's spoken request becomes
        # a REAL command through the same pipeline (never faked).
        if _looks_like_mod_request(text):
            if uid != config.OWNER_CHAT_ID:
                _deliver_answer(chat_id, {
                    "text": "Bhai, delete/mute/ban ye sab sirf owner ke kehne pe karta hu — "
                            "kisi member ke bolne pe kisi pe action nahi lunga. 🙂",
                })
                return
            synth = _synthesize_owner_command(text, msg, chat_id)
            if synth and moderation.handle_mod_command(synth, uid, chat_id):
                return
            # No clear action+target: fall through to the LLM, which is
            # instructed never to claim it performed an action.

        if is_ask:
            question = text[ask_match.end():].strip()
        else:
            question = text
        # Strip bot username from the question.
        uname = _bot_username()
        if uname:
            question = re.sub(rf"@{re.escape(uname)}\b", "", question, flags=re.IGNORECASE).strip()
        if not question:
            return
        if triggers.is_group_scan_request(question):
            _deliver_answer(chat_id, _group_scan_answer(chat_id))
            return
        tg_api.send_chat_action(chat_id, "typing")
        ctx = {"user_id": uid, "chat_id": chat_id, "first_name": user.get("first_name"),
               "username": user.get("username"), "group": True, "group_id": chat_id,
               "group_role": _group_role(chat_id, uid),
               "bot_admin": _bot_rights(chat_id)}
        try:
            res = agent.answer(question, ctx)
        except Exception:
            log("agent.answer failed:", traceback.format_exc())
            res = {"text": None, "grounded": False, "error": "internal"}
        _deliver_answer(chat_id, res)
        return


def _deliver_answer(chat_id: int, res: dict) -> None:
    """Send the answer / file to the user.

    The bot ALWAYS replies itself and never escalates automatically.
    Human escalation is manual only, via the /admin command.
    The answer may contain several messages separated by a line with
    only <SPLIT> — each is sent (and remembered) as its own message,
    like a human manager would.
    """
    def _send_and_remember(part: str) -> None:
        tg_api.send_message(chat_id, part)
        # Remember the bot's own message for the reply gate's context.
        if chat_id and chat_id < 0:
            _remember(chat_id, "RK Manager", part, is_bot=True)

    # Interim acknowledgement from a deep-lookup round ("Ruko bhai...").
    interim = (res.get("interim") or "").strip()
    if interim:
        _send_and_remember(interim)
        time.sleep(0.6)

    text = res.get("text") or ""
    parts = [p.strip() for p in text.split("<SPLIT>") if p.strip()]
    if parts:
        for i, part in enumerate(parts):
            _send_and_remember(part)
            if i < len(parts) - 1:
                time.sleep(0.6)
    elif not interim:
        _send_and_remember("🤔 Samajh nahi aaya — thoda detail me pucho.")

    # If there's a file offer, send it
    fo = res.get("file_offer")
    if fo:
        try:
            tg_api.send_any_file(chat_id, fo["file_id"], fo.get("file_type", "document"),
                                 caption=fo.get("caption"))
        except Exception:
            pass


# ---------------- run modes ----------------

def run_polling() -> None:
    try:
        tg_api.delete_webhook()
    except Exception:
        pass
    log(f"polling mode owner={config.OWNER_CHAT_ID} admin={config.ADMIN_CHAT_ID} "
        f"keys={ai_engine.get_router().key_count()}")
    offset = db.get_state("update_offset", 0)
    backoff = 1
    while True:
        try:
            r = knowledge.maybe_prune()
            if r != "skipped":
                log("prune:", r)
        except Exception:
            log("prune failed:", traceback.format_exc())
        try:
            r = knowledge.maybe_reconcile()
            if r != "skipped":
                log("reconcile:", r)
        except Exception:
            log("reconcile failed:", traceback.format_exc())
        try:
            updates = tg_api.get_updates(offset=offset, timeout=config.POLL_TIMEOUT)
            backoff = 1
        except Exception:
            log(f"getUpdates error, backoff {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        for u in updates:
            uid = u.get("update_id")
            try:
                _dispatch_update(u)
            except Exception:
                log(f"update {uid} failed:", traceback.format_exc())
            if uid is not None:
                offset = uid + 1
                try:
                    db.save_state("update_offset", offset)
                except Exception:
                    pass
        time.sleep(0.5)


def _dispatch_update(u) -> None:
    if "my_chat_member" in u:
        handle_my_chat_member(u)
    elif "channel_post" in u:
        handle_channel_post(u["channel_post"])
    elif "edited_channel_post" in u:
        handle_channel_post(u["edited_channel_post"])
    elif "callback_query" in u:
        handle_callback_query(u["callback_query"])
    elif "message" in u:
        handle_message(u["message"])


def run_webhook() -> None:
    secret = config.WEBHOOK_SECRET
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret or ""):
        raise RuntimeError(
            "WEBHOOK_SECRET must be set to a random 1-256 character Telegram secret token."
        )
    hook_path = "/" + secret
    full_url = config.WEBHOOK_URL + hook_path
    try:
        r = tg_api.set_webhook(full_url, secret_token=secret)
        log("setWebhook ->", r.get("ok"), r.get("description", ""))
    except Exception:
        log("setWebhook failed:", traceback.format_exc())

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != hook_path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
                self.send_response(403)
                self.end_headers()
                return
            try:
                ln = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                ln = 0
            body = self.rfile.read(ln) if ln else b""
            try:
                u = json.loads(body.decode("utf-8"))
                _dispatch_update(u)
            except Exception:
                log("webhook update failed:", traceback.format_exc())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    log(f"webhook mode on port {config.PORT} (secret path configured)")
    HTTPServer(("0.0.0.0", config.PORT), H).serve_forever()


def main() -> None:
    log("starting RK Manager Agent…")
    log(f"owner={config.OWNER_CHAT_ID} admin={config.ADMIN_CHAT_ID or config.OWNER_CHAT_ID}")
    log(f"gemini_keys={len(config.GEMINI_KEYS)} deepseek={'yes' if config.DEEPSEEK_KEY else 'no'}")
    log(
        "storage=MongoDB (persistent)" if db.is_persistent()
        else "storage=in-memory (volatile; restart loses stored data)"
    )
    if config.WEBHOOK_URL:
        run_webhook()
    else:
        run_polling()


if __name__ == "__main__":
    main()
