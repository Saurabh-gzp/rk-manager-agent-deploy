"""agent_tools.py — v5.0 MAX POWER AGENT: REAL tool-use loop.

Owner ka brief: 'agent ko sb kuch access do aur sabhi tools use kar ke
ek real agent ki tarah respond kare'. v4.2 tak model ke paas sirf EK
round ka LOOKUP: tha (deep search). v5.0 ab ek asli TOOL LOOP hai —
model jitna poochh sake live Telegram/Mongo se nikaal sakta hai, aur
har tool ka result REAL data hota hai (koi imagination nahi).

Protocol (prompt-side):
    Model jawab ki jagah likhta hai:  TOOL: <NAME> <args>
    System tool chalaata hai, result waapas feed karta hai, model se
    dobara poochta hai. Max TOOL_MAX_ROUNDS rounds (default 3), uske
    baad final answer dena mandatoriy hai. Purana LOOKUP: alias
    TOOL: SEARCH me map hota hai (backward compatible).

Tools (sab LIVE data, sab graceful — layer down ho to TOOL_ERROR):
    SEARCH <query>    -> knowledge.search deep (channel + group + Q&A)
    GETMSG <id>       -> MTProto se current chat ka ek message by ID
    RECENT <n>        -> MTProto live recent n messages (max 30)
    USER <name/@uid>  -> member profile: role, join date, activity, card
    PINNED            -> current chat/group ka pinned message
    FILES <query>     -> managed channel ki files (live listing)
    INFO              -> group/channel live facts: members, admins, rights
    ADMINLOG <n>      -> channel admin actions (kisne delete/edit kiya) —
                         MTProto channels.getAdminLog (needs admin-log right)
    LINKCHECK <url>   -> kisi bhi link ko live follow karke real target
                         (spoo.me/bit.ly shortlink expander + t.me checker)

Design rules:
    - Har tool BLOCKING hai par bounded timeout ke saath (kabhi hang nahi).
    - Har tool non-fatal: exception -> "TOOL_ERROR: ..." string, model
      apne aap gracefully handle kar leta hai.
    - Tool results me sirf wahi jaankari jaati hai jo koi bhi group
      member waise bhi padh sakta hai — privacy boundary intact.
"""
from __future__ import annotations

import re
import time
import urllib.request
from typing import Any

import config
import links
import tg_api


# ---------------------------------------------------------------------------
# tool-call parsing
# ---------------------------------------------------------------------------

_TOOL_RE = re.compile(
    r"^\s*(?:TOOL|LOOKUP)\s*[:]\s*(?P<name>[A-Z_]+)\s*(?P<args>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_LEGACY_LOOKUP_RE = re.compile(r"^\s*LOOKUP\s*:\s*(?P<query>.+)$",
                               re.IGNORECASE | re.MULTILINE)

TOOL_MAX_ROUNDS = max(1, int(getattr(config, "TOOL_MAX_ROUNDS", 3)))
TOOL_NAMES = ("SEARCH", "GETMSG", "RECENT", "USER", "WHOIS", "PINNED",
              "FILES", "INFO", "ADMINLOG", "LINKCHECK")


def parse_tool_request(text: str | None) -> tuple[str, str] | None:
    """Model ke reply me TOOL: request hai? -> (NAME, args) ya None.

    LOOKUP: <query> (v4.x legacy) -> ("SEARCH", query) me map hota hai.
    Case-insensitive; pehli TOOL line hi maani jaati hai.
    """
    if not text:
        return None
    m = _TOOL_RE.search(text.strip())
    if m:
        name = m.group("name").strip().upper()
        args = (m.group("args") or "").strip()
        if name == "WHOIS":
            name = "USER"
        if name in TOOL_NAMES:
            return name, args[:200]
    # v4.x legacy: 'LOOKUP: <poora search query>' — naam-capture galat
    # pakadta tha ('key kha milega' -> name=KEY), isliye iska apna regex.
    m = _LEGACY_LOOKUP_RE.search(text.strip())
    if m:
        query = (m.group("query") or "").strip()
        if query:
            return "SEARCH", query[:200]
    return None


# ---------------------------------------------------------------------------
# tool implementations (har ek bounded + non-fatal)
# ---------------------------------------------------------------------------

def _chat_id(user_ctx: dict) -> int | None:
    return user_ctx.get("chat_id") or user_ctx.get("group_id")


def _tool_search(args: str, user_ctx: dict) -> str:
    query = args.strip()
    if not query:
        return "TOOL_ERROR: SEARCH needs a query (TOOL: SEARCH <query>)."
    import knowledge

    def _scope() -> dict:
        if user_ctx.get("group"):
            gid = user_ctx.get("group_id") or user_ctx.get("chat_id")
            if gid:
                return {"chat_id": gid}
        uid = user_ctx.get("user_id")
        return {"user_id": uid} if uid is not None else {}

    try:
        hits = knowledge.search(query, limit=12, **_scope())
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: search failed ({exc!r})"
    if not hits:
        return "(no records matched — you have no data on this)"
    out = []
    for i, h in enumerate(hits, 1):
        meta = []
        if h.get("kind") == "post" and h.get("message_id"):
            meta.append(f"channel post #{h['message_id']}")
        if h.get("file_name"):
            meta.append(f"file: {h['file_name']}")
        for u in (h.get("links") or [])[:3]:
            meta.append(f"link: {u}")
        tag = f" ({'; '.join(meta)})" if meta else ""
        out.append(f"[{i}]{tag} {(h.get('text') or '').strip()[:400]}")
    return "\n".join(out)[:5000] or "(no records matched)"


def _tool_getmsg(args: str, user_ctx: dict) -> str:
    chat_id = _chat_id(user_ctx)
    if not chat_id:
        return "TOOL_ERROR: no chat context."
    m = re.search(r"\d{1,10}", args or "")
    if not m:
        return "TOOL_ERROR: GETMSG needs a numeric message id (TOOL: GETMSG 123)."
    mid = int(m.group(0))
    try:
        import mtproto_layer
        if not mtproto_layer.is_connected():
            return "TOOL_ERROR: realtime layer not connected right now."
        raw = mtproto_layer.fetch_messages(chat_id, [mid])
        p = raw.get(mid)
        if not p:
            return f"message #{mid}: not found in this chat (deleted or never existed)."
        if not p.get("alive"):
            return f"message #{mid}: DELETED (it no longer exists on Telegram)."
        author = (p.get("author") or "").strip() or "a member"
        date = p.get("date") or 0
        when = time.strftime("%d %b %H:%M", time.gmtime(date)) if date else "?"
        txt = (p.get("text") or "").strip()
        media = p.get("file_name") or p.get("file_type") or ""
        body = txt if txt else f"(media message: {media})" if media else "(no text)"
        return f"message #{mid} by {author} at {when} UTC: {body[:600]}"
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {exc!r}"


def _tool_recent(args: str, user_ctx: dict) -> str:
    chat_id = _chat_id(user_ctx)
    if not chat_id:
        return "TOOL_ERROR: no chat context."
    m = re.search(r"\d{1,3}", args or "")
    n = min(max(int(m.group(0)), 1), 30) if m else 15
    try:
        import mtproto_layer
        if not mtproto_layer.is_connected():
            return "TOOL_ERROR: realtime layer not connected right now."
        # current latest known id — bot.py radar ya live context se;
        # nahi to fetch around a high window via context fetch
        latest = mtproto_layer.latest_group_message_id(chat_id)
        if not latest:
            return "TOOL_ERROR: no known recent ids for this chat yet."
        msgs = mtproto_layer.fetch_group_context(chat_id, latest, before=n)
        if not msgs:
            return "(no recent messages found)"
        out = []
        for p in msgs:
            who = (p.get("author") or "").strip() or "member"
            txt = (p.get("text") or "").strip()
            media = p.get("file_name") or p.get("file_type") or ""
            body = txt if txt else (f"(media: {media})" if media else "")
            if not body:
                continue
            out.append(f"#{p['id']} {who}: {body[:150]}")
        return "\n".join(out[-n:])[:4000] or "(no recent text messages)"
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {exc!r}"


def _resolve_user_token(token: str, chat_id: int | None, user_ctx: dict):
    """@username / numeric id / plain naam -> (uid, display_name).

    member_cards bhi search hota hai (sirf messages me na aane wale
    members — radar-seen ya card-only — bhi resolve ho jaate hain).
    """
    import knowledge

    token = (token or "").strip().lstrip("@")
    if not token:
        return None, None
    if token.isdigit() and len(token) >= 5:
        return int(token), None
    try:
        from db import get_db
        coll = get_db()["messages"]
        # username match
        docs = list(coll.find({"username": token})) if hasattr(coll, "find") else []
        if docs:
            d = docs[-1]
            return d.get("user_id"), d.get("first_name") or token
        # first-name match (case-insensitive, safe regex)
        rgx = {"$regex": f"^{re.escape(token)}$", "$options": "i"}
        docs = list(coll.find({"first_name": rgx})) if hasattr(coll, "find") else []
        if docs:
            d = docs[-1]
            return d.get("user_id"), d.get("first_name") or token
    except Exception:
        pass
    # v5.0: member_cards se resolve (radar-seen members bhi)
    try:
        from db import get_db
        cards = get_db()["member_cards"]
        rgx = {"$regex": f"^{re.escape(token)}$", "$options": "i"}
        docs = list(cards.find({"first_name": rgx}))
        if docs:
            d = docs[-1]
            return d.get("user_id"), d.get("first_name") or token
    except Exception:
        pass
    return None, token


def _tool_user(args: str, user_ctx: dict) -> str:
    token = (args or "").strip()
    if not token:
        return "TOOL_ERROR: USER needs a name/@username/id (TOOL: USER rich)."
    chat_id = _chat_id(user_ctx)
    uid, display = _resolve_user_token(token, chat_id, user_ctx)
    if uid is None:
        # khud sender ka naam token ho sakta hai
        if token.lower() in ((user_ctx.get("first_name") or "").lower()):
            uid = user_ctx.get("user_id")
        else:
            return f"(no member named '{token}' in my records — ask them to message in the group)"
    lines = []
    # 1) live role
    if chat_id and uid:
        try:
            m = tg_api.get_chat_member(chat_id, uid) or {}
            status = m.get("status") or "unknown"
            if status in ("creator", "administrator"):
                lines.append(f"role: {status} (admin)")
            elif status:
                lines.append(f"role: {status}")
            jd = m.get("joined_date")
            if jd:
                lines.append("joined: " + time.strftime("%d %b %Y", time.gmtime(jd)))
        except Exception:
            pass
    # 2) member card (activity)
    try:
        from db import get_db
        card = get_db()["member_cards"].find_one({"user_id": uid})
        if card:
            if display is None:
                display = card.get("first_name")
            if card.get("first_seen"):
                lines.append("first seen: " + time.strftime(
                    "%d %b %Y", time.gmtime(card["first_seen"])))
            lines.append(f"messages seen from them: {card.get('msg_count') or 0}")
            if card.get("question_count"):
                lines.append(f"questions asked: {card['question_count']}")
            topics = card.get("topics") or []
            if topics:
                lines.append("topics they talk about: " + ", ".join(topics[:8]))
    except Exception:
        pass
    # 3) unke recent messages + bot ke unhe diye jawab
    try:
        from db import get_db
        docs = list(get_db()["messages"].find(
            {"user_id": uid}))[-8:] if uid is not None else []
        mine = [d for d in docs if (d.get("text") or "").strip()]
        if mine:
            lines.append("recent messages: " + " | ".join(
                (d.get("text") or "").strip()[:80] for d in mine[-6:]))
        qas = list(get_db()["qa"].find({"user_id": uid}))[-6:]
        if qas:
            lines.append("recent Q&A: " + " ;; ".join(
                f"Q: {(q.get('question') or '')[:70]} → A: {(q.get('answer') or '')[:70]}"
                for q in qas[-4:]))
    except Exception:
        pass
    if not lines:
        return f"(member '{display or token}' — no activity record)"
    name = display or token
    return f"member {name} (id {uid}):\n" + "\n".join(lines)


def _tool_pinned(args: str, user_ctx: dict) -> str:
    chat_id = _chat_id(user_ctx)
    if not chat_id:
        return "TOOL_ERROR: no chat context."
    try:
        info = tg_api.get_chat(chat_id) or {}
        pm = info.get("pinned_message") or {}
        if not pm.get("message_id"):
            return "(no pinned message in this chat)"
        who = ((pm.get("from") or {}).get("first_name")) or "—"
        txt = (pm.get("text") or pm.get("caption") or "").strip()
        return (f"pinned message #{pm.get('message_id')} by {who}: "
                + (txt[:500] if txt else "(media message)"))
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {exc!r}"


def _tool_files(args: str, user_ctx: dict) -> str:
    import commands
    try:
        items = commands.list_channel_files((args or "").strip() or None)
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {exc!r}"
    if not items:
        return "(no files found in the channel)"
    out = []
    for it in items[:12]:
        out.append(f"- {it['label'][:60]} [{it.get('type') or 'document'}] "
                   f"(post #{it.get('message_id')})")
    return "channel files:\n" + "\n".join(out)


def _tool_info(args: str, user_ctx: dict) -> str:
    chat_id = _chat_id(user_ctx)
    if not chat_id:
        return "TOOL_ERROR: no chat context."
    lines = []
    try:
        count = tg_api.get_chat_member_count(chat_id)
        if count:
            lines.append(f"members: {count}")
    except Exception:
        pass
    try:
        admins = tg_api.get_chat_administrators(chat_id) or []
        names = []
        for a in admins:
            u = a.get("user") or {}
            nm = u.get("first_name") or u.get("username") or str(u.get("id"))
            if a.get("status") == "creator":
                nm += " (owner)"
            names.append(nm)
        if names:
            lines.append("admins: " + ", ".join(names[:10]))
    except Exception:
        pass
    try:
        cid = links.get_channel_id()
        if cid:
            info = tg_api.get_chat(cid) or {}
            uname = (info.get("username") or "").strip()
            title = info.get("title") or "channel"
            lines.append(f"linked channel: {title}"
                         + (f" (@{uname})" if uname else " (private)"))
    except Exception:
        pass
    if not lines:
        return "TOOL_ERROR: could not fetch live chat info"
    return "live chat info:\n" + "\n".join(lines)


def _tool_adminlog(args: str, user_ctx: dict) -> str:
    m = re.search(r"\d{1,3}", args or "")
    n = min(max(int(m.group(0)), 1), 20) if m else 10
    cid = links.get_channel_id()
    if not cid:
        return "TOOL_ERROR: no managed channel linked."
    try:
        import mtproto_layer
        if not mtproto_layer.is_connected():
            return "TOOL_ERROR: realtime layer not connected right now."
        return mtproto_layer.get_admin_log(cid, n)
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {exc!r}"


def _tool_linkcheck(args: str, user_ctx: dict) -> str:
    url = (args or "").strip()
    if not re.match(r"^https?://", url):
        if re.match(r"^[\w.-]+\.\w{2,}", url):
            url = "https://" + url
        else:
            return "TOOL_ERROR: LINKCHECK needs a URL (TOOL: LINKCHECK https://...)."
    # 1) t.me managed-post links -> MTProto live verdict
    m = re.search(r"t\.me/(?:c/(\d+)|([\w.]+))/(\d{1,6})", url)
    if m:
        try:
            import mtproto_layer
            if mtproto_layer.is_connected():
                cid = links.get_channel_id()
                if cid:
                    raw = mtproto_layer.fetch_messages(cid, [int(m.group(3))])
                    p = raw.get(int(m.group(3)))
                    if p is None:
                        return f"{url}: NOT FOUND in the channel (dead/wrong link)"
                    if not p.get("alive"):
                        return f"{url}: DELETED from the channel (dead link)"
                    return f"{url}: LIVE — post exists: {(p.get('text') or '(media)')[:150]}"
        except Exception:
            pass
    # 2) generic redirect follower (spoo.me / bit.ly expander)
    hops, final, status = 0, url, ""
    cur = url
    while hops < 4:
        try:
            req = urllib.request.Request(cur, method="HEAD",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                status = str(resp.status)
                final = resp.geturl() or cur
                if final == cur:
                    break
                cur = final
                hops += 1
        except Exception as exc:  # noqa: BLE001
            status = repr(exc)[:80]
            break
    if hops == 0 and final == url:
        return f"{url} -> reachable ({status})"
    if final != url:
        return f"{url} -> redirects to {final} (status {status})"
    return f"{url} -> status {status}"


_TOOLS = {
    "SEARCH": _tool_search,
    "GETMSG": _tool_getmsg,
    "RECENT": _tool_recent,
    "USER": _tool_user,
    "PINNED": _tool_pinned,
    "FILES": _tool_files,
    "INFO": _tool_info,
    "ADMINLOG": _tool_adminlog,
    "LINKCHECK": _tool_linkcheck,
}


def execute(name: str, args: str, user_ctx: dict) -> str:
    """Ek tool chalao. Hamesha string return (kabhi raise nahi)."""
    fn = _TOOLS.get(name)
    if not fn:
        return f"TOOL_ERROR: unknown tool {name}. Valid: {', '.join(TOOL_NAMES)}"
    try:
        return fn(args, user_ctx)
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {name} failed ({exc!r})"


# ---------------------------------------------------------------------------
# tool-loop driver — agent.answer isse use karta hai
# ---------------------------------------------------------------------------

TOOL_STATS: dict[str, int] = {"calls": 0, "rounds": 0}


def run_tool_loop(question: str, first_text: str, user_ctx: dict,
                  generate_fn, system: str,
                  notify_interim=None) -> tuple[str | None, str | None]:
    """Multi-round tool loop.

    Args:
      first_text  — model ka pehla reply (isme TOOL: ho sakta hai)
      generate_fn — fn(prompt, system, user_ctx) -> str
      notify_interim — fn() -> None (ek baar, pehle tool call par
                        'Ruko bhai...' bhejne ke liye)

    Return: (final_text, interim_sent_marker)
    """
    interim: str | None = None
    text = (first_text or "").strip()
    req = parse_tool_request(text)
    if not req:
        return first_text, None  # koi tool nahi — seedha final
    results: list[str] = []
    rounds = 0
    while req and rounds < TOOL_MAX_ROUNDS:
        if rounds == 0 and notify_interim:
            try:
                notify_interim()
                interim = "sent"
            except Exception:
                pass
        name, args = req
        result = execute(name, args, user_ctx)
        with _STAT_LOCK:
            TOOL_STATS["calls"] += 1
        results.append(f"TOOL {name} result:\n{result}")
        rounds += 1
        with _STAT_LOCK:
            TOOL_STATS["rounds"] += 1
        followup = (
            f"User ka original sawal: {question}\n\n"
            + ("\n\n".join(results)[:9000])
            + "\n\nYe REAL live results hain (tumhari memory nahi — Telegram/"
              "Mongo ka asli data). Ab ya to AGLA tool call karo (TOOL: <NAME> "
              "<args>, max 3 total), ya ab FINAL jawab do — user ki language "
              "me, concise, apne shabdon me. Jo tool result me nahi mila, "
              "usko 'mere paas record nahi hai' hi bolo — invent mat karo. "
              "Fake progress phrases ('check kar raha hu') bilkul nahi."
        )
        try:
            text = (generate_fn(followup, system, user_ctx) or "").strip()
        except Exception:
            text = ""
        if not text:
            break
        req = parse_tool_request(text)
    return text or None, interim


_STAT_LOCK = __import__("threading").Lock()


def stats() -> dict:
    with _STAT_LOCK:
        return dict(TOOL_STATS)
