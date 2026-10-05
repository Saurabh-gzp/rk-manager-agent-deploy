"""Community identity facts — WHO is who in this bot's world.

The bot's AI used to answer 'owner kaun hai? channel kaun sa?' with 'mere
paas record nahi hai' — because the prompts never contained the owner's or
the channel's identity. This module fixes that permanently: it gathers
owner / group / channel identity ONCE (live Telegram lookups, cached in
state with a long TTL) and renders a COMMUNITY CONTEXT block that
agent.answer() injects into every prompt.

Sources of truth (in order):
  - config.OWNER_CHAT_ID  -> getChat() for the owner's name/@username
  - links.get_channel_id() -> getChat() for the channel's title/@username/
    description (channel descriptions often carry 'Owner: @username')
  - DB message counts for 'owner se kitni baar baat hui' style facts.
"""
from __future__ import annotations

import re
import time

import config
import links
import tg_api
from db import get_state, save_state

_CACHE_TTL = 6 * 3600  # refresh identity facts every 6h (cheap either way)


def _cached(key: str):
    """Return a fresh cached value, or None."""
    box = get_state(key) or {}
    if not isinstance(box, dict):
        return None
    if time.time() - float(box.get("ts") or 0) > _CACHE_TTL:
        return None
    return box.get("v")


def _store(key: str, value) -> None:
    try:
        save_state(key, {"ts": time.time(), "v": value})
    except Exception:
        pass


def _chat_identity(chat_id: int, cache_key: str) -> dict:
    """Fetch {title, username, description, type} for a chat (cached)."""
    if not chat_id:
        return {}
    hit = _cached(cache_key)
    if isinstance(hit, dict):
        return hit
    info = tg_api.get_chat(chat_id) or {}
    ident = {
        "title": (info.get("title") or "").strip(),
        "username": (info.get("username") or "").strip(),
        "description": (info.get("description") or "").strip(),
        "type": info.get("type") or "",
    }
    if ident["title"] or ident["username"]:
        _store(cache_key, ident)
    return ident


# ---------------------------------------------------------------------------
# Channel owner extraction — channel descriptions commonly carry a line like
# 'Owner: @oxsidianX'. Parse it so 'channel ka owner kaun hai?' is answerable.
# ---------------------------------------------------------------------------

_OWNER_LINE_RE = re.compile(
    r"owner\s*[:\-–]\s*(@?[A-Za-z0-9_]{3,64})", re.IGNORECASE)


def extract_channel_owner(description: str | None) -> str:
    """Pull an 'Owner: @handle' mention out of a channel description."""
    m = _OWNER_LINE_RE.search(description or "")
    if not m:
        return ""
    handle = m.group(1).strip()
    return handle if handle.startswith("@") else "@" + handle


def owner_identity() -> dict:
    """The bot OWNER's identity: {name, username, id}."""
    oid = config.OWNER_CHAT_ID
    if not oid:
        return {}
    hit = _cached("community_owner")
    if isinstance(hit, dict):
        return hit
    info = tg_api.get_chat(oid) or {}
    ident = {
        "id": oid,
        "name": (info.get("first_name") or info.get("title") or "").strip(),
        "username": (info.get("username") or "").strip(),
    }
    if ident["name"] or ident["username"]:
        _store("community_owner", ident)
    return ident


def channel_identity() -> dict:
    """The managed channel's identity: {id, title, username, owner, link}."""
    cid = links.get_channel_id()
    if not cid:
        return {}
    ident = _chat_identity(cid, "community_channel")
    owner_handle = extract_channel_owner(ident.get("description"))
    out = {
        "id": cid,
        "title": ident.get("title") or "",
        "username": ident.get("username") or "",
        "owner": owner_handle,
    }
    out["link"] = f"https://t.me/{out['username']}" if out["username"] else ""
    return out


def group_identity() -> dict:
    """The managed group's identity: {id, title}."""
    gid = links.get_group_id()
    if not gid:
        return {}
    ident = _chat_identity(gid, "community_group")
    return {"id": gid, "title": ident.get("title") or ""}


_ACT_TTL = 600  # owner message count: 10-min cache (it moves slowly)


def _owner_activity() -> dict:
    """How many messages the OWNER has sent (learned memory) — grounds
    'owner se kitni baar baat ki' style questions in real counts.

    SPEED: count_documents costs ~140ms on Atlas M0 and used to run on
    EVERY answer; now it refreshes at most every 10 minutes.
    """
    try:
        box = get_state("community_owner_activity") or {}
        if isinstance(box, dict) and time.time() - float(box.get("ts") or 0) < _ACT_TTL:
            v = box.get("v")
            return v if isinstance(v, dict) else {}
        from db import get_db
        n = get_db()["messages"].count_documents(
            {"user_id": config.OWNER_CHAT_ID})
        out = {"owner_messages": int(n)}
        save_state("community_owner_activity", {"ts": time.time(), "v": out})
        return out
    except Exception:
        return {}


def community_block() -> str:
    """The COMMUNITY CONTEXT injected into every AI prompt.

    Guarantees the model ALWAYS knows: who the owner is, what the group and
    channel are, and the channel's public owner handle. Never raises.
    """
    try:
        lines = ["COMMUNITY FACTS (verified — trust these over everything):"]
        oi = owner_identity()
        if oi:
            who = oi.get("name") or "owner"
            uname = f" (@{oi['username']})" if oi.get("username") else ""
            lines.append(
                f"- Group & channel OWNER (your boss): {who}{uname}, "
                f"ID {oi.get('id')}. Ye wahi insaan hai jiske liye tum kaam "
                f"karte ho — sab rules/instructions SIRF isi se aate hain.")
        gi = group_identity()
        if gi:
            lines.append(f"- Managed group: {gi.get('title') or '—'} "
                         f"(ID {gi.get('id')}).")
        ci = channel_identity()
        if ci:
            link = ci.get("link") or f"(private channel, ID {ci.get('id')})"
            own = f", channel owner: {ci['owner']}" if ci.get("owner") else ""
            lines.append(
                f"- Linked channel: {ci.get('title') or '—'} — {link}{own}. "
                f"Channel ka content members ko unke maangne par forward "
                f"hotaa hai (real forward, system karta hai).")
        act = _owner_activity()
        if act.get("owner_messages"):
            lines.append(
                f"- Owner ne ab tak {act['owner_messages']} messages group me "
                f"bheje hain (learned memory).")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)
    except Exception:
        return ""


def refresh_now() -> None:
    """Force-drop the identity caches (owner panel 'refresh' hook)."""
    try:
        for k in ("community_owner", "community_channel", "community_group"):
            save_state(k, {})
    except Exception:
        pass
