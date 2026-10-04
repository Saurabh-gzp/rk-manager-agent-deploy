"""Two-level escalation: User -> ADMIN (first) -> OWNER (last resort).

When the agent can't answer:
  1. Notify ADMIN (or OWNER if not configured) with the user question.
  2. ADMIN gets buttons: "✍️ User ko jawab do" or "⏫ Owner ko forward karo".
  3. If ADMIN forwards to OWNER, OWNER gets "✍️ User ko jawab do".
  4. Either's reply goes back to the user; status -> resolved.
"""
from __future__ import annotations
import time
from typing import Any

import config
from db import get_db, get_state, save_state


def _send(chat_id, text, reply_markup=None):
    import tg_api
    return tg_api.send_message(chat_id, text, reply_markup=reply_markup)


def _edit(chat_id, message_id, text, reply_markup=None):
    import tg_api
    tg_api.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)


def _answer(cb_id, text=None, show_alert=False):
    import tg_api
    tg_api.answer_callback_query(cb_id, text=text, show_alert=show_alert)


def _eid_filter(eid):
    try:
        from bson import ObjectId
        return {"_id": ObjectId(eid)}
    except Exception:
        return {"_id": eid}


def _get(eid):
    return get_db()["escalations"].find_one(_eid_filter(eid))


_STALE_SECONDS = 48 * 3600  # 48 ghante tak pending rahi to auto-expire


def _expire_stale() -> None:
    """Pending escalations older than 48h ko 'expired' mark karo."""
    try:
        get_db()["escalations"].update_many(
            {"status": "pending_admin",
             "created_ts": {"$lt": time.time() - _STALE_SECONDS}},
            {"$set": {"status": "expired"}},
        )
    except Exception:
        pass


def _admin_id() -> int:
    return config.ADMIN_CHAT_ID or config.OWNER_CHAT_ID or 0


def _owner_id() -> int:
    return config.OWNER_CHAT_ID or 0


def open_escalation(user_id: int, user_chat_id: int, question: str,
                    source: str = "private") -> str:
    """Create a new escalation, ping admin first. Returns escalation id."""
    _expire_stale()
    db = get_db()
    esc = {
        "user_id": user_id,
        "user_chat_id": user_chat_id,
        "question": question,
        "status": "pending_admin",
        "source": source,
        "created_ts": time.time(),
        "history": [],
    }
    res = db["escalations"].insert_one(esc)
    eid = str(res.inserted_id)

    admin = _admin_id()
    if admin:
        text = (
            "🆘 Naya escalation (pehle aapko)\n\n"
            f"From: {user_id} ({source})\n"
            f"Sawal: {question}\n\n"
            "Aapke paas options:\n"
            "• ✍️ User ko jawab do — DM me bhejo, main user tak pahunchata hu\n"
            "• ⏫ Owner ko forward karo — last resort"
        )
        kb = {"inline_keyboard": [[
            {"text": "✍️ User ko jawab do", "callback_data": f"esc:reply:{eid}"},
            {"text": "⏫ Owner ko forward", "callback_data": f"esc:up:{eid}"},
        ]]}
        _send(admin, text, reply_markup=kb)
    return eid


def handle_callback(cb) -> bool:
    """Route esc:* callbacks. Returns True if handled."""
    data = (cb.get("data") or "")
    if not data.startswith("esc:"):
        return False
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    eid = parts[2] if len(parts) > 2 else ""
    cb_id = cb.get("id")
    from_uid = (cb.get("from") or {}).get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    # route reply button: starts "awaiting_reply" mode for the clicker
    if action in ("reply", "oreply"):
        if from_uid not in (_admin_id(), _owner_id()):
            _answer(cb_id, "Sirf admin/owner.", show_alert=True)
            return True
        if action == "oreply" and from_uid != _owner_id():
            _answer(cb_id, "Sirf owner.", show_alert=True)
            return True
        esc = _get(eid)
        expected_status = "pending_owner" if action == "oreply" else "pending_admin"
        if not esc or esc.get("status") != expected_status:
            _answer(cb_id, "Ye escalation ab open nahi hai.", show_alert=True)
            return True
        save_state(f"awaiting_reply:{from_uid}", eid)
        _edit(chat_id, message_id, "✍️ Apna jawab DM me likho, main user tak pahunchata hu.",
              reply_markup={"inline_keyboard": [
                  [{"text": "❌ Cancel", "callback_data": f"esc:cancel:{eid}"}]
              ]})
        _answer(cb_id)
        return True

    if action == "up":
        # admin -> owner
        admin_id = _admin_id()
        owner_id = _owner_id()
        if admin_id and from_uid != admin_id:
            _answer(cb_id, "Sirf admin.", show_alert=True)
            return True
        esc = _get(eid)
        if not esc or esc.get("status") != "pending_admin":
            _answer(cb_id, "Ye escalation ab admin ke liye open nahi hai.", show_alert=True)
            return True
        if not owner_id or owner_id == admin_id:
            _answer(cb_id, "Owner configured nahi hai.")
            _edit(chat_id, message_id, "Owner configured nahi — admin khud jawab do.")
            return True
        get_db()["escalations"].update_one(
            _eid_filter(eid),
            {"$set": {"status": "pending_owner"}, "$push": {
                "history": {"by": from_uid, "action": "forward_to_owner", "ts": time.time()}
            }},
        )
        esc = _get(eid) or {}
        text = (
            "⏫ Admin ne owner ko forward kiya\n\n"
            f"From user: {esc.get('user_id')}\n"
            f"Sawal: {esc.get('question')}\n\n"
            "Aap last resort ho — owner se jawab do."
        )
        kb = {"inline_keyboard": [[
            {"text": "✍️ User ko jawab do", "callback_data": f"esc:oreply:{eid}"},
        ]]}
        _send(owner_id, text, reply_markup=kb)
        _answer(cb_id, "Owner ko bhej diya ✅")
        _edit(chat_id, message_id, "✅ Owner ko bhej diya. Owner ka jawab user tak pahunchega.")
        return True

    if action == "cancel":
        if from_uid not in (_admin_id(), _owner_id()):
            _answer(cb_id, "Sirf admin/owner.", show_alert=True)
            return True
        esc = _get(eid)
        if not esc or esc.get("status") in ("resolved", "cancelled"):
            _answer(cb_id, "Ye escalation ab open nahi hai.", show_alert=True)
            return True
        if get_state(f"awaiting_reply:{from_uid}") == eid:
            save_state(f"awaiting_reply:{from_uid}", None)
        get_db()["escalations"].update_one(
            _eid_filter(eid),
            {"$set": {"status": "cancelled"}},
        )
        _answer(cb_id, "Cancelled")
        _edit(chat_id, message_id, "❌ Cancelled.")
        return True

    _answer(cb_id, "Pata nahi ye button.")
    return True


def handle_admin_message(msg) -> bool:
    """Relay admin/owner reply to waiting user. Returns True if handled."""
    from_uid = (msg.get("from") or {}).get("id")
    if from_uid not in (_admin_id(), _owner_id()) or not from_uid:
        return False
    eid = get_state(f"awaiting_reply:{from_uid}")
    if not eid:
        return False
    esc = _get(eid)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not esc or esc.get("status") in ("resolved", "cancelled") or not text:
        save_state(f"awaiting_reply:{from_uid}", None)
        return False
    user_chat_id = (esc or {}).get("user_chat_id")
    if user_chat_id and text:
        prefix = "💬 Admin ka jawab" if from_uid == _admin_id() else "💬 Owner ka jawab"
        _send(user_chat_id, f"{prefix}:\n\n{text}")
    if esc:
        get_db()["escalations"].update_one(
            _eid_filter(eid),
            {"$set": {"status": "resolved", "resolved_by": from_uid, "resolved_at": time.time()},
             "$push": {"history": {"by": from_uid, "reply": text, "ts": time.time()}}},
        )
        # also save to qa collection for future learning
        try:
            q = (esc.get("question") or "").strip()
            if q and text:
                get_db()["qa"].insert_one({
                    "question": q,
                    "answer": text,
                    "chat_id": esc.get("user_chat_id"),
                    "user_id": esc.get("user_id"),
                    "answered_by": from_uid,
                    "date": time.time(),
                })
        except Exception:
            pass
    save_state(f"awaiting_reply:{from_uid}", None)
    _send(msg.get("chat", {}).get("id"), "✅ User tak pahunch gaya.")
    return True


def pending_for(who: str) -> list[dict]:
    """who = 'admin' or 'owner'. Return pending escalations for that level."""
    db = get_db()
    if who == "admin":
        return list(db["escalations"].find({"status": "pending_admin"}))
    return list(db["escalations"].find({"status": "pending_owner"}))