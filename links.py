"""Group <-> Channel linking + management.

The owner sets group_id and channel_id via the panel. We track them in `state`
and a `links` collection so that:
  - when the bot is added to a new chat, we know if it's the configured one
  - we can look up "the channel for this group" or vice versa
  - leaving/joining a chat updates state
"""
from __future__ import annotations
import time
from typing import Any

import config
from db import get_db, get_state, save_state


def get_group_id() -> int:
    v = get_state("group_id")
    return int(v) if isinstance(v, (int, str)) and str(v).lstrip("-").isdigit() else 0


def get_channel_id() -> int:
    v = get_state("channel_id")
    return int(v) if isinstance(v, (int, str)) and str(v).lstrip("-").isdigit() else 0


def set_group(chat_id: int, title: str | None = None) -> None:
    save_state("group_id", int(chat_id))
    if title:
        save_state("group_title", title)
    _link_pair(group_id=int(chat_id), channel_id=get_channel_id())


def set_channel(chat_id: int, title: str | None = None) -> None:
    save_state("channel_id", int(chat_id))
    if title:
        save_state("channel_title", title)
    _link_pair(group_id=get_group_id(), channel_id=int(chat_id))


def clear_group() -> None:
    save_state("group_id", 0)


def clear_channel() -> None:
    save_state("channel_id", 0)


def _link_pair(group_id: int, channel_id: int) -> None:
    db = get_db()
    if group_id and channel_id:
        db["links"].update_one(
            {"group_id": group_id, "channel_id": channel_id},
            {"$set": {"linked_at": time.time()}},
            upsert=True,
        )
    if group_id:
        # remove other links with this group
        for d in db["links"].find({"group_id": group_id}):
            if d.get("channel_id") != channel_id:
                db["links"].delete_many({"_id": d["_id"]})
    if channel_id:
        for d in db["links"].find({"channel_id": channel_id}):
            if d.get("group_id") != group_id:
                db["links"].delete_many({"_id": d["_id"]})


def get_link() -> dict:
    """Return the active link record (group + channel)."""
    g = get_group_id()
    c = get_channel_id()
    if not g and not c:
        return {}
    if g and c:
        return get_db()["links"].find_one({"group_id": g, "channel_id": c}) or {"group_id": g, "channel_id": c}
    if g:
        return {"group_id": g, "channel_id": 0}
    return {"group_id": 0, "channel_id": c}


def is_managed_group(chat_id: int) -> bool:
    return bool(get_group_id()) and get_group_id() == int(chat_id)


def is_managed_channel(chat_id: int) -> bool:
    return bool(get_channel_id()) and get_channel_id() == int(chat_id)