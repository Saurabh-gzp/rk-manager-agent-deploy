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
import moderation
import tg_api
import rules


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
    "Yahan se group/channel manage karo, AI provider/model control karo,\n"
    "ya memory cleanup chala do. Sab kuch buttons se."
)


def _ai_status_line() -> str:
    """Panel ke niche current text/vision AI selection dikhata hai."""
    try:
        router = ai_engine.get_router()
        tp, tm = router.get_text_selection()
        tp_label = ai_engine.PROVIDER_LABELS.get(tp, tp)
        line = f"🤖 Text: {tp_label} · {tm}"
        try:
            vp, vm = router.get_vision_selection()
            vp_label = ai_engine.PROVIDER_LABELS.get(vp, vp)
            line += f"\n👁 Vision (photo/audio): {vp_label} · {vm}"
        except Exception:
            line += "\n👁 Vision: koi provider nahi (Gemini key add karo)"
        return line
    except Exception:
        return "🤖 Text: koi AI provider nahi\n👁 Vision: koi AI provider nahi"


def panel_keyboard() -> dict:
    rows = [
        [{"text": "➕ Group Admin", "callback_data": "p:group"},
         {"text": "➕ Channel Admin", "callback_data": "p:channel"}],
        [{"text": "🔗 Link Group ↔ Channel", "callback_data": "p:link"}],
        [{"text": "🤖 Text AI", "callback_data": "p:ai_text"},
         {"text": "👁 Vision AI", "callback_data": "p:ai_vision"}],
        [{"text": "🔑 API Keys", "callback_data": "p:keys"},
         {"text": "🧠 Memory Cleanup", "callback_data": "p:memory"}],
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
    _send(chat_id, PANEL_TEXT + "\n\n" + _ai_status_line(),
          reply_markup=panel_keyboard())
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


def handle_owner_command(msg) -> bool:
    """Handle moderation and runtime commands from the owner's private DM only."""
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
    known = {
        "/mod_add", "/mod_remove", "/mod_list", "/mod_on", "/mod_off",
        "/mute", "/status", "/yaad", "/rules", "/bhul", "/users",
    }
    if command not in known:
        return False
    if not _is_owner(uid):
        _send(chat_id, "Ye command sirf owner chala sakta hai.")
        return True

    argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if command == "/mod_list":
        terms = moderation.get_terms()
        joined = ", ".join(terms) if terms else "koi moderation term set nahi hai"
        _send(chat_id, f"Moderation: {'ON' if moderation.is_enabled() else 'OFF'}\nTerms: {joined}")
        return True
    if command == "/mod_add":
        term = argument[:64].strip()
        if not term:
            _send(chat_id, "Usage: /mod_add <word or phrase>")
            return True
        terms = moderation.get_terms()
        normalized = term.casefold()
        if normalized not in terms:
            terms.append(term)
            moderation.set_terms(terms)
        _send(chat_id, f"✅ Moderation term add kiya: {term}")
        return True
    if command == "/mod_remove":
        term = argument[:64].strip().casefold()
        if not term:
            _send(chat_id, "Usage: /mod_remove <word or phrase>")
            return True
        moderation.set_terms([item for item in moderation.get_terms() if item != term])
        _send(chat_id, f"✅ Moderation term remove kiya: {term}")
        return True
    if command in ("/mod_on", "/mod_off"):
        enabled = command == "/mod_on"
        moderation.set_enabled(enabled)
        _send(chat_id, f"✅ Automatic moderation {'ON' if enabled else 'OFF'} hai. Ban action kabhi nahi hota.")
        return True
    if command == "/mute":
        parts = argument.split()
        if len(parts) not in (1, 2):
            _send(chat_id, "Usage: /mute <telegram_user_id> [minutes, 1-1440]")
            return True
        try:
            target_id = int(parts[0])
            minutes = int(parts[1]) if len(parts) == 2 else 10
        except ValueError:
            _send(chat_id, "User ID aur duration numeric hone chahiye.")
            return True
        group_id = links.get_group_id()
        if not group_id:
            _send(chat_id, "Managed group set nahi hai.")
            return True
        result = moderation.manual_mute(group_id, target_id, minutes)
        if result.get("ok"):
            _send(chat_id, f"⏱ User {target_id} ko {minutes} minute mute kiya. Ban nahi kiya.")
        else:
            _send(chat_id, f"Mute apply nahi hua: {result.get('description', 'Telegram API error')}")
        return True
    if command == "/users":
        import commands as _commands
        lines = _commands.list_users()
        _send(chat_id, "\n".join(lines))
        return True
    if command == "/status":
        from db import is_persistent
        store = "MongoDB" if is_persistent() else "temporary in-memory (restart par data lost ho sakta hai)"
        # v4.2: MTProto realtime layer status (owner ka naya power hub)
        try:
            import mtproto_layer
            mt = mtproto_layer.status()
            if not mt.get("enabled"):
                mt_line = "Realtime (MTProto): OFF — API ID/Hash set nahi"
            elif mt.get("connected"):
                mt_line = ("Realtime (MTProto): 🟢 LIVE — deletes/edits "
                           f"turant sync, {mt.get('events_seen', 0)} events")
            else:
                mt_line = ("Realtime (MTProto): 🟡 connecting..."
                           + (f" ({mt.get('last_error', '')[:60]})" if mt.get("last_error") else ""))
        except Exception:
            mt_line = "Realtime (MTProto): n/a"
        try:
            from db import get_state
            last_sync = get_state("last_mt_sync") or {}
            if last_sync:
                mt_line += (f"\nLast live-sync: +{last_sync.get('added', 0)} new, "
                            f"-{last_sync.get('purged', 0)} deleted verify")
        except Exception:
            pass
        # v5.1 REAL AGENT: tools + radar (group + channel) ka live snapshot
        try:
            import powers as _powers
            import agent_tools as _agent_tools
            import bot as _bot_mod
            ps = _powers.powers_status()
            ts = _agent_tools.stats()
            radar_ids = getattr(_bot_mod, "_RADAR_LAST", {})
            ch_ids = getattr(_bot_mod, "_CH_RADAR_LAST", {})
            radar_line = (f"Radar: 🟢 group (last id {max(radar_ids.values()) if radar_ids else '?'})"
                          if radar_ids else "Radar: ⏳ seed wait (pehla message aane par ON)")
            if ch_ids:
                radar_line += f" · 📢 channel (last id {max(ch_ids.values())})"
            power_line = (
                f"{radar_line}\n"
                f"Watchdog: {'🟢 ON' if getattr(__import__('config'), 'WATCHDOG_ENABLED', 1) else '🔴 OFF'} "
                f"({ps.get('pending_reminders', 0)} due-checks/min)\n"
                f"Welcome: {'ON' if ps.get('welcome') else 'OFF'} · "
                f"Reactions: {'ON' if ps.get('reactions') else 'OFF'} · "
                f"Flood-guard: {'ON' if ps.get('flood_guard') else 'OFF'}\n"
                f"Member cards: {ps.get('member_cards', 0)} profiles · "
                f"Reminders pending: {ps.get('pending_reminders', 0)}\n"
                f"🛠 Live tool calls: {ts.get('auto_calls', 0)} auto · "
                f"{ts.get('fc_calls', 0)} FC · {ts.get('calls', 0)} manual "
                f"· {ts.get('rescued', 0)} rescued"
            )
        except Exception:
            power_line = "v5.1 agent powers: n/a"
        _send(
            chat_id,
            f"Runtime status\nStorage: {store}\n"
            f"{mt_line}\n"
            f"{power_line}\n"
            f"Group ID: {links.get_group_id() or 'not set'}\n"
            f"Channel ID: {links.get_channel_id() or 'not set'}\n"
            f"Moderation: {'ON' if moderation.is_enabled() else 'OFF'} "
            f"({len(moderation.get_terms())} terms)\n"
            f"Standing rules: {len(rules.get_rules())}",
        )
        return True
    if command == "/yaad":
        if not argument:
            _send(chat_id, "Usage: /yaad <hamesha yaad rakhne wali baat>")
            return True
        idx = rules.add_rule(argument, uid)
        if idx == -1:
            _send(chat_id, "Ye rule pehle se noted hai.")
        elif idx == 0:
            _send(chat_id, "Rule invalid hai.")
        else:
            _send(chat_id, f"✅ Yaad rakh liya (rule #{idx}). Ab se hamesha follow karunga.")
        return True
    if command == "/rules":
        all_rules = rules.get_rules()
        if not all_rules:
            _send(chat_id, "Koi standing rule noted nahi hai.")
        else:
            lines = ["📋 Standing rules:"]
            lines.extend(f"{i}. {r['rule']}" for i, r in enumerate(all_rules, 1))
            _send(chat_id, "\n".join(lines))
        return True
    if command == "/bhul":
        try:
            n = int(argument)
        except ValueError:
            _send(chat_id, "Usage: /bhul <rule number>")
            return True
        removed = rules.remove_rule(n)
        _send(chat_id, f"🗑 Rule #{n} bhula diya: {removed}" if removed else f"Rule #{n} mila nahi.")
        return True
    return False


def _back():
    return {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "p:back"}]]}


# ---------- callbacks ----------

def _show_channel(chat_id: int, message_id: int, cb_id: str) -> None:
    """Render the Channel Admin screen.

    The channel is fully optional — if the owner skips it, the setup nag
    disappears and the bot works group-only.
    """
    cid = links.get_channel_id()
    title = get_state("channel_title") or "-"
    if cid and get_state("channel_skipped", False):
        save_state("channel_skipped", False)  # self-heal once a channel exists
    skipped = bool(get_state("channel_skipped", False))
    kb_buttons = [
        [{"text": "Refresh", "callback_data": "p:channel"}],
    ]
    if not cid:
        if skipped:
            text = (
                "Channel Admin\n\n"
                "Channel skip kiya hua hai — bot sirf group me kaam karega, "
                "ab channel setup nahi mangega. 👍"
            )
            kb_buttons.append(
                [{"text": "↩️ Channel enable karo", "callback_data": "p:channel_unskip"}])
        else:
            text = (
                "Channel Admin\n\n"
                "Abhi koi channel set nahi. (Channel optional hai — sirf group bhi chalega.)\n\n"
                "Setup ke 2 tarike:\n"
                "1) Channel kholo > Administrators > Add > @rkgamermanagerbot search, Post messages permission de do\n"
                "2) Set by Forward — kisi bhi channel se ek message yahan DM me forward karo, bot uska chat ID use karega"
            )
            kb_buttons.append(
                [{"text": "Set by Forward", "callback_data": "p:channel_fwd"}])
            kb_buttons.append(
                [{"text": "🔕 Channel nahi chahiye", "callback_data": "p:channel_skip"}])
    else:
        text = (
            f"Channel Admin\n\n"
            f"Channel ID: {cid}\n"
            f"Title: {title}\n"
            f"Subscribers: {tg_api.get_chat_member_count(cid)}"
        )
        kb_buttons.append(
            [{"text": "Set by Forward", "callback_data": "p:channel_fwd"}])
        kb_buttons.append(
            [{"text": "Clear", "callback_data": "p:channel_clear"}])
        kb_buttons.append(
            [{"text": "Leave channel", "callback_data": "p:channel_leave"}])
    kb_buttons.append([{"text": "Back", "callback_data": "p:back"}])
    _edit(chat_id, message_id, text,
          reply_markup={"inline_keyboard": kb_buttons})
    _answer(cb_id)


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
        title = get_state("group_title") or "-"
        kb = {"inline_keyboard": [
            [{"text": "Refresh", "callback_data": "p:group"}],
            [{"text": "Set by Forward", "callback_data": "p:group_fwd"}],
            [{"text": "Clear", "callback_data": "p:group_clear"}],
            [{"text": "Leave group", "callback_data": "p:group_leave"}],
            [{"text": "Back", "callback_data": "p:back"}],
        ]}
        if not gid:
            text = (
                "Group Admin\n\n"
                "Abhi koi group set nahi hai.\n\n"
                "Setup ke 2 tarike:\n"
                "1) Add bot to group - niche button dabao, group me add karo (admin banao), bot auto-detect karega\n"
                "2) Set by Forward - kisi bhi group se ek message yahan DM me forward karo, bot uska chat ID use karega"
            )
            kb["inline_keyboard"].insert(0, [
                {"text": "Add bot to group", "url": _addgroup_url()}
            ])
        else:
            text = (
                f"Group Admin\n\n"
                f"Group ID: {gid}\n"
                f"Title: {title}\n"
                f"Members: {tg_api.get_chat_member_count(gid)}"
            )
        _edit(chat_id, message_id, text, reply_markup=kb)
        _answer(cb_id)
        return True

    if action == "group_fwd":
        save_state(f"{from_uid}:group_fwd_mode", time.time())
        _edit(chat_id, message_id,
              "Set Group by Forward\n\n"
              "Apne target group se koi bhi message yahan DM me forward karo.\n"
              "Bot us group ka chat ID pakad ke set kar dega.\n\n"
              "Cancel: /cancel",
              reply_markup=_back())
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
        _show_channel(chat_id, message_id, cb_id)
        return True

    if action == "channel_skip":
        save_state("channel_skipped", True)
        _answer(cb_id, "Channel skip kiya 👍")
        _show_channel(chat_id, message_id, cb_id)
        return True

    if action == "channel_unskip":
        save_state("channel_skipped", False)
        _answer(cb_id)
        _show_channel(chat_id, message_id, cb_id)
        return True

    if action == "channel_fwd":
        save_state(f"{from_uid}:channel_fwd_mode", time.time())
        _edit(chat_id, message_id,
              "Set Channel by Forward\n\n"
              "Apne target channel se koi bhi message yahan DM me forward karo.\n"
              "Bot us channel ka chat ID pakad ke set kar dega.\n\n"
              "Cancel: /cancel",
              reply_markup=_back())
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
        text = "Link Group <-> Channel\n\n"
        if gid and cid:
            text += f"Linked:\nGroup: {gid}\nChannel: {cid}"
        else:
            text += (
                f"Group: {gid if gid else 'not set'}\n"
                f"Channel: {cid if cid else 'not set'}\n\n"
                "Dono set karne ke baad auto-link ho jayega."
            )
        _edit(chat_id, message_id, text, reply_markup=_back())
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
        me = tg_api.get_me() or {}
        uname = me.get("username") or "rkgamermanagerbot"
        kb = {"inline_keyboard": [
            [{"text": "👤 Bot ki profile kholo (@{})".format(uname),
              "url": f"https://t.me/{uname}"}],
            [{"text": "⬅️ Back", "callback_data": "p:back"}],
        ]}
        text = (
            "➕ Add to channel\n\n"
            "⚠️ Telegram channels me bots ko LINK se add NAHI kar sakte — "
            "ye Telegram ki limit hai (groups me hota hai, channels me nahi).\n\n"
            "Isliye manually add karo (2 minute ka kaam):\n\n"
            "1️⃣ Apna channel kholo\n"
            "2️⃣ Channel ka naam → ✏️ Edit / Settings\n"
            "3️⃣ Administrators → Add Admin\n"
            "4️⃣ Search me likho: @{}\n"
            "5️⃣ Add karo — sirf ek permission chahiye:\n"
            "    • Post Messages ✅ (baaki sab OFF rakh sakte ho)\n\n"
            "✅ Add hone par bot khud channel ko auto-set kar dega aur "
            "tumhe DM me confirm karega.".format(uname)
        )
        _edit(chat_id, message_id, text, reply_markup=kb)
        _answer(cb_id)
        return True

    # ---------- v4.1 AI selection: text provider/model ----------

    if action == "ai_text":
        router = ai_engine.get_router()
        avail = router.text_providers()
        if not avail:
            _edit(chat_id, message_id,
                  "🤖 Koi AI provider configured nahi.\n"
                  "GEMINI_KEYS / MERCURY_KEYS env var set karo.",
                  reply_markup=_back())
            _answer(cb_id)
            return True
        try:
            cur_prov, cur_model = router.get_text_selection()
        except Exception:
            cur_prov, cur_model = "-", "-"
        rows = []
        for name in avail:
            label = ai_engine.PROVIDER_LABELS.get(name, name)
            mark = "✅" if name == cur_prov else "⬜"
            note = " \u2022 text only" if name != "gemini" else " \u2022 text+vision"
            rows.append([{"text": f"{mark} {label}{note}",
                          "callback_data": f"p:ai_textp:{name}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
        text = (
            "🤖 Text AI — provider choose karo\n\n"
            "Ye provider group/chat ke TEXT messages ka jawab dega.\n"
            f"Current: {ai_engine.PROVIDER_LABELS.get(cur_prov, cur_prov)} · {cur_model}\n\n"
            "ℹ️ Agar selected provider fail ho to bot khud dusre\n"
            "providers par failover kar deta hai — bot kabhi down nahi hota."
        )
        _edit(chat_id, message_id, text, reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("ai_textp:"):
        prov = action[len("ai_textp:"):]
        router = ai_engine.get_router()
        if not router.prov(prov):
            _edit(chat_id, message_id, "Ye provider available nahi.",
                  reply_markup=_back())
            _answer(cb_id)
            return True
        models = ai_engine.list_models_for(prov)
        if not models:
            models = [router.prov(prov).model]
        current = ai_engine.get_text_model(prov)
        rows = []
        for m in models[:25]:
            label = f"{'✅' if m == current else '⬜'} {m}"
            rows.append([{"text": label, "callback_data": f"p:ai_textm:{prov}:{m}"}])
        rows.append([{"text": "⬅️ Providers", "callback_data": "p:ai_text"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
        label = ai_engine.PROVIDER_LABELS.get(prov, prov)
        hint = ("\nℹ️ Mercury text-only hai — photo/audio wale messages\n"
                "automatically Vision AI provider se answer honge." if prov != "gemini"
                else "")
        _edit(chat_id, message_id,
              f"🤖 Text AI — {label} ka model choose karo\nCurrent: {current}{hint}",
              reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("ai_textm:"):
        # p:ai_textm:<provider>:<model>
        rest = action[len("ai_textm:"):]
        prov, _, model = rest.partition(":")
        model = model.strip() or rest
        router = ai_engine.get_router()
        if not router.prov(prov):
            _edit(chat_id, message_id, "Ye provider available nahi.",
                  reply_markup=_back())
            _answer(cb_id)
            return True
        ai_engine.set_text_model(prov, model)
        ai_engine.set_text_provider(prov)
        router.prov(prov).model = model
        label = ai_engine.PROVIDER_LABELS.get(prov, prov)
        _edit(chat_id, message_id,
              f"✅ Text AI set ho gaya:\n{label} · {model}\n\n{_ai_status_line()}",
              reply_markup=_back())
        _answer(cb_id, f"Text AI: {model} ✅")
        return True

    # ---------- v4.1 AI selection: vision provider/model ----------

    if action == "ai_vision":
        router = ai_engine.get_router()
        avail = router.vision_providers()
        if not avail:
            _edit(chat_id, message_id,
                  "👁 Vision ke liye koi provider nahi.\n"
                  "Photo/voice/video ke jawab ke liye Gemini key chahiye\n"
                  "(Mercury text-only hai). GEMINI_KEYS env var set karo.",
                  reply_markup=_back())
            _answer(cb_id)
            return True
        try:
            cur_prov, cur_model = router.get_vision_selection()
        except Exception:
            cur_prov, cur_model = "-", "-"
        if len(avail) == 1:
            # sirf ek vision provider — seedha model list dikhao
            prov = avail[0]
            models = ai_engine.list_models_for(prov)
            current = cur_model
            rows = []
            for m in models[:25]:
                label = f"{'✅' if m == current else '⬜'} {m}"
                rows.append([{"text": label,
                              "callback_data": f"p:ai_vism:{prov}:{m}"}])
            rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
            _edit(chat_id, message_id,
                  f"👁 Vision AI — {ai_engine.PROVIDER_LABELS.get(prov, prov)}\n"
                  f"Photo/audio/video messages is model se answer honge.\n"
                  f"Current: {current}",
                  reply_markup={"inline_keyboard": rows})
            _answer(cb_id)
            return True
        rows = []
        for name in avail:
            label = ai_engine.PROVIDER_LABELS.get(name, name)
            mark = "✅" if name == cur_prov else "⬜"
            rows.append([{"text": f"{mark} {label}",
                          "callback_data": f"p:ai_visp:{name}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
        _edit(chat_id, message_id,
              "👁 Vision AI — provider choose karo\n"
              "Photo/audio/video messages isi se answer honge.\n"
              f"Current: {ai_engine.PROVIDER_LABELS.get(cur_prov, cur_prov)} · {cur_model}",
              reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("ai_visp:"):
        prov = action[len("ai_visp:"):]
        router = ai_engine.get_router()
        if prov not in router.vision_providers():
            _edit(chat_id, message_id, "Ye vision provider available nahi.",
                  reply_markup=_back())
            _answer(cb_id)
            return True
        models = ai_engine.list_models_for(prov)
        _, cur_model = router.get_vision_selection()
        rows = []
        for m in models[:25]:
            label = f"{'✅' if m == cur_model else '⬜'} {m}"
            rows.append([{"text": label, "callback_data": f"p:ai_vism:{prov}:{m}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:ai_vision"}])
        _edit(chat_id, message_id,
              f"👁 Vision AI — {ai_engine.PROVIDER_LABELS.get(prov, prov)} ka model choose karo\n"
              f"Current: {cur_model}",
              reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("ai_vism:"):
        # p:ai_vism:<provider>:<model>
        rest = action[len("ai_vism:"):]
        prov, _, model = rest.partition(":")
        model = model.strip() or rest
        router = ai_engine.get_router()
        if prov not in router.vision_providers():
            _edit(chat_id, message_id, "Ye vision provider available nahi.",
                  reply_markup=_back())
            _answer(cb_id)
            return True
        ai_engine.set_vision_selection(prov, model)
        _edit(chat_id, message_id,
              f"✅ Vision AI set ho gaya:\n{ai_engine.PROVIDER_LABELS.get(prov, prov)} · {model}\n\n{_ai_status_line()}",
              reply_markup=_back())
        _answer(cb_id, f"Vision AI: {model} ✅")
        return True

    # ---------- model chooser (legacy panel buttons) ----------

    if action == "model":
        # purana 'Choose Model' button — Gemini chat model chunta tha;
        # v4.1 me Gemini ka model = gemini TEXT model. Provider-wise list
        # dikhate hain taaki purane panel messages bhi kaam karein.
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
        current = ai_engine.get_text_model("gemini")
        rows = []
        for m in models[:30]:
            label = f"{'✅ ' if m == current else '   '}{m}"
            rows.append([{"text": label, "callback_data": f"p:mset:{m}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "p:back"}])
        _edit(chat_id, message_id, f"🤖 Gemini model choose karo (current: {current})\n\n💡 Naya AI selector: '🤖 Text AI' / '👁 Vision AI' buttons use karo.", reply_markup={"inline_keyboard": rows})
        _answer(cb_id)
        return True

    if action.startswith("mset:"):
        m = action[len("mset:"):]
        # v4.1: gemini ka text model + vision model dono sync (legacy
        # single-model behavior)
        ai_engine.set_text_model("gemini", m)
        ai_engine.set_vision_selection("gemini", m)
        router = ai_engine.get_router()
        gp = router.prov("gemini")
        if gp:
            gp.model = m
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
            text, err = ai_engine.test_key(provider, key, prov.current_model())
            ok = bool(text) and "ok" in text.lower()
            result = ("✅ " + text) if ok else ("⚠️ unexpected: " + (text or f"fail: {err[:150]}"))
            _edit(chat_id, message_id, f"🧪 Test [{provider}] {ai_engine.mask_key(key)}\n\nResult: {result}", reply_markup=_back())
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
                    text, err = ai_engine.test_key(prov.name, k, prov.current_model())
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
        save_state(f"{from_uid}:forget_mode", time.time())
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
            f"Storage: {'MongoDB' if __import__('db').is_persistent() else 'RAM fallback (volatile)'}\n"
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
            "❓ Help — RK Manager v2\n\n"
            "⚡ NAYA (power upgrade):\n"
            "• Bot ke jawab ab hamesha tumhare message ka REPLY hote hain\n"
            "• Gaali catch: spelling-bypass bhi pakda jata hai (madharchood/bh0sda)\n"
            "• 'unmute rich ko' — naam se bhi action hota hai (reply/tag/naam)\n"
            "• Bot action kabhi fake claim nahi karta — real result hi bolta hai\n\n"
            "🛡 Group commands (owner/admin):\n"
            "• /mute /unmute /tmute 30m|2h|1d — reply, ID, @username YA naam se\n"
            "• /warn /warnings /resetwarns — 3 warn par 1hr auto-mute\n"
            "• /delete — replied msg; /purge — spam bulk-clean (reply + count)\n"
            "• /pin /unpin — message pin; /kick — member bahar (owner)\n"
            "• /ban /unban (owner only); /filter (Rose-style: reply karke "
            "content save — word bole to as-is milega) /filters /stop /modlog\n\n"
            "👥 Group commands (sab ke liye):\n"
            "• /help /id /stats /files /report /rules /ask /admin\n\n"
            "🧠 Owner teaching system:\n"
            "• /yaad <baat> — hamesha yaad rakhne wali instruction\n"
            "• /sikhao <trigger> :: <reply> — user bole to bot wahi bolega\n"
            "• /sikhao <trigger> :: file <naam> — user bole to file mil jayegi\n"
            "• /sikhai — list; /bhulo <trigger> — bhul jao\n\n"
            "📢 Channel (bot khud KABHI post nahi karta):\n"
            "• Owner DM me /post <message> — tabhi channel me post hota hai\n"
            "• User file maange to channel se seedha mil jayegi (/files dekho)\n"
            "• Contact cards bhi forward ho sakte hain channel se\n\n"
            "⚙️ Owner DM: /mod_add /mod_remove /mod_list /mod_on /mod_off\n"
            "• /mute <user_id> [minutes]; /status; /yaad /rules /bhul\n"
            "• Cleanup: panel > 🧠 Memory Cleanup\n"
            "• Limitation: purane messages bot nahi padh sakta (add hone ke din se)"
        )
        _edit(chat_id, message_id, text, reply_markup=_back())
        _answer(cb_id)
        return True

    # ---------- back ----------

    if action == "back":
        _edit(chat_id, message_id,
              PANEL_TEXT + "\n\n" + _ai_status_line(),
              reply_markup=panel_keyboard())
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


def _forwarded_chat(msg) -> dict:
    """Support both legacy forward_from_chat and modern forward_origin."""
    legacy = msg.get("forward_from_chat") or {}
    if legacy.get("id"):
        return legacy
    origin = msg.get("forward_origin") or {}
    if origin.get("type") in ("channel", "chat"):
        return origin.get("chat") or {}
    return {}


def _bot_is_member(chat_id: int, require_admin: bool) -> bool:
    bot_id = (tg_api.get_me() or {}).get("id")
    if not bot_id:
        return False
    member = tg_api.get_chat_member(chat_id, bot_id)
    status = member.get("status")
    if require_admin:
        return status in ("administrator", "creator")
    return status in ("member", "administrator", "creator")


def get_state(k: str, default=None):
    from db import get_state as _g
    return _g(k, default)


def save_state(k: str, v):
    from db import save_state as _s
    return _s(k, v)


def get_db():
    from db import get_db as _g
    return _g()


# Input modes (forward-set, forget) auto-expire so the owner is never
# trapped in one forever.
MODE_TIMEOUT_S = 600


def _mode_on(uid, name: str) -> bool:
    """True if the owner entered the named mode recently.

    Stale modes are cleared automatically (old boolean True values count
    as expired).
    """
    key = f"{uid}:{name}"
    ts = get_state(key)
    if not ts:
        return False
    try:
        age = time.time() - float(ts)
    except (TypeError, ValueError):
        age = 0.0
    if age > MODE_TIMEOUT_S:
        save_state(key, False)
        return False
    return True


def handle_forget_mode_text(msg) -> bool:
    """If owner typed a forget query after clicking the button, consume it."""
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not _is_owner(uid):
        return False
    if not _mode_on(uid, "forget_mode"):
        return False
    if text in ("/cancel", "/back"):
        save_state(f"{uid}:forget_mode", False)
        _send(chat_id, "Cancelled.")
        return True
    n = knowledge.forget_query(text)
    save_state(f"{uid}:forget_mode", False)
    _send(chat_id, f"🗑 {n} rows bhula diye for query: {text}")
    return True


def handle_group_fwd(msg) -> bool:
    """Owner forwarded a message from a group -> save it as managed group."""
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    if not _is_owner(uid):
        return False
    if not _mode_on(uid, "group_fwd_mode"):
        return False
    if (msg.get("text") or "").strip() in ("/cancel", "/back"):
        save_state(f"{uid}:group_fwd_mode", False)
        _send(chat_id, "Cancelled.")
        return True
    origin = _forwarded_chat(msg)
    origin_id = origin.get("id")
    origin_type = origin.get("type")
    if not origin_id or origin_type not in ("group", "supergroup"):
        _send(chat_id,
              "Ye message group se forwarded nahi hai.\n"
              "Target group se forward karo (kisi member ka msg ya apna msg).\n\n"
              "Mode se bahar nikalne ke liye /cancel bhejo.")
        return True
    if not _bot_is_member(int(origin_id), require_admin=False):
        _send(chat_id, "Pehle bot ko target group mein add karo, phir us group ka message forward karke set karo.")
        return True
    title = origin.get("title") or origin.get("username") or "Group"
    prev = links.get_group_id()
    links.set_group(int(origin_id), title)
    if prev and prev != origin_id:
        try:
            tg_api.leave_chat(prev)
        except Exception:
            pass
    save_state(f"{uid}:group_fwd_mode", False)
    _send(chat_id,
          f"Group set!\n\nTitle: {title}\nID: {origin_id}\n\n"
          f"Ab main is group me kaam karunga. /start daba ke panel dekh lo.")
    return True


def handle_channel_fwd(msg) -> bool:
    """Owner forwarded a message from a channel -> save it as managed channel."""
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    if not _is_owner(uid):
        return False
    if not _mode_on(uid, "channel_fwd_mode"):
        return False
    if (msg.get("text") or "").strip() in ("/cancel", "/back"):
        save_state(f"{uid}:channel_fwd_mode", False)
        _send(chat_id, "Cancelled.")
        return True
    origin = _forwarded_chat(msg)
    origin_id = origin.get("id")
    origin_type = origin.get("type")
    if not origin_id or origin_type != "channel":
        _send(chat_id,
              "Ye message channel se forwarded nahi hai.\n"
              "Target channel se forward karo.\n\n"
              "Mode se bahar nikalne ke liye /cancel bhejo.")
        return True
    if not _bot_is_member(int(origin_id), require_admin=True):
        _send(chat_id, "Channel read karne ke liye pehle bot ko channel admin banao (Post Messages permission OFF rakhna), phir forward karke set karo.")
        return True
    title = origin.get("title") or origin.get("username") or "Channel"
    prev = links.get_channel_id()
    links.set_channel(int(origin_id), title)
    if prev and prev != origin_id:
        try:
            tg_api.leave_chat(prev)
        except Exception:
            pass
    save_state(f"{uid}:channel_fwd_mode", False)
    _send(chat_id,
          f"Channel set!\n\nTitle: {title}\nID: {origin_id}\n\n"
          "Main channel se read karunga, koi post/message nahi bhejunga.")
    return True
