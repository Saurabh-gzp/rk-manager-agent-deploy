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


def _remember(chat_id: int, name: str, text: str,
              is_bot: bool = False, is_owner: bool = False) -> None:
    text = (text or "").strip()
    if chat_id and text:
        _GROUP_CONTEXT[chat_id].append(
            {"name": name, "text": text[:300], "is_bot": is_bot,
             "owner": is_owner})


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
               "username": user.get("username"), "group": True, "group_id": chat_id}
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
    """
    text = res.get("text") or ""
    if text:
        tg_api.send_message(chat_id, text)
    else:
        text = "🤔 Samajh nahi aaya — thoda detail me pucho."
        tg_api.send_message(chat_id, text)
    # Remember the bot's own message for the reply gate's context.
    if chat_id and chat_id < 0:
        _remember(chat_id, "RK Manager", text, is_bot=True)

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
