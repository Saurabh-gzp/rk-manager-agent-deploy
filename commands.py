"""Public utility commands: /help /id /stats /files /report.

Any group member can use these. They give the bot a proper Telegram-native
command menu (set via setMyCommands on startup) plus live info commands.
"""
from __future__ import annotations

import config
import links
import tg_api
from db import get_db
from moderation import _say_reply

_PUBLIC_COMMANDS = {"help", "id", "stats", "files", "report"}
_OWNER_COMMANDS = {"users"}

BOT_COMMANDS_PUBLIC = [
    {"command": "ask", "description": "Bot se kuch bhi pucho"},
    {"command": "help", "description": "Saare commands ki list"},
    {"command": "files", "description": "Channel ki files/media list"},
    {"command": "id", "description": "Chat aur user ID info"},
    {"command": "stats", "description": "Group stats"},
    {"command": "report", "description": "Message report karo (admin ko)"},
    {"command": "rules", "description": "Group rules dekho"},
    {"command": "admin", "description": "Admin/owner se baat karao"},
]

BOT_COMMANDS_ADMINS = [
    {"command": "mute", "description": "Mute karo (reply/naam/id)"},
    {"command": "tmute", "description": "Temp mute: /tmute 30m"},
    {"command": "unmute", "description": "Unmute karo (reply/naam/id)"},
    {"command": "warn", "description": "Warning do (3 par auto-mute)"},
    {"command": "warnings", "description": "Warnings count dekho"},
    {"command": "delete", "description": "Replied message delete karo"},
    {"command": "purge", "description": "Bulk delete (reply + count)"},
    {"command": "pin", "description": "Replied message pin karo"},
    {"command": "unpin", "description": "Pinned message unpin karo"},
    {"command": "filter", "description": "Word filter set karo"},
    {"command": "filters", "description": "Filters list dekho"},
    {"command": "modlog", "description": "Mod actions history"},
]

BOT_COMMANDS_OWNER = [
    {"command": "kick", "description": "Member ko group se nikalo"},
    {"command": "ban", "description": "Ban karo (owner only)"},
    {"command": "unban", "description": "Unban karo (owner only)"},
    {"command": "users", "description": "Users list — kaun kitna active"},
    {"command": "sikhao", "description": "Bot ko task sikhao (owner)"},
    {"command": "sikhai", "description": "Sikhaye hue tasks dekho"},
    {"command": "bhulo", "description": "Sikhaya task bhulo (owner)"},
    {"command": "yaad", "description": "Standing rule yaad rakho"},
    {"command": "bhul", "description": "Standing rule bhulo"},
]

HELP_TEXT = (
    "🤖 <b>RK MANAGER — Command Center</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "👥 <b>Sabke liye:</b>\n"
    "• /ask &lt;sawal&gt; — jo bhi puchna hai\n"
    "• /files — channel ki files list\n"
    "• /id — chat/user ID info\n"
    "• /stats — group stats\n"
    "• /report — kisi message pe reply karke report\n"
    "• /rules — group rules\n"
    "• /admin &lt;note&gt; — admin se baat\n\n"
    "🛡 <b>Mods/Owner:</b>\n"
    "• /mute /unmute — reply, ID, @username YA NAAM se\n"
    "• /tmute 30m|2h|1d — temp mute\n"
    "• /warn — 3 warn = 1hr auto-mute\n"
    "• /warnings / /resetwarns\n"
    "• /delete — replied msg delete\n"
    "• /purge — spam bulk-clean (reply + count)\n"
    "• /pin / /unpin — messages pin/unpin\n"
    "• /filter &lt;word&gt; [reply] — auto filter\n"
    "• /modlog — action history\n\n"
    "👑 <b>Owner only:</b>\n"
    "• /kick /ban /unban\n"
    "• /users — kaun kitna active hai (users list)\n"
    "• /sikhao — bot ko naya task sikhao\n"
    "• /yaad — hamesha yaad rakhne wali baat\n\n"
    "💬 Bot ko seedha bhi bolo — @mention ya reply karke.\n"
    "Owner DM me /start — full control panel."
)


def setup_bot_commands() -> None:
    """Register the Telegram command menu ('/' button UI). Best-effort."""
    try:
        tg_api.set_my_commands(BOT_COMMANDS_PUBLIC, scope="default")
        tg_api.set_my_commands(BOT_COMMANDS_ADMINS + BOT_COMMANDS_PUBLIC,
                               scope="all_group_admins")
    except Exception:
        pass


def handle_public_command(msg: dict, uid: int, chat_id: int) -> bool:
    """Dispatch public utility commands. True = handled."""
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower().lstrip("/")
    if cmd not in _PUBLIC_COMMANDS and cmd not in _OWNER_COMMANDS:
        return False
    args = " ".join(parts[1:]).strip()
    mid = msg.get("message_id")
    issuer = msg.get("from") or {}

    # ---- /users (owner only) — users list with activity ----
    if cmd == "users":
        if not (uid == config.OWNER_CHAT_ID
                or (config.ADMIN_CHAT_ID and uid == config.ADMIN_CHAT_ID)):
            _say_reply(chat_id, "❌ Ye sirf owner dekh sakta hai.", mid)
            return True
        lines = list_users()
        _say_reply(chat_id, "\n".join(lines), mid)
        return True

    # ---- /help ----
    if cmd == "help":
        r = tg_api.send_message(chat_id, HELP_TEXT, parse_mode="HTML")
        if not r.get("ok"):
            plain = (HELP_TEXT.replace("<b>", "").replace("</b>", "")
                     .replace("&lt;", "<").replace("&gt;", ">"))
            _say_reply(chat_id, plain, mid)
        return True

    # ---- /id ----
    if cmd == "id":
        replied = msg.get("reply_to_message") or {}
        target_user = (replied.get("from") or issuer) or {}
        lines = [f"🆔 <b>ID Info</b>",
                 f"Chat ID: <code>{chat_id}</code>",
                 f"Tumhara ID: <code>{issuer.get('id')}</code>",
                 f"Naam: {target_user.get('first_name') or '—'}"]
        uname = target_user.get("username")
        if uname:
            lines.append(f"Username: @{uname}")
        if replied.get("message_id"):
            lines.append(f"Reply target ID: <code>{target_user.get('id')}</code>")
            lines.append(f"Message ID: <code>{replied.get('message_id')}</code>")
        if links.get_channel_id():
            lines.append(f"Channel ID: <code>{links.get_channel_id()}</code>")
        tg_api.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        return True

    # ---- /stats ----
    if cmd == "stats":
        count = tg_api.get_chat_member_count(chat_id)
        try:
            messages = get_db()["messages"].count_documents(
                {"chat_id": chat_id}) or 0
            files = get_db()["channel_posts"].count_documents(
                {"file_id": {"$ne": None}}) or 0
            top = list(get_db()["messages"].aggregate([
                {"$match": {"chat_id": chat_id}},
                {"$group": {"_id": "$user_id",
                            "n": {"$sum": 1},
                            "name": {"$last": "$first_name"}}},
                {"$sort": {"n": -1}},
                {"$limit": 5},
            ]))
        except Exception:
            messages, files, top = 0, 0, []
        lines = ["📊 <b>Group Stats</b>",
                 f"Members: {count or '—'}",
                 f"Bot ki memory me messages: {messages}",
                 f"Channel files: {files}"]
        if top:
            lines.append("\n🏆 Top talkers:")
            for i, row in enumerate(top, 1):
                lines.append(f"{i}. {row.get('name') or row.get('_id')} — {row.get('n')}")
        tg_api.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        return True

    # ---- /files [query] ----
    if cmd == "files":
        items = list_channel_files(args)
        if not items:
            if args:
                _say_reply(chat_id, f"🔎 '{args}' se koi channel file match "
                                    "nahi hui. /files se puri list dekho.",
                           mid)
            else:
                _say_reply(chat_id, "📭 Channel me abhi koi file save nahi "
                                    "hai. Owner channel me file daalega to "
                                    "yahan dikhegi (channel link hone chahiye).",
                           mid)
            return True
        lines = ["📁 <b>Channel files:</b>"]
        for it in items[:15]:
            emoji = {"photo": "🖼", "video": "🎬", "audio": "🎵",
                     "voice": "🎤", "contact": "👤"}.get(it["type"], "📄")
            lines.append(f"{emoji} {it['label']}")
        if len(items) > 15:
            lines.append(f"…aur {len(items) - 15} files. Naam bolke maango "
                         "— 'mod config wali file bhejo' jaisa.")
        lines.append("\n💡 Kisi file ke liye seedha bolo ya /ask use karo.")
        tg_api.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        return True

    # ---- /report (reply-based) ----
    if cmd == "report":
        replied = msg.get("reply_to_message") or {}
        if not replied.get("message_id"):
            _say_reply(chat_id, "Report karne wale message pe reply karke "
                                "/report bhejo.", mid)
            return True
        reporter = issuer.get("first_name") or "Member"
        target = (replied.get("from") or {}).get("first_name") or "kisi ne"
        snippet = (replied.get("text") or "[media]")[:80]
        note = args[:200]
        text = (f"🚨 <b>Report</b>\n"
                f"{reporter} ne {target} ke message ko report kiya.\n"
                f"Message: <i>{snippet}</i>\n"
                f"Message ID: <code>{replied.get('message_id')}</code>")
        if note:
            text += f"\nNote: <i>{note}</i>"
        # Owner/admin ko DM me notify karo (best-effort).
        admin_id = config.ADMIN_CHAT_ID or config.OWNER_CHAT_ID
        try:
            tg_api.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass
        _say_reply(chat_id, "✅ Report admin ko bhej diya. Jaldi dekha "
                            "jayega.", mid)
        return True

    return False


def list_users() -> list[str]:
    """Owner ke liye users list: naam, username, msg count, last-seen."""
    import time as _time

    try:
        docs = list(get_db()["messages"].find({}))
    except Exception:
        docs = []
    users: dict[int, dict] = {}
    for d in docs:
        uid = d.get("user_id")
        if uid is None:
            continue
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        row = users.setdefault(uid, {
            "name": d.get("first_name") or "",
            "username": d.get("username") or "",
            "n": 0, "last": 0.0,
        })
        row["n"] += 1
        row["last"] = max(row["last"], float(d.get("date") or 0))
        if not row["name"]:
            row["name"] = d.get("first_name") or ""
        if not row["username"]:
            row["username"] = d.get("username") or ""
    if not users:
        return ["👥 Abhi koi user memory me nahi hai."]
    now = _time.time()
    rows = sorted(users.items(), key=lambda kv: kv[1]["last"] or 0,
                  reverse=True)
    lines = [f"👥 Users list — {len(rows)} log yaad hain "
             "(memory active):"]
    for uid, row in rows[:20]:
        name = row["name"] or ("@" + row["username"] if row["username"]
                               else f"user {uid}")
        uname = f" (@{row['username']})" if row["username"] else ""
        days = int((now - (row["last"] or now)) / 86400)
        when = ("abhi active" if days == 0
                else f"{days} din pehle")
        lines.append(f"• {name}{uname} — {row['n']} msgs, {when}")
    if len(rows) > 20:
        lines.append(f"…aur {len(rows) - 20} log.")
    lines.append("\n7+ din se inactive user ki memory auto-delete hoti "
                 "hai (owner ko notice jata hai).")
    return lines


def list_channel_files(query: str | None = None) -> list[dict]:
    """List stored channel files, optionally filtered by a query."""
    try:
        posts = get_db()["channel_posts"]
        flt = {"file_id": {"$ne": None}}
        if query:
            import re as _re
            rgx = {"$regex": _re.escape(query), "$options": "i"}
            flt = {"$or": [{"text": rgx, "file_id": {"$ne": None}},
                           {"caption": rgx, "file_id": {"$ne": None}}]}
        docs = list(posts.find(flt))
    except Exception:
        return []
    docs.sort(key=lambda d: d.get("date") or 0, reverse=True)
    items = []
    for d in docs:
        label = (d.get("caption") or d.get("text") or "").strip()
        if len(label) > 40:
            label = label[:37] + "..."
        if not label:
            label = f"file (msg {d.get('message_id')})"
        items.append({"label": label, "type": d.get("file_type") or "document",
                      "file_id": d.get("file_id"),
                      "message_id": d.get("message_id"),
                      "channel_id": d.get("channel_id")})
    return items
