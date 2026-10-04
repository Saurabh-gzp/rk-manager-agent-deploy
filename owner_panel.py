"""Owner DM control panel.

Layout (text-first, simple buttons):
  - ➕ Group Admin        (set / clear / leave / view members)
  - ➕ Channel Admin      (set / clear / leave / view posts)
  - 🔗 Link Group <-> Channel
  - 🤖 Choose Model      (live list of Gemini models -> inline buttons)
  - 🔑 API Keys Manager  (provider list, key status, enable/disable, test)
  - 🧠 Memory Cleanup    (manual prune, channel reconcile, forget)
  - 📊 Stats
  - ❓ Help

Only OWNER_CHAT_ID can use this. ADMIN can also see Stats + pending escalations.
"""
from __future__ import annotations
import re
import time
import json
from typing import Any

import ai_engine
import config
import escalation
import knowledge
import links
import tg_api


def _is_owner(uid: int) -> bool:
    return bool(uid) and uid == config.OWNER_CHAT_ID


def _is_admin(uid: int) -> bool:
    return _is_owner(uid) or (bool(uid) and uid == (config.ADMIN_CHAT_ID or 0))


def _send(chat_id, text, reply_markup=None):
    return tg_api.send_message(chat_id, text, reply_markup=reply_markup)


def _edit(chat_id, message_id, text, reply_markup=None):
    tg_api.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)


def _answer(cb_id, text=None, show_alert=False):
    tg_api.answer_callback_query(cb_id, text=text, show_alert=show_alert)


# ---------- panel main ----------

PANEL_TEXT = (
    "🎛 RK Manager Control Panel\n"
    "─────────────────────\n"
    "Yahan se group/channel manage karo, model + AI keys control karo,\n"
    "ya memory cleanup chala do. Sab kuch buttons se."
)


def panel_keyboard() -> dict:
    rows = [
        [{"text": "➕ Group Admin", "callback_data": "p:group"},
         {"text": "➕ Channel Admin", "callback_data": "p:channel"}],
        [{"text": "🔗 Link Group ↔ Channel", "callback_data": "p:link"}],
        [{"text": "🤖 Choose Model", "callback_data": "p:model"},
         {"text": "🔑 API Keys", "callback_data": "p:keys"}],
        [{"text": "🧠 Memory Cleanup", "callback_data": "p:memory"}],
        [{"text": "➕ Add bot to group", "callback_data": "p:addgroup"},
         {"text": "➕ Add bot to channel", "callback_data": "p:addchannel"}],
        [{"text": "📊 Stats", "callback_data": "p:stats"},
         {"text": "❓ Help", "callback_data": "p:help"}],
    ]
    return {"inline_keyboard": rows}


def handle_start(msg) -> bool:
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    if not _is_owner(uid):
        _send(chat_id, "Ye bot RK Manager ka control panel hai. Owner-only 🙂")
        return True
    _send(chat_id, PANEL_TEXT, reply_markup=panel_keyboard())
    return True


def handle_forget_command(msg) -> bool:
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text.startswith("/forget"):
        return False
    if not _is_admin(uid):
        _send(chat_id, "Owner/admin only 🙂")
        return True
    q = text[len("/forget"):].strip()
    if not q:
        _send(chat_id, "Usage: /forget <query>")
        return True
    n = knowledge.forget_query(q)
    _send(chat_id, f"🗑 {n} rows bhula diye.")
    return True


def _back():
    return {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "p:back"}]]}


# ---------- callbacks ----------

def handle_callback(cb) -> bool:
    data = cb.get("data") or ""
    if not data.startswith("p:"):
        return False
    cb_id = cb.get("id")
    from_uid = (cb.get("from") or {}).get("id")
    if not _is_owner(from_uid):
        _answer(cb_id, "Owner-only 🙂", show_alert=True)
        return True
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    action = data[2:]

    # ---------- group/channel admin ----------

    if action == "group":
        gid = links.get_group_id()
        title = get_state("group_title") or "—"
        kb = {"inline_keyboard": [
            [{"text": "🔄 Refresh", "callback_data": "p:group"}],
            [{"text": "🗑 Clear", "callback_data": "p:group_clear"}],
            [{"text": "🚪 Leave group", "callback_data": "p:group_leave"}],
            [{"text": "⬅️ Back", "callback_data": "p:back"}],
        ]}
        if not gid:
            text = (
                "➕ Group Admin\n\n"
                "Abhi koi group set nahi hai. Neeche button se bot ko group me add karo.\n"
                "Bot admin banake wapas yahan aao — auto-detect ho jayega."
            )
            kb["inline_keyboard"].insert(0, [
                {"text": "➕ Add bot to group", "url": _addgroup_url()}
            ])
        else:
            text = (
                f"➕ Group Admin\n\n"
                f"Group ID: `{gid}`\n"
                f"Title: {title}\n\n"
                f"Members: {tg_api.get_chat_member_count(gid)}"
            )
        _edit(chat_id, message_id, text, reply_markup=kb, parse_mode="Markdown")
        _answer(cb_id)
        return True

    if action == "group_clear":
        links.clear_group()
        _edit(chat_id, message_id, "✅ Group cleared.", reply_markup=_back())
        _answer(cb_id, "Cleared")
        return True

    if action == "group_leave":
        gid = links.get_group_id()
        if not gid:
            _edit(chat_id, message_id, "Koi group set nahi.", reply_markup=_back())
        else:
            tg_api.leave_chat(gid)
            links.clear_group()
            _edit(chat_id, message_id, "🚪 Group chhod diya.", reply_markup=_back())
        _answer(cb_id)
        return True

    if action == "channel":
        cid = links.get_channel_id()
        title = get_state("channel_title") or "—"
        kb = {"inline_keyboard": [
            [{"text": "🔄 Refresh", "callback_data": "p:channel"}],
            [{"text": "🗑 Clear", "callback_data": "p:channel_clear"}],
            [{"text": "🚪 Leave channel", "callback_data": "p:channel_leave"}],
            [{"text": "⬅️ Back", "callback_data": "p:back"}],
        ]}
        if not cid:
            text = (
                "📢 Channel Admin\n\n"
                "Abhi koi channel set nahi.\n"
                "Steps:\n"
                "1) Channel kholo > ⋮ > Administrators > Add > @yourbot search\n"
                "2) 'Post messages' permission de do\n"
                "3) Bot add hone par auto-detect hoga"
            )
        else:
            text = (
                f"📢 Channel Admin\n\n"
                f"Channel ID: `{cid}`\n"
                f"Title: {title}\n"
                f"Subscribers: {tg_api.get_chat_member_count(cid)}"
            )
        _edit(chat_id, message_id, text, reply_markup=kb, parse_mode="Markdown")
        _answer(cb_id)
        return True

    if action == "channel_clear":
        links.clear_channel()
        _edit(chat_id, message_id, "✅ Channel cleared.", reply_markup=_back())
        _answer(cb_id)
        return True

    if action == "channel_leave":
        cid = links.get_channel_id()
        if not cid:
            _edit(chat_id, message_id, "Koi channel set nahi.", reply_markup=_back())
        else:
            tg_api.leave_chat(cid)
            links.clear_channel()
            _edit(chat_id, message_id, "🚪 Channel chhod diya.", reply_markup=_back())
        _answer(cb_id)
        return True

    if action == "link":
        gid = links.get_group_id()
        cid = links.get_channel_id()
        text = "🔗 Link Group ↔ Channel\n\n"
        if gid and cid:
            text += f"✅ Linked:\nGroup: `{gid}`\nChannel: `{cid}`"
        else:
            text += (
                f"Group: {'`' + str(gid) + '`' if gid else '❌ not set'}\n"
                f"Channel: {'`' + str(cid) + '`' if cid else '❌ not set'}\n\n"
                "Dono set karne ke baad auto-link ho jayega."
            )
        _edit(chat_id, message_id, text, reply_markup=_back(), parse_mode="Markdown")
        _answer(cb_id)
        return True

    # ---------- add bot buttons (deep links) ----------

    if action == "addgroup":
        kb = {"inline_keyboard": [
            [{"text": "➕ Bot ko group me add karo", "url": _addgroup_url()}],
            [{"text": "⬅️ Back", "callback_data": "p:back"}],
        ]}
        _edit(chat_id, message_id, "➕ Add to group\n\nClick karke group select karo. Bot admin banake wapas /start bhejo.", reply_markup=kb)
        _answer(cb_id)
        return True

    if action == "addchannel":
        kb = {"inline_keyboard": [
            [{"text": "➕ Bot ko channel me add karo", "url": _addchannel_url()}],
            [{"text": "⬅️ Back", "callback_data": "p:back"}],
        ]}
        _edit(chat_id, message_id, "➕ Add to channel\n\nClick karke channel select karo. Bot admin banake wapas /start bhejo.", reply_markup=kb)
        _answer(cb_id)
        return True

    # ---------- model chooser ----------

    if action == "model":
        try:
            models = ai_engine.list_gemini_models()
        except Exception as exc:
            _edit(chat_id, message_id, f"Model list fail: {exc}", reply_markup=_back())
            _answer(cb_id)
            return True
        if not models:
            _edit(chat_id, message_id, "Pehle API keys add karo. 🔑", reply_markup=_back())
            _answer(cb_id)
            return True
        current = get_state("model", config.DEFAULT_MODEL)
        rows = []
        for m in models[:30]:
            label = f"{'✅ ' if m == current else '   '}{m}"
            rows.append([{"text": label, "callback_data": f"p:mset:{m}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
        _edit(chat_id, message_id, f"🤖 Model choose karo (current: {current})", reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("mset:"):
        m = action[len("mset:"):]
        save_state("model", m)
        # also update default model in running router's gemini provider
        router = ai_engine.get_router()
        for prov in router.providers:
            if prov.name == "gemini":
                prov.model = m
        _edit(chat_id, message_id, f"✅ Model set: {m}", reply_markup=_back())
        _answer(cb_id, "Model set ✅")
        return True

    # ---------- API keys ----------

    if action == "keys":
        router = ai_engine.get_router()
        if router.key_count() == 0:
            _edit(chat_id, message_id, "🔑 Koi AI key configure nahi hai.\nGEMINI_KEYS / DEEPSEEK_KEY env var set karo.", reply_markup=_back())
            _answer(cb_id)
            return True
        rows = []
        for s in router.status():
            label = f"{'✅' if s['healthy'] else '⛔'} [{s['provider']}] {s['masked']}"
            rows.append([{"text": label, "callback_data": f"p:keyinfo:{s['provider']}:{s['index']}"}])
        rows.append([{"text": "🧪 Test all", "callback_data": "p:keytest_all"}])
        rows.append([{"text": "♻️ Reset backoff", "callback_data": "p:keyreset"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
        text = (
            f"🔑 API Keys ({router.key_count()} total, {router.provider_count()} providers)\n\n"
            "Tap a key to test it."
        )
        _edit(chat_id, message_id, text, reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("keyinfo:"):
        # p:keyinfo:gemini:1
        _, provider, idx = action.split(":", 2)
        idx = int(idx)
        router = ai_engine.get_router()
        prov = next((p for p in router.providers if p.name == provider), None)
        if not prov:
            _edit(chat_id, message_id, "Provider nahi mila.", reply_markup=_back())
            _answer(cb_id)
            return True
        if idx - 1 < 0 or idx - 1 >= len(prov.pool.keys):
            _edit(chat_id, message_id, "Key nahi mili.", reply_markup=_back())
            _answer(cb_id)
            return True
        key = prov.pool.keys[idx - 1]
        try:
            text = ai_engine._gemini_call(key, prov.model, "Reply with exactly: ok", None, 256, 30)[0] if prov.name == "gemini" else ai_engine._deepseek_call(key, prov.model, "Reply with exactly: ok", None, 256, 30)[0]
            ok = bool(text) and "ok" in text.lower()
            _edit(chat_id, message_id, f"🧪 Test [{provider}] {ai_engine.mask_key(key)}\n\nResult: {('✅ ' + text) if ok else '⚠️ unexpected: ' + (text or '(empty)')}", reply_markup=_back())
        except Exception as exc:
            _edit(chat_id, message_id, f"❌ Fail: {str(exc)[:200]}", reply_markup=_back())
        _answer(cb_id)
        return True

    if action == "keytest_all":
        router = ai_engine.get_router()
        ok = 0
        bad = 0
        for prov in router.providers:
            for k in prov.pool.keys:
                try:
                    if prov.name == "gemini":
                        text, _ = ai_engine._gemini_call(k, prov.model, "Reply: ok", None, 256, 30)
                    else:
                        text, _ = ai_engine._deepseek_call(k, prov.model, "Reply: ok", None, 256, 30)
                    if text and "ok" in text.lower():
                        ok += 1
                    else:
                        bad += 1
                except Exception:
                    bad += 1
        _edit(chat_id, message_id, f"🧪 Test results\n✅ {ok} working\n❌ {bad} failing", reply_markup=_back())
        _answer(cb_id, f"{ok} ok, {bad} fail")
        return True

    if action == "keyreset":
        router = ai_engine.get_router()
        for prov in router.providers:
            prov.pool._backoff_until.clear()
        _edit(chat_id, message_id, "♻️ All key backoffs cleared.", reply_markup=_back())
        _answer(cb_id, "Reset ✅")
        return True

    # ---------- memory cleanup ----------

    if action == "memory":
        kb = {"inline_keyboard": [
            [{"text": "🧹 Prune now", "callback_data": "p:mprune"}],
            [{"text": "🔄 Reconcile channel", "callback_data": "p:mreconcile"}],
            [{"text": "🗑 Forget…", "callback_data": "p:mforget"}],
            [{"text": "⬅️ Back", "callback_data": "p:back"}],
        ]}
        stats = knowledge.stats()
        text = (
            "🧠 Memory Cleanup\n\n"
            f"Messages: {stats['messages']}\n"
            f"Channel posts: {stats['posts']}\n"
            f"Q&A: {stats['qa']}\n"
            f"Open escalations: {stats['escalations_open']}\n"
            f"Links: {stats['links']}\n\n"
            "Prune: old + low-importance data\n"
            "Reconcile: orphan channel posts\n"
            "Forget: regex se specific data hatao"
        )
        _edit(chat_id, message_id, text, reply_markup=kb)
        _answer(cb_id)
        return True

    if action == "mprune":
        counts = knowledge.prune_once()
        _edit(chat_id, message_id, f"🧹 Pruned: {counts}", reply_markup=_back())
        _answer(cb_id, "Prune done")
        return True

    if action == "mreconcile":
        r = knowledge.maybe_reconcile.__wrapped__() if hasattr(knowledge.maybe_reconcile, "__wrapped__") else _reconcile_now()
        _edit(chat_id, message_id, f"🔄 Reconcile: {r}", reply_markup=_back())
        _answer(cb_id)
        return True

    if action == "mforget":
        save_state(f"{from_uid}:forget_mode", True)
        _edit(chat_id, message_id, "🗑 Bhulane ke liye query DM me bhejo. (Cancel: /cancel)", reply_markup=_back())
        _answer(cb_id)
        return True

    # ---------- stats ----------

    if action == "stats":
        s = knowledge.stats()
        gid = links.get_group_id()
        cid = links.get_channel_id()
        router = ai_engine.get_router()
        model = get_state("model", config.DEFAULT_MODEL)
        text = (
            "📊 Stats\n\n"
            f"Group:  {gid or '—'}\n"
            f"Channel: {cid or '—'}\n"
            f"Model:  {model}\n"
            f"AI providers: {router.provider_count()} ({router.key_count()} keys)\n\n"
            f"Messages: {s['messages']}\n"
            f"Channel posts: {s['posts']}\n"
            f"Q&A pairs: {s['qa']}\n"
            f"Open escalations: {s['escalations_open']}\n"
            f"Links: {s['links']}"
        )
        _edit(chat_id, message_id, text, reply_markup=_back())
        _answer(cb_id)
        return True

    # ---------- help ----------

    if action == "help":
        text = (
            "❓ Help\n\n"
            "• Owner DM me /start → control panel\n"
            "• Group me: /ask <sawal> ya @botname mention karo\n"
            "• Channel: bot admin banao, posts auto-learn\n"
            "• File maango: 'apk bhej' / 'send file' likho\n"
            "• Escalation: admin pehle, owner last resort\n"
            "• Cleanup: panel > 🧠 Memory Cleanup\n"
            "• Limitation: purane messages bot nahi padh sakta (add hone ke din se seekhega)\n"
        )
        _edit(chat_id, message_id, text, reply_markup=_back())
        _answer(cb_id)
        return True

    # ---------- back ----------

    if action == "back":
        _edit(chat_id, message_id, PANEL_TEXT, reply_markup=panel_keyboard())
        _answer(cb_id)
        return True

    _answer(cb_id, "Pata nahi ye button.")
    return True


# ---------- helpers ----------

def _addgroup_url() -> str:
    me = tg_api.get_me() or {}
    uname = me.get("username") or ""
    if not uname:
        return "https://t.me/"
    return f"https://t.me/{uname}?startgroup=true"


def _addchannel_url() -> str:
    me = tg_api.get_me() or {}
    uname = me.get("username") or ""
    if not uname:
        return "https://t.me/"
    # channels have no startgroup; we link to bot profile and owner adds manually
    return f"https://t.me/{uname}"


def _reconcile_now() -> str:
    try:
        from db import get_state, save_state
        db = get_db()
        chans: set = set()
        for lnk in db["links"].find({"channel_id": {"$exists": True, "$ne": None}}):
            cid = lnk.get("channel_id")
            if isinstance(cid, int):
                chans.add(cid)
        cid = get_state("channel_id")
        if isinstance(cid, int):
            chans.add(cid)
        total = 0
        for c in chans:
            r = knowledge.reconcile_channel(c)
            total += r.get("removed", 0)
        save_state("last_reconcile_ts", __import__("time").time())
        return f"removed={total} across {len(chans)} channels"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc!r}"


def get_state(k: str, default=None):
    from db import get_state as _g
    return _g(k, default)


def save_state(k: str, v):
    from db import save_state as _s
    return _s(k, v)


def get_db():
    from db import get_db as _g
    return _g()


def handle_forget_mode_text(msg) -> bool:
    """If owner typed a forget query after clicking the button, consume it."""
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not _is_owner(uid):
        return False
    if not get_state(f"{uid}:forget_mode"):
        return False
    if text in ("/cancel", "/back"):
        save_state(f"{uid}:forget_mode", False)
        _send(chat_id, "Cancelled.")
        return True
    n = knowledge.forget_query(text)
    save_state(f"{uid}:forget_mode", False)
    _send(chat_id, f"🗑 {n} rows bhula diye for query: {text}")
    return True
