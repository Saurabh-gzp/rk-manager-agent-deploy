"""RK Manager Agent — main entry.

Dispatch:
  - my_chat_member   -> auto-detect group/channel + announce
  - channel_post     -> knowledge.ingest_channel_post
  - callback_query   -> owner_panel.handle_callback, escalation.handle_callback
  - message:
      private (owner ONLY): /start -> owner_panel.handle_start
                            /forget -> owner_panel.handle_forget_command
                            forget-mode text -> owner_panel.handle_forget_mode_text
                            DM reply to escalation -> escalation.handle_admin_message
                            other text -> treated as a question, answered (no escalation)
      private (anyone else): silently ignored. Bot only talks to its owner in DMs.
      group/supergroup:       ALWAYS ingest
                              /ask or @mention -> agent.answer; if not grounded, escalate

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


def _is_private_owner(uid: int) -> bool:
    return bool(uid) and uid == config.OWNER_CHAT_ID


def _is_private_admin(uid: int) -> bool:
    return _is_private_owner(uid) or (bool(uid) and uid == (config.ADMIN_CHAT_ID or 0))


# ---------------- handlers ----------------

def handle_my_chat_member(update) -> None:
    cm = update.get("my_chat_member") or {}
    chat = cm.get("chat") or {}
    new = cm.get("new_chat_member") or {}
    status = new.get("status")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    title = chat.get("title") or chat.get("username") or "—"

    if status in ("kicked", "left"):
        # auto-clear if it was our managed one
        if links.is_managed_group(chat_id):
            links.clear_group()
            log(f"left managed group {chat_id}")
        elif links.is_managed_channel(chat_id):
            links.clear_channel()
            log(f"left managed channel {chat_id}")
        return

    if status not in ("administrator", "member", "creator"):
        return

    # Auto-set if not already configured
    if chat_type in ("group", "supergroup"):
        if not links.get_group_id():
            links.set_group(chat_id, title)
            log(f"auto-set group: {chat_id} {title}")
        elif links.get_group_id() != chat_id:
            # not our main group, just greet
            pass
        greeting = (
            "👋 Namaste! Main RK Manager hoon.\n\n"
            "✅ Is group ko maine apna primary group bana liya.\n"
            "Ab main yahan ki har chat se seekhunga aur members ke sawalon ka jawab dunga."
        )
        tg_api.send_message(chat_id, greeting)
    elif chat_type == "channel":
        if not links.get_channel_id():
            links.set_channel(chat_id, title)
            log(f"auto-set channel: {chat_id} {title}")
        # Check admin status
        me = tg_api.get_me()
        bot_id = me.get("id")
        admins = tg_api.get_chat_administrators(chat_id) or []
        is_admin = any((a.get("user") or {}).get("id") == bot_id for a in admins)
        if is_admin:
            tg_api.send_message(chat_id, "✅ Channel se seekhna shuru! Posts auto-index hongi.")
        else:
            tg_api.send_message(chat_id, "⚠️ Mujhe admin banao (Post messages) taaki channel se seekh sakun.")


def handle_channel_post(post) -> None:
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
        if uid != config.OWNER_CHAT_ID:
            # non-owner DM: do nothing
            return
        # /start
        if text == "/start" or text.startswith("/start "):
            owner_panel.handle_start(msg)
            return
        # /forget
        if text.startswith("/forget"):
            if owner_panel.handle_forget_command(msg):
                return
        # forget-mode text (after panel button)
        if owner_panel.handle_forget_mode_text(msg):
            return
        # admin/owner reply to escalation
        if escalation.handle_admin_message(msg):
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
        _deliver_answer(chat_id, res, escalate=False)
        return

    # ---------------- group chat ----------------
    if chat_type in ("group", "supergroup"):
        # Learn from EVERY non-command non-bot message
        knowledge.ingest_message(msg)

        # Only respond to explicit asks or mentions
        is_ask = text.startswith("/ask ")
        is_mention = _mentions_bot(text, msg.get("entities") or msg.get("caption_entities") or [])
        if not (is_ask or is_mention):
            return

        question = text[len("/ask "):].strip() if is_ask else text
        # strip bot username from the question
        uname = _bot_username()
        if uname:
            question = re.sub(rf"@{re.escape(uname)}\b", "", question, flags=re.IGNORECASE).strip()
        if not question:
            return

        tg_api.send_chat_action(chat_id, "typing")
        ctx = {"user_id": uid, "chat_id": chat_id, "first_name": user.get("first_name"),
               "username": user.get("username"), "group": True, "group_id": chat_id}
        try:
            res = agent.answer(question, ctx)
        except Exception:
            log("agent.answer failed:", traceback.format_exc())
            res = {"text": None, "grounded": False, "error": "internal"}
        _deliver_answer(chat_id, res, escalate=True)
        return


def _deliver_answer(chat_id: int, res: dict, escalate: bool) -> None:
    """Send the answer / file to the user; if not grounded, escalate."""
    text = res.get("text") or ""
    if text:
        tg_api.send_message(chat_id, text)

    # If there's a file offer, send it
    fo = res.get("file_offer")
    if fo:
        try:
            tg_api.send_any_file(chat_id, fo["file_id"], fo.get("file_type", "document"),
                                 caption=fo.get("caption"))
        except Exception:
            pass

    if escalate and not res.get("grounded"):
        eid = escalation.open_escalation(
            user_id=0, user_chat_id=chat_id,
            question=(res.get("text") or "(no text)")[:400],
            source="group" if chat_id < 0 else "private",
        )
        if not text:
            tg_api.send_message(
                chat_id,
                f"🤔 Iska jawab mere paas nahi hai. Main admin/owner ko bhej raha hu. (ref: {eid[:8]})"
            )


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
    elif "callback_query" in u:
        handle_callback_query(u["callback_query"])
    elif "message" in u:
        handle_message(u["message"])


def run_webhook() -> None:
    secret = config.WEBHOOK_SECRET
    hook_path = "/" + secret
    full_url = config.WEBHOOK_URL + hook_path
    try:
        r = tg_api.set_webhook(full_url)
        log("setWebhook ->", r.get("ok"), r.get("description", ""))
    except Exception:
        log("setWebhook failed:", traceback.format_exc())

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != hook_path.rstrip("/"):
                self.send_response(404)
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

    log(f"webhook mode on port {config.PORT} path {hook_path}")
    HTTPServer(("0.0.0.0", config.PORT), H).serve_forever()


def main() -> None:
    log("starting RK Manager Agent…")
    log(f"owner={config.OWNER_CHAT_ID} admin={config.ADMIN_CHAT_ID or config.OWNER_CHAT_ID}")
    log(f"gemini_keys={len(config.GEMINI_KEYS)} deepseek={'yes' if config.DEEPSEEK_KEY else 'no'}")
    log(f"mongo={'yes' if config.MONGODB_URI else 'no (in-memory)'}")
    if config.WEBHOOK_URL:
        run_webhook()
    else:
        run_polling()


if __name__ == "__main__":
    main()
