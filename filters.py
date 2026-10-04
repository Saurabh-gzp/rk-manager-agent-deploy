r"""Rose-style content filters (custom filters).

How the REAL Rose bot (@MissRose_bot) filters work — this module is a
faithful port of that behavior onto the plain Bot API:

  - `/filter <keyword> <reply text>`        → text filter
  - `/filter <keyword>` (replying to a msg) → the replied message is saved
    AS-IS: photo, video, document, audio, voice, sticker, animation (GIF),
    video-note, contact, caption text — anything.
  - When ANYONE (owner, admin, member) uses the keyword in chat, the bot
    REPLIES to that message with the saved content — the exact same media
    and the exact same caption. Filters NEVER delete messages (that is
    what the abuse blocklist does instead).
  - `/filters` lists them, `/stop <word>` removes one, `/stopall` clears.
  - Keyword matching is Rose's word-boundary regex:
        ( |^|[^\w])keyword( |$|[^\w])     with IGNORECASE
    so filter 'ok' fires on 'ok', 'OK?', 'theek ok.' — but NOT on 'okok'.

Delivery strategy (stronger than stock Rose because we keep BOTH a
message pointer and the file_id):
  1. copyMessage  → pixel-perfect as-is copy, no 'Forwarded from' header
  2. sendMediaGroup / send by file_id → works even after the original
     message got deleted (file_ids stay valid)
  3. plain text   → last resort for text-only content

Albums (media groups): when the owner replies to one part of an album,
every cached part of that media-group is saved and delivered together,
regrouped via sendMediaGroup — exactly like Rose delivers whole albums.

Legacy migration: old-style entries ({reply: None} blocklists saved by
the previous auto-delete implementation) are inert and lazily removed.
"""
from __future__ import annotations

import re
import time
import unicodedata

import tg_api
from db import get_state, save_state


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


_KIND_ICONS = {
    "text": "💬", "photo": "🖼", "video": "🎬", "document": "📄",
    "audio": "🎵", "voice": "🎤", "animation": "🎞", "sticker": "🏷",
    "video_note": "👁", "contact": "👤", "album": "🗂",
}

# Media types the Bot API accepts inside sendMediaGroup.
_GROUPABLE = {"photo", "video", "document", "audio"}


def _content_from_message(msg: dict) -> tuple[str | None, str | None, str]:
    """Extract (kind, file_id, caption) from a message dict, any content."""
    msg = msg or {}
    for ft in ("document", "video", "audio", "voice", "video_note",
               "animation", "sticker"):
        m = msg.get(ft)
        if isinstance(m, dict) and m.get("file_id"):
            return ft, m["file_id"], (msg.get("caption") or "")
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        best = max(photos, key=lambda p: (
            p.get("file_size") or 0, p.get("width") or 0,
            p.get("height") or 0))
        if best.get("file_id"):
            return "photo", best["file_id"], (msg.get("caption") or "")
    contact = msg.get("contact")
    if isinstance(contact, dict) and (contact.get("phone_number")
                                      or contact.get("vcard")):
        return "contact", None, (msg.get("caption") or msg.get("text") or "")
    return None, None, (msg.get("text") or msg.get("caption") or "")


def _describe(kind: str | None, target: dict, caption: str) -> str:
    """One-line human label for the /filters list."""
    if kind == "document":
        name = ((target.get("document") or {}).get("file_name")
                or (target.get("document") or {}).get("file_id") or "file")
        return f"file: {name}"
    if kind == "album":
        return "album (media group)"
    if kind and kind != "text":
        return _KIND_ICONS.get(kind, "") + " " + kind
    text = (caption or "").strip()
    if text:
        return ("text: " + text[:32] + "...") if len(text) > 32 else f"text: {text}"
    return "text"


def _entry_usable(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("items"):
        return True
    if entry.get("file_id") or (entry.get("text") or entry.get("reply")):
        return True
    src = entry.get("src") or {}
    return bool(src.get("message_id"))


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def get_filters() -> dict:
    value = get_state("mod_filters", {})
    return dict(value) if isinstance(value, dict) else {}


def add_filter(word: str, entry: dict, by_uid: int) -> str | None:
    """Save a content filter. Returns the normalized word, or None if bad."""
    word = _normalize(word)
    if not word or len(word) > 64:
        return None
    entry = dict(entry or {})
    entry["by"] = int(by_uid or 0)
    entry["ts"] = time.time()
    filters = get_filters()
    filters[word] = entry
    save_state("mod_filters", filters)
    return word


def add_text_filter(word: str, text: str, by_uid: int) -> str | None:
    """Shorthand: save a plain text reply filter (Rose's typed-text filter)."""
    return add_filter(word, {"kind": "text", "text": (text or "").strip(),
                             "label": "text"}, by_uid)


def remove_filter(word: str) -> bool:
    word = _normalize(word)
    filters = get_filters()
    if word not in filters:
        return False
    del filters[word]
    save_state("mod_filters", filters)
    return True


def clear_filters() -> None:
    save_state("mod_filters", {})


# ---------------------------------------------------------------------------
# Rose-style trigger matching
# ---------------------------------------------------------------------------

_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _word_pattern(word: str) -> re.Pattern | None:
    pat = _PATTERN_CACHE.get(word)
    if pat is None:
        try:
            pat = re.compile(
                r"( |^|[^\w])" + re.escape(word) + r"( |$|[^\w])",
                re.IGNORECASE)
        except Exception:
            return None
        if len(_PATTERN_CACHE) > 500:
            _PATTERN_CACHE.clear()
        _PATTERN_CACHE[word] = pat
    return pat


def matches_filter(text: str) -> tuple[str, dict] | None:
    """Return (word, entry) for the first matching filter, else None.

    Rose semantics: word-boundary regex on the raw text, IGNORECASE.
    Old schema-less entries (auto-delete blocklists) are inert; they are
    lazily removed the first time they would have fired.
    """
    if not text:
        return None
    filters_map = get_filters()
    if not filters_map:
        return None
    stale: list[str] = []
    hit = None
    for word, entry in filters_map.items():
        if not word:
            continue
        pat = _word_pattern(word)
        if not pat or not pat.search(str(text)):
            continue
        if not _entry_usable(entry):
            stale.append(word)   # legacy auto-delete filter: drop it
            continue
        hit = (word, entry)
        break
    for w in stale:
        remove_filter(w)
    return hit


# ---------------------------------------------------------------------------
# album (media-group) cache
# ---------------------------------------------------------------------------

_ALBUMS: dict[tuple[int, str], dict] = {}
_ALBUM_TTL = 6 * 3600


def remember_media(chat_id, msg: dict) -> None:
    """Cache one album part so /filter can later save the WHOLE album.

    Cheap, in-memory, never raises, no Telegram API calls.
    """
    try:
        if not chat_id:
            return
        mgid = msg.get("media_group_id")
        if not mgid:
            return
        kind, file_id, cap = _content_from_message(msg)
        if not file_id:
            return
        key = (int(chat_id), str(mgid))
        item = {"message_id": msg.get("message_id"), "kind": kind,
                "file_id": file_id, "caption": cap or None}
        bucket = _ALBUMS.get(key)
        if bucket is None:
            if len(_ALBUMS) > 200:
                now = time.time()
                for k in [k for k, v in _ALBUMS.items()
                          if now - (v.get("ts") or 0) > _ALBUM_TTL]:
                    _ALBUMS.pop(k, None)
            bucket = {"ts": 0.0, "items": []}
            _ALBUMS[key] = bucket
        bucket["ts"] = time.time()
        if item["message_id"] not in {i.get("message_id")
                                      for i in bucket["items"]}:
            bucket["items"].append(item)
            bucket["items"].sort(key=lambda i: i.get("message_id") or 0)
    except Exception:
        pass


def _album_items(chat_id: int, mgid, target: dict) -> list[dict]:
    """All parts of a media group (cache + the replied-to part)."""
    items: list[dict] = []
    seen: set = set()
    bucket = _ALBUMS.get((int(chat_id), str(mgid))) or {}
    for it in bucket.get("items") or []:
        mid = it.get("message_id")
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        items.append({"src": {"chat_id": chat_id, "message_id": mid},
                      "kind": it.get("kind"), "file_id": it.get("file_id"),
                      "caption": it.get("caption")})
    mid = (target or {}).get("message_id")
    if mid and mid not in seen:
        kind, file_id, cap = _content_from_message(target or {})
        items.append({"src": {"chat_id": chat_id, "message_id": mid},
                      "kind": kind, "file_id": file_id,
                      "caption": cap or None})
    items.sort(key=lambda i: (i.get("src") or {}).get("message_id") or 0)
    return items


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

def _deliver_album_group(chat_id: int, items: list[dict],
                         reply_to_message_id) -> bool:
    """Re-group the album with sendMediaGroup (exactly Rose's approach)."""
    media = []
    for it in items:
        fid = it.get("file_id")
        t = (it.get("kind") or "document")
        if not fid or t not in _GROUPABLE:
            return False
        m = {"type": t, "media": fid}
        cap = (it.get("caption") or "").strip()
        if cap:
            m["caption"] = cap[:1024]
        media.append(m)
    if not 2 <= len(media) <= 10:
        return False
    r = tg_api.send_media_group(chat_id, media,
                                reply_to_message_id=reply_to_message_id)
    return bool(r.get("ok"))


def deliver_filter(chat_id: int, word: str, entry: dict,
                   reply_to_message_id=None) -> bool:
    """Deliver a filter's saved content as-is, as a reply. Never deletes.

    Order: copyMessage (perfect as-is) -> album regroup -> file_id
    re-send (Rose's way) -> caption/text send.
    """
    entry = entry if isinstance(entry, dict) else {}
    items = list(entry.get("items") or [])
    if not items:
        src = entry.get("src") or {}
        if src.get("message_id"):
            items = [{"src": src, "kind": entry.get("kind"),
                      "file_id": entry.get("file_id"),
                      "caption": entry.get("caption")}]
    text = str(entry.get("text") or entry.get("reply") or "").strip()

    if not items:
        if text:
            r = tg_api.send_message(chat_id, text[:4000],
                                    reply_to_message_id=reply_to_message_id)
            return bool(r.get("ok"))
        return False

    # Whole album saved → deliver it regrouped like Rose does.
    if len(items) > 1 and _deliver_album_group(
            chat_id, items, reply_to_message_id):
        return True

    delivered = 0
    for it in items:
        # 1) perfect as-is copy — no 'Forwarded from' header
        src = it.get("src") or {}
        if src.get("chat_id") and src.get("message_id"):
            r = tg_api.copy_message(chat_id, src["chat_id"],
                                    src["message_id"],
                                    reply_to_message_id=reply_to_message_id)
            if r.get("ok"):
                delivered += 1
                continue
        # 2) file_id re-send — works even if the source message is gone
        fid = it.get("file_id")
        if fid:
            r = tg_api.send_any_file(
                chat_id, fid, it.get("kind") or "document",
                caption=(it.get("caption") or None),
                reply_to_message_id=reply_to_message_id)
            if r.get("ok"):
                delivered += 1
                continue
        # 3) caption/text only
        cap = str(it.get("caption") or "").strip()
        if cap:
            r = tg_api.send_message(chat_id, cap[:4000])
            delivered += 1 if r.get("ok") else 0
    return delivered > 0


# ---------------------------------------------------------------------------
# command handling: /filter /filters /stop /stopall
# (Rose-style; called from moderation.handle_mod_command AFTER the
#  owner/admin permission gates, with a `say` closure that threads the
#  reply to the command message.)
# ---------------------------------------------------------------------------

def _split_args(args: str) -> tuple[str | None, str]:
    """Split '/filter' args into (word, rest). Quoted phrases supported."""
    args = (args or "").strip()
    if args.startswith('"'):
        end = args.find('"', 1)
        if end == -1:
            return None, ""
        return args[1:end], args[end + 1:].strip()
    parts = args.split(None, 1)
    if not parts:
        return None, ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _build_entry(chat_id: int, target: dict, rest: str) -> tuple[dict | None, str]:
    """Build the filter entry: typed text wins (Rose), else the replied
    message's own content saved AS-IS (any type). Returns (entry, label)."""
    rest = (rest or "").strip()
    target = target or {}

    # Rose behavior: text typed after the keyword is the filter content.
    if rest:
        return ({"kind": "text", "text": rest[:4000], "label": "text"},
                "text")

    # No typed text → save the replied-to message itself, as-is.
    if target:
        kind, file_id, cap = _content_from_message(target)
        label = _describe(kind, target, cap)
        mid = target.get("message_id")
        src = ({"chat_id": chat_id, "message_id": mid} if mid else None)
        mgid = target.get("media_group_id")
        if mgid:
            items = _album_items(chat_id, mgid, target)
            if len(items) > 1:
                return ({"kind": "album", "items": items, "label": label},
                        label)
        if kind:
            entry = {"kind": kind, "label": label, "src": src}
            if file_id:
                entry["file_id"] = file_id
            if cap:
                entry["caption"] = cap[:1024]
                entry["text"] = cap[:4000]
            if kind == "contact":
                entry["contact"] = {
                    "phone_number": (target.get("contact") or {}).get(
                        "phone_number") or "",
                    "first_name": (target.get("contact") or {}).get(
                        "first_name") or "Contact",
                    "last_name": (target.get("contact") or {}).get(
                        "last_name") or "",
                    "vcard": (target.get("contact") or {}).get("vcard") or "",
                }
            return (entry, label)
        # plain text message replied to → save its text as-is
        t = (target.get("text") or "").strip()
        if t:
            return ({"kind": "text", "text": t[:4000],
                     "label": _describe("text", target, t),
                     "src": src}, "text")
    return (None, "")


def handle_filter_command(msg: dict, uid: int, chat_id: int, cmd: str,
                          args: str, say) -> bool:
    """Handle /filter /filters /stop /stopall. Always returns True."""
    text = (msg.get("text") or "").strip()

    if cmd == "filter":
        word, rest = _split_args(args)
        if not word:
            return say(
                "Usage (Rose-style):\n"
                "• Kisi bhi message pe REPLY karke: /filter <word>\n"
                "  → wo message as-is save hoga (file, video, photo, text — "
                "kuch bhi)\n"
                "• Ya seedha: /filter <word> <reply text>\n\n"
                "Jab bhi koi <word> bolega, bot usko saved content ke sath "
                "REPLY karega. Message delete kabhi nahi hota.")
        entry, label = _build_entry(chat_id, msg.get("reply_to_message") or {},
                                    rest)
        if entry is None:
            return say(
                f'❌ Kya save karu? Kisi message pe REPLY karke "/filter '
                f'{word}" likho, ya text do: /filter {word} <reply text>')
        saved = add_filter(word, entry, uid)
        if not saved:
            return say("❌ Filter word invalid hai (khali ya bahut lamba).")
        icon = _KIND_ICONS.get(entry.get("kind") or "text", "💬")
        n = len(entry.get("items") or [])
        extra = f" — {n} items ka album" if n > 1 else ""
        return say(
            f'✅ Filter "{saved}" save ho gaya {icon}{extra}\n'
            f'Ab koi bhi "{saved}" bolega to main ye content as-is '
            f'reply karunga — delete kuch nahi hota.')

    if cmd == "filters":
        flist = get_filters()
        if not flist:
            return say("Koi filter nahi hai. Kisi message pe reply karke "
                       "/filter <word> se add karo.")
        lines = ["📋 Group filters (Rose-style):"]
        for w, e in sorted(flist.items()):
            e = e if isinstance(e, dict) else {}
            icon = _KIND_ICONS.get(e.get("kind") or "text", "💬")
            label = e.get("label") or ""
            lines.append(f"• {icon} {w}" + (f" — {label}" if label else ""))
        lines.append("\n/stop <word> — hatao. Trigger hone par content "
                     "as-is REPLY hota hai (delete kabhi nahi).")
        return say("\n".join(lines))

    if cmd == "stop":
        word, _ = _split_args(args)
        word = _normalize(word or "")
        if not word:
            cur = ", ".join(sorted(get_filters())) or "(koi nahi)"
            return say(f"Usage: /stop <word>\n\nAbhi ke filters: {cur}")
        if remove_filter(word):
            return say(f'🛑 Filter "{word}" hata diya — ab is word par '
                       'reply nahi karunga.')
        return say(f'❌ "{word}" naam ka filter nahi mila. /filters dekho.')

    if cmd == "stopall":
        clear_filters()
        return say("🗑 Saare filters clear kar diye.")

    return False
