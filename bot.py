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
import rules  # noqa: E402
import commands  # noqa: E402
import teach  # noqa: E402
import channel_forward  # noqa: E402
import community  # noqa: E402


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
    # Cheap name-detection: 'manager', 'rk manager', 'agent', 'bot' —
    # LLM gate call skip hota hai (ek pura roundtrip bachta hai).
    low = (text or "").lower()
    for name in ("rk manager", "rkmanager", "manager", "agent", "bot"):
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", low):
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
# maxlen=20: the ANSWER prompts also read this log (grounding for
# past-event questions — the model sees what actually happened).
_GROUP_CONTEXT: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))


# Conversational moderation requests ("isko mute kar do", "ye msg delete kr").
# Members can NEVER trigger actions on their word — owner only. The owner’s
# spoken request is turned into a REAL command and run through the same
# pipeline as /commands (never faked by just saying so).
_MOD_REQUEST_RE = re.compile(r"""
    \b(hatao|udao|mitao|nikal|bahar)\b
    | \b(delete|del|remove|uda|hata|mita|mute|unmute|ban|unban|kick)\w*\b.{0,25}\b(kar|karo|kr|krdo|kardo|de|do|dijiye)\b
    | \b(kar|karo|kr|krdo|kardo|de|do|dijiye)\b.{0,25}\b(delete|del|remove|uda|hata|mita|mute|unmute|ban|unban|kick)\w*\b
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
    there is no clear action+target (then the bot asks for a target — the
    LLM must NEVER claim the action).
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
    elif "kick" in low or "nikal" in low or "bahar" in low:
        cmd = "kick"
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

    # User-targeted commands need a reply, an ID/@username token, or a NAME
    # ('rich ko unmute kar do'). Never treat the bot's own mention as target.
    target_token = None
    _ACTION_WORDS = {
        "mute", "unmute", "ban", "unban", "delete", "del", "kick",
        "purge", "pin", "unpin", "warn", "block", "remove",
        "udao", "uda", "hatao", "hata", "mitao", "mita", "nikal",
        "bahar", "kickr", "band", "khol",
    }
    if (reply.get("from") or {}).get("id"):
        synth["reply_to_message"] = reply
    else:
        uname = _bot_username()
        clean = re.sub(rf"@{re.escape(uname)}\b", "", text or "",
                       flags=re.IGNORECASE).strip() if uname else (text or "")
        # 1) explicit @username or numeric ID
        m = re.search(r"(?:^|\s)(@\w+|\d{6,})", clean)
        if m:
            target_token = m.group(1)
        else:
            # 2) '<naam> ko' pattern — 'unmute rich ko'
            m = re.search(r"(?:^|\s)([a-zA-Z][\w.-]{1,30})\s+ko\b", clean,
                          re.IGNORECASE)
            if not m:
                # 3) strip filler + action words; jo ek alphabetic token
                # bachega wahi naam hai
                words = re.findall(r"[a-zA-Z][\w.-]{1,30}", clean)
                leftover = [w for w in words
                            if w.lower() not in moderation._STOPWORDS_TARGET
                            and w.lower() not in _ACTION_WORDS]
                if len(leftover) == 1:
                    m = re.match(r"(.+)", leftover[0])
            if m and m.group(1).lower() not in _ACTION_WORDS:
                target_token = m.group(1)
        if not target_token:
            return None

    if cmd == "mute":
        dur = _spoken_duration(text)
        synth["text"] = f"/tmute {dur}" if dur else "/mute"
    elif cmd == "unmute":
        synth["text"] = "/unmute"
    elif cmd == "ban":
        synth["text"] = "/ban"
    elif cmd == "kick":
        synth["text"] = "/kick"
    else:  # unban
        synth["text"] = "/unban"
    if target_token:
        synth["text"] += f" {target_token}"
    return synth


def _handle_owner_channel_post(msg: dict, chat_id: int, text: str) -> bool:
    """Owner DM: /post <text> — linked channel me post (owner ONLY).

    Bot khud se kabhi channel me post nahi karta — ye ek matra rasta hai,
    aur wo bhi owner ke explicit command par. Media/forward bhi chalega:
    /post photo <caption> ke sath photo bhejo DM me.
    """
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    if cmd != "/post":
        return False
    channel_id = links.get_channel_id()
    if not channel_id:
        tg_api.send_message(
            chat_id, "❌ Koi channel link nahi hai. Panel → 🔗 Link Channel "
                    "se channel set karo (ya channel me bot ko admin banao).")
        return True
    arg = parts[1].strip() if len(parts) > 1 else ""
    kinds = _media_kinds(msg)
    if not arg and not kinds:
        tg_api.send_message(
            chat_id, "Usage: /post <message>\n"
                    "Photo/video/file bhi bhej sakte ho: media attach karke "
                    "/post <caption> likho.\n\n⚠️ Ye channel me seedha post "
                    "ho jayega — bot sirf tumhare kehne par post karta hai.")
        return True
    caption = arg or None
    if kinds:
        # Owner ne media bheja hai — channel me as-is post karo.
        try:
            if msg.get("photo"):
                biggest = max(msg["photo"],
                              key=lambda p: p.get("file_size") or 0)
                r = tg_api.send_photo(channel_id, biggest["file_id"],
                                      caption=caption)
            elif (msg.get("video") or {}).get("file_id"):
                r = tg_api.send_video(channel_id, msg["video"]["file_id"],
                                      caption=caption)
            elif (msg.get("document") or {}).get("file_id"):
                r = tg_api.send_document(channel_id,
                                         msg["document"]["file_id"],
                                         caption=caption)
            elif (msg.get("audio") or {}).get("file_id"):
                r = tg_api.send_audio(channel_id, msg["audio"]["file_id"],
                                      caption=caption)
            else:
                r = {"ok": False, "description": "unsupported media"}
        except Exception:
            r = {"ok": False, "description": "send failed"}
    else:
        r = tg_api.send_message(channel_id, arg)
    if r.get("ok"):
        tg_api.send_message(chat_id, "✅ Channel pe post ho gaya.")
    else:
        tg_api.send_message(
            chat_id, f"❌ Channel post fail: "
                    f"{r.get('description') or 'bot ko channel me Post Messages permission chahiye'}")
    return True


def _handle_rule_command(msg: dict, uid: int, chat_id: int, text: str) -> bool:
    """Owner's standing instructions: /yaad <rule>, /rules, /bhul <n>.

    True = handled. Rules live in rules.py and are injected into every
    AI prompt, so they can never be buried by ordinary knowledge.
    """
    parts = (text or "").split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    if cmd not in ("/yaad", "/rules", "/bhul"):
        return False
    arg = parts[1].strip() if len(parts) > 1 else ""
    is_owner = uid == config.OWNER_CHAT_ID
    is_admin = is_owner or (config.ADMIN_CHAT_ID and uid == config.ADMIN_CHAT_ID)

    if cmd == "/yaad":
        if not is_owner:
            tg_api.send_message(chat_id, "Ye sirf owner kar sakta hai.")
            return True
        if not arg:
            tg_api.send_message(chat_id, "Usage: /yaad <hamesha yaad rakhne wali baat>")
            return True
        idx = rules.add_rule(arg, uid)
        if idx == -1:
            tg_api.send_message(chat_id, "Ye rule pehle se noted hai.")
        elif idx == 0:
            tg_api.send_message(chat_id, "Rule invalid hai.")
        else:
            tg_api.send_message(
                chat_id, f"✅ Yaad rakh liya (rule #{idx}). Ab se hamesha follow karunga.")
        return True

    if cmd == "/rules":
        if not is_admin:
            tg_api.send_message(chat_id, "Ye sirf owner/admin dekh sakte hain.")
            return True
        all_rules = rules.get_rules()
        if not all_rules:
            tg_api.send_message(chat_id, "Koi standing rule noted nahi hai.")
        else:
            lines = ["📋 Standing rules (owner):"]
            lines.extend(f"{i}. {r['rule']}" for i, r in enumerate(all_rules, 1))
            tg_api.send_message(chat_id, "\n".join(lines))
        return True

    # /bhul
    if not is_owner:
        tg_api.send_message(chat_id, "Ye sirf owner kar sakta hai.")
        return True
    try:
        n = int(arg)
    except ValueError:
        tg_api.send_message(chat_id, "Usage: /bhul <rule number>  (number /rules me dikhta hai)")
        return True
    removed = rules.remove_rule(n)
    if removed:
        tg_api.send_message(chat_id, f"🗑 Rule #{n} bhula diya: {removed}")
    else:
        tg_api.send_message(chat_id, f"Rule #{n} mila nahi.")
    return True


# ---------------- media (photo / voice / video) ----------------
# Gemini model capabilities (per docs): images up to 7MB inline each
# (png/jpeg/webp/heic); audio up to ~9.5h (ogg/mp3/wav/aac/flac);
# video up to ~45 min with audio, 20MB inline ceiling per request.
# Bot operating caps below stay comfortably inside those limits so replies
# stay fast.

_MEDIA_KIND_LABEL = {"photo": "[photo]", "voice": "[voice message]",
                     "video": "[video]"}
_MAX_MEDIA_BYTES = 15 * 1024 * 1024   # under the 20MB inline ceiling
_MAX_VOICE_SECONDS = 600              # 10 min
_MAX_VIDEO_SECONDS = 600              # 10 min (model allows ~45 min)


def _media_kinds(msg: dict) -> list[str]:
    """Cheap check: which media types are attached (no download)."""
    kinds = []
    if msg.get("photo"):
        kinds.append("photo")
    if (msg.get("voice") or {}).get("file_id"):
        kinds.append("voice")
    vid = msg.get("video") or msg.get("video_note") or {}
    if vid.get("file_id"):
        kinds.append("video")
    return kinds


def _download_media(msg: dict) -> tuple[list[dict], str | None]:
    """Download attached media for the AI.

    Returns (media_list, error). media items: {mime_type, data (b64), kind}.
    error is a human message when something was attached but unusable.
    """
    import base64

    media: list[dict] = []
    photo = msg.get("photo") or []
    if photo:
        biggest = max(photo, key=lambda p: p.get("file_size") or 0)
        data = tg_api.download_file(biggest.get("file_id"),
                                    max_bytes=_MAX_MEDIA_BYTES)
        if data is None:
            return [], "Photo download nahi ho paya — dobara bhejo."
        media.append({"mime_type": "image/jpeg",
                      "data": base64.b64encode(data).decode("ascii"),
                      "kind": "photo"})

    voice = msg.get("voice") or {}
    if voice.get("file_id"):
        if (voice.get("duration") or 0) > _MAX_VOICE_SECONDS:
            return [], (f"Voice message {_MAX_VOICE_SECONDS // 60} minute se lamba hai — "
                        "chhota bhejo.")
        data = tg_api.download_file(voice["file_id"],
                                    max_bytes=_MAX_MEDIA_BYTES)
        if data is None:
            return [], "Voice download nahi ho paya — dobara bhejo."
        media.append({"mime_type": voice.get("mime_type") or "audio/ogg",
                      "data": base64.b64encode(data).decode("ascii"),
                      "kind": "voice"})

    video = msg.get("video") or msg.get("video_note") or {}
    if video.get("file_id"):
        if (video.get("duration") or 0) > _MAX_VIDEO_SECONDS:
            return [], (f"Video {_MAX_VIDEO_SECONDS // 60} minute se lambi hai — "
                        "chhoti clip bhejo.")
        data = tg_api.download_file(video["file_id"],
                                    max_bytes=_MAX_MEDIA_BYTES)
        if data is None:
            return [], "Video download nahi ho paya — dobara bhejo ya chhoti file bhejo."
        media.append({"mime_type": video.get("mime_type") or "video/mp4",
                      "data": base64.b64encode(data).decode("ascii"),
                      "kind": "video"})
    return media, None


def _remember(chat_id: int, name: str, text: str,
              is_bot: bool = False, is_owner: bool = False) -> None:
    text = (text or "").strip()
    if chat_id and text:
        _GROUP_CONTEXT[chat_id].append(
            {"name": name, "text": text[:300], "is_bot": is_bot,
             "owner": is_owner})


def _remember_qa(uid: int, chat_id: int, question: str, answer: str | None,
                 date=None) -> None:
    """Save the bot's REAL answer with the question (its own track record).

    Future answers search this record, so claims like 'maine kya kaha /
    bheja tha' stay grounded in what was actually said — the bot can
    never again invent a past answer or file send. Never raises.
    """
    try:
        q = (question or "").strip()
        a = (answer or "").strip()
        if not q or not a or len(q) < 3:
            return
        db.get_db()["qa"].insert_one({
            "user_id": uid,
            "chat_id": chat_id,
            "question": q[:500],
            "answer": a[:2000],
            "date": date or int(time.time()),
        })
    except Exception:
        pass


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

    SPEED: hard timeout GATE_TIMEOUT_SECONDS (default 10s, less than the
    25s AI timeout) — a stuck gate must never hold back the pipeline for
    the full request budget. One-word YES/NO needs a fraction of that.
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
        + "- it is directed at another member (a reply to them, or "
          "mentioning them), or\n"
        + "- it is a statement or narration NOT addressed to you — someone "
          "sharing info, reacting, or answering ANOTHER member (e.g. 'kal "
          "diya tha ...', 'khela hu', 'acha', 'ye mast hai', 'so gya tha') "
          "— even if the topic touches something you said before, or\n"
        + "- it is just a greeting, thanks, ok, hmm with nothing else.\n"
        + "Answer with exactly one word: YES or NO."
    )
    try:
        out = ai_engine.get_router().generate(
            prompt, system=_GATE_SYSTEM, max_tokens=8,
            timeout=config.GATE_TIMEOUT_SECONDS,
        )
    except Exception:
        return False
    return out.strip().upper().startswith("YES")


def _start_answer_prefetch(question: str, uid: int | None):
    """SPEED: run the answer's DB work (knowledge search + rules + facts)
    in a daemon thread WHILE the reply-gate LLM call is in flight. The box
    is handed to agent.answer(ctx['prefetch']) — the search latency hides
    completely behind the gate roundtrip instead of stacking after it.
    """
    import threading
    box: dict = {}

    def _work():
        try:
            box["hits"] = knowledge.search(question, limit=10, user_id=uid)
        except Exception:
            box["hits"] = None
        try:
            box["standing"] = rules.rules_block()
        except Exception:
            box["standing"] = None
        try:
            box["facts"] = community.community_block()
        except Exception:
            box["facts"] = None

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    return box



# ---------------- handlers ----------------

def handle_my_chat_member(update) -> None:
    """Strict: only owner can activate the bot in a chat.

    Three cases, handled honestly:
    1. TELEGRAM SYSTEM auto-add (adder == the bot itself) — happens when
       the bot becomes admin of a channel and Telegram auto-adds it to the
       linked discussion group. NOT a stranger: stay quietly, never post
       the 'join @...' refuse message, never leave, never announce a
       'leave' that never happened (the old lie).
    2. A REAL stranger adds the bot — refuse + leave + report to owner
       (only claiming 'leave kar diya' when the API confirms it).
    3. The OWNER adds it — auto-set as managed group/channel. For channels
       the username/description is fetched immediately so the AI always
       knows the channel's identity and its owner handle.
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

    bot_id = ((new.get("user") or {}).get("id")) or _bot_id()

    # ---- CASE 1: Telegram system auto-add (adder is the bot itself) ----
    # Jab bot channel ka admin banta hai, Telegram uske linked discussion
    # group me bot ko KHUD add kar deta hai — adder 'from' bot ka apna ID
    # hota hai. Ye stranger add NAHI hai: quietly raho, kuch post mat karo,
    # leave mat karo (warna channel ka discussion access chhoot jata hai).
    if bot_id and adder_id == bot_id:
        linked = ""
        try:
            if links.get_channel_id():
                info = tg_api.get_chat(links.get_channel_id()) or {}
                if info.get("linked_chat_id") == chat_id:
                    linked = (" (ye linked channel ka discussion group hai — "
                              "isliye yahin rahunga)")
        except Exception:
            pass
        log(f"system auto-add to {chat_type} {chat_id} — staying" + linked)
        try:
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                f"ℹ️ Telegram ne mujhe apne aap `{title}` me add kiya"
                f"{linked or ' (system add)'} — main ruk gaya, wahan kuch "
                "post nahi karunga.",
            )
        except Exception:
            pass
        return

    # ---- CASE 2: a real stranger added the bot ----
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
        left = tg_api.leave_chat(chat_id)
        # log + notify owner — only claim the leave when it really happened.
        log(f"refused non-owner add to {chat_type} {chat_id} by {adder_id}")
        if left.get("ok"):
            outcome = ("Maine bina channel par post kiye reject karke leave "
                       "kar diya.")
        else:
            outcome = ("Leave nahi ho paya — owner manually remove kar de. "
                       f"({left.get('description') or 'unknown error'})")
        try:
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                f"⚠️ Kisi ne ({adder_id}) bot ko apne {chat_type} me add kiya: "
                f"{title}. {outcome}",
            )
        except Exception:
            pass
        return

    # ---- CASE 3: OWNER added — auto-set as managed chat ----
    if chat_type in ("group", "supergroup"):
        prev = links.get_group_id()
        links.set_group(chat_id, title)
        community.refresh_now()
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
        community.refresh_now()
        if prev and prev != chat_id:
            try:
                tg_api.leave_chat(prev)
            except Exception:
                pass
        is_admin = status in ("administrator", "creator")
        # Fetch + cache the channel's identity right now: username (public
        # link) and description (often carries 'Owner: @handle') — this is
        # what lets the AI answer 'channel ka owner kaun hai' truthfully.
        username = ""
        desc_note = ""
        try:
            info = tg_api.get_chat(chat_id) or {}
            username = (info.get("username") or "").strip()
            if username:
                db.save_state("managed_channel_username", username)
            desc = (info.get("description") or "").strip()
            owner_handle = community.extract_channel_owner(desc)
            if owner_handle:
                db.save_state("channel_owner_handle", owner_handle)
                desc_note = f"\nChannel owner (description se): {owner_handle}"
            linked_group = info.get("linked_chat_id")
            if linked_group:
                desc_note += f"\nLinked discussion group: `{linked_group}`"
        except Exception:
            pass
        link_line = f"https://t.me/{username}" if username else "(private channel — invite link se hi khulega)"
        try:
            tg_api.send_message(
                config.OWNER_CHAT_ID,
                f"✅ Channel auto-set!\n\nTitle: {title}\nID: `{chat_id}`\n"
                f"Link: {link_line}{desc_note}\n"
                f"Read access: {'yes' if is_admin else 'not yet'}\n\n"
                "Channel me main kabhi khud se post NAHI karunga — sirf "
                "tumhare /post command par. Members channel ka content "
                "maange to (link/number se) main forward kar dunga. "
                "Post Messages permission OFF hi rakh sakte ho.",
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
        # /post <text> — OWNER-ONLY channel posting. Bot kabhi bhi channel
        # me apne aap post nahi karta; sirf is command par.
        if is_owner and text.lower().startswith("/post"):
            if _handle_owner_channel_post(msg, chat_id, text):
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
        # Channel content delivery in DM too (/get + link/number/'vo wala')
        # — owner DM me bhi REAL forward hota hai, sirf baat nahi.
        channel_forward.note_link_in_text(chat_id, text)
        if channel_forward.handle_get_command(msg, chat_id, uid):
            return
        if channel_forward.handle_forward_request(msg, chat_id, uid):
            return
        # otherwise: treat as a normal question (owner context)
        kinds = _media_kinds(msg)
        if not text and not kinds:
            return
        question = text or " ".join(_MEDIA_KIND_LABEL[k] for k in kinds)
        media, media_err = _download_media(msg) if kinds else ([], None)
        if media_err:
            tg_api.send_message(chat_id, f"⚠️ {media_err}")
            return
        tg_api.send_chat_action(chat_id, "typing")
        ctx = {"user_id": uid, "chat_id": chat_id, "first_name": user.get("first_name"),
               "username": user.get("username"), "owner": True, "media": media}
        try:
            res = agent.answer(question, ctx)
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
        _remember(chat_id, _who,
                  text or " ".join(_MEDIA_KIND_LABEL[k] for k in _media_kinds(msg)),
                  is_owner=(uid == config.OWNER_CHAT_ID))

        # Deterministic REAL channel-content delivery — t.me link, post
        # number, 'latest', ya 'vo wala message bhejo'. Ye sabse PEHLE
        # chalta hai (teach/LLM se bhi pehle): forwardMessage sach me hota
        # hai, LLM kabhi sirf 'nikal kar pahuncha deta hu' bol nahi sakta.
        channel_forward.note_link_in_text(chat_id, text)
        if channel_forward.handle_forward_request(msg, chat_id, uid):
            return
        if channel_forward.handle_get_command(msg, chat_id, uid):
            return

        # Owner-taught auto-actions ('/sikhao' system) fire on normal
        # messages BEFORE any gate — jo sikhaya gaya hai wahi hoga.
        if teach.handle_teach_command(msg, uid, chat_id):
            return
        if not text.startswith("/") and teach.match_and_act(msg, chat_id):
            return

        # Public utility commands: /help /id /stats /files /report —
        # sab members use kar sakte hain.
        if commands.handle_public_command(msg, uid, chat_id):
            return

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
                reply_to_message_id=msg.get("message_id"),
            )
            return

        # Rose-style mod tools (owner/admin): /filter /mute /tmute /unmute
        # /ban /unban /delete. Handled before the reply gate so commands
        # are never swallowed as chatter.
        if moderation.handle_mod_command(msg, uid, chat_id):
            return

        # Standing-rule commands: /yaad (owner), /rules, /bhul (owner).
        if _handle_rule_command(msg, uid, chat_id, text):
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
        prefetch_box: dict | None = None

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
                # SPEED: answer-context (search/rules/facts) builds in a
                # daemon thread WHILE the gate LLM call runs — if the gate
                # says YES the answer starts instantly, zero added latency.
                prefetch_box = _start_answer_prefetch(text, uid)
                # Everything else: the bot decides itself with the LLM gate
                # (it sees recent conversation context).
                who = user.get("first_name") or user.get("username") or "member"
                if not _should_reply_llm(text, who, chat_id,
                                         is_owner=(uid == config.OWNER_CHAT_ID)):
                    return
            else:
                prefetch_box = _start_answer_prefetch(text, uid)

        # Conversational moderation: members can NEVER trigger actions on
        # their word — only the owner can. The owner's spoken request becomes
        # a REAL command through the same pipeline (never faked).
        if _looks_like_mod_request(text):
            mid = msg.get("message_id")
            if uid != config.OWNER_CHAT_ID:
                _deliver_answer(chat_id, {
                    "text": "Bhai, delete/mute/ban ye sab sirf owner ke kehne pe karta hu — "
                            "kisi member ke bolne pe kisi pe action nahi lunga. 🙂",
                }, reply_to=mid)
                return
            synth = _synthesize_owner_command(text, msg, chat_id)
            if synth and moderation.handle_mod_command(synth, uid, chat_id):
                return
            # No clear action+target: NEVER let the LLM claim the action.
            # Seedha pucho — kisko? Reply/tag/naam batao.
            _deliver_answer(chat_id, {
                "text": "Haan bhai, kar dunga — par kis par? 🤔\n"
                        "Uske kisi message pe REPLY karke bolo, ya @username "
                        "tag karo, ya naam batao (jaise 'rich ko unmute kar do').",
            }, reply_to=mid)
            return

        if is_ask:
            question = text[ask_match.end():].strip()
        else:
            question = text
        if prefetch_box is None:
            prefetch_box = _start_answer_prefetch(text, uid)
        # Media-only message: give the gate something to judge from context
        # (e.g. the bot just asked for a screenshot).
        kinds = _media_kinds(msg)
        if not question and kinds:
            question = " ".join(_MEDIA_KIND_LABEL[k] for k in kinds)
        # Strip bot username from the question.
        uname = _bot_username()
        if uname:
            question = re.sub(rf"@{re.escape(uname)}\b", "", question, flags=re.IGNORECASE).strip()
        if not question:
            return
        if triggers.is_group_scan_request(question):
            _deliver_answer(chat_id, _group_scan_answer(chat_id),
                            reply_to=msg.get("message_id"))
            return
        # Download attached media (only now — not for ignored messages).
        media, media_err = _download_media(msg) if kinds else ([], None)
        if media_err:
            _deliver_answer(chat_id, {"text": f"⚠️ {media_err}"},
                            reply_to=msg.get("message_id"))
            return
        tg_api.send_chat_action(chat_id, "typing")
        # Grounding: the model gets the verbatim recent log (incl. its own
        # replies) so it never hallucinates past events or file sends.
        _recent = list(_GROUP_CONTEXT.get(chat_id, []))[:-1][-14:]
        ctx = {"user_id": uid, "chat_id": chat_id, "first_name": user.get("first_name"),
               "username": user.get("username"), "group": True, "group_id": chat_id,
               "group_role": _group_role(chat_id, uid),
               "bot_admin": _bot_rights(chat_id),
               "media": media, "recent_log": _recent, "prefetch": prefetch_box}
        try:
            res = agent.answer(question, ctx)
        except Exception:
            log("agent.answer failed:", traceback.format_exc())
            res = {"text": None, "grounded": False, "error": "internal"}
        # Deliver FIRST — the answer must reach the user before any DB bookkeeping.
        _deliver_answer(chat_id, res, reply_to=msg.get("message_id"))
        # Long-term memory of what the bot ACTUALLY answered — grounding
        # for future 'kya tumne bola/bheja tha' questions (no false claims).
        # SPEED: after delivery — this Mongo write no longer sits between
        # the user's question and the bot's answer.
        _remember_qa(uid, chat_id, question, res.get("text"),
                     msg.get("date"))
        return


def _deliver_answer(chat_id: int, res: dict, reply_to: int | None = None) -> None:
    """Send the answer / file to the user.

    The bot ALWAYS replies itself and never escalates automatically.
    Human escalation is manual only, via the /admin command.
    The answer may contain several messages separated by a line with
    only <SPLIT> — each is sent (and remembered) as its own message,
    like a human manager would.
    Group me jawab hamesha user ke message ka REPLY hota hai — taaki
    sabko dikhe kis baat ka jawab hai.
    """
    def _send_and_remember(part: str) -> None:
        r = tg_api.send_message(chat_id, part,
                                reply_to_message_id=reply_to)
        if not r.get("ok"):
            # reply target gayab ho to plain send (never lose the answer)
            tg_api.send_message(chat_id, part)
        # Remember the bot's own message for the reply gate's context.
        if chat_id and chat_id < 0:
            _remember(chat_id, "RK Manager", part, is_bot=True)

    # Interim acknowledgement from a deep-lookup round ("Ruko bhai...").
    interim = (res.get("interim") or "").strip()
    if interim:
        _send_and_remember(interim)
        time.sleep(0.4)

    text = res.get("text") or ""
    parts = [p.strip() for p in text.split("<SPLIT>") if p.strip()]
    if parts:
        for i, part in enumerate(parts):
            _send_and_remember(part)
            if i < len(parts) - 1:
                time.sleep(0.4)
    elif not interim:
        _send_and_remember("🤔 Samajh nahi aaya — thoda detail me pucho.")

    # If there's a file offer, send it (files, media, contacts — channel se)
    fo = res.get("file_offer")
    if fo:
        try:
            agent._deliver_channel_item(chat_id, fo)
        except Exception:
            pass


# ---------------- run modes ----------------


def _dispatch_update_async(u) -> None:
    """Run update handling in a background thread.

    Webhook mode: Telegram ko turant 200 milta hai, processing background
    me hoti hai — user ko faster perceived response + Telegram retries nahi.
    """
    import threading
    t = threading.Thread(target=_dispatch_update, args=(u,), daemon=True)
    t.start()


def _keepalive_loop(url: str) -> None:
    """Self-ping every 10 min — Render free tier me service sleep (cold
    start 10-20s) hone nahi deta, isliye bot ka reply turant aata hai.
    """
    import threading
    import urllib.request

    def _loop():
        while True:
            time.sleep(600)  # 10 min
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()


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
            # Telegram ko TURANT 200 do; update background me process karo.
            # Isse webhook kabhi slow/timeout nahi hota, aur user ka answer
            # jitna fast ho sakta tha utna fast banta hai.
            try:
                u = json.loads(body.decode("utf-8"))
                _dispatch_update_async(u)
            except Exception:
                log("webhook update failed:", traceback.format_exc())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    log(f"webhook mode on port {config.PORT} (secret path configured)")
    # Render free tier cold-start fix: khud ko har 10 min ping karo.
    if config.WEBHOOK_URL:
        _keepalive_loop(config.WEBHOOK_URL + "/healthz")
        log("keep-alive self-ping enabled (10 min interval)")
    HTTPServer(("0.0.0.0", config.PORT), H).serve_forever()


def main() -> None:
    log("starting RK Manager Agent…")
    log(f"owner={config.OWNER_CHAT_ID} admin={config.ADMIN_CHAT_ID or config.OWNER_CHAT_ID}")
    log(f"gemini_keys={len(config.GEMINI_KEYS)} deepseek={'yes' if config.DEEPSEEK_KEY else 'no'}")
    log(
        "storage=MongoDB (persistent)" if db.is_persistent()
        else "storage=in-memory (volatile; restart loses stored data)"
    )
    # Telegram command menu (the '/' button) register karo — best-effort.
    try:
        commands.setup_bot_commands()
        log("bot command menu registered")
    except Exception:
        log("command menu registration failed (non-fatal)")
    if config.WEBHOOK_URL:
        run_webhook()
    else:
        run_polling()


if __name__ == "__main__":
    main()
