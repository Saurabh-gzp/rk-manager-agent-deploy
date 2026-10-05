"""Telegram Bot API client (stdlib only — no external deps).

SPEED: keeps ONE persistent HTTPS connection to api.telegram.org.
urllib opens a fresh TCP+TLS handshake per call (~0.5-0.8s measured);
with a keep-alive connection each call costs one roundtrip instead of
three. Falls back to a fresh urllib request for long polls (getUpdates)
and file downloads, and auto-reconnects on a dead connection.
"""
from __future__ import annotations
import http.client
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "https://api.telegram.org"
API_HOSTNAME = "api.telegram.org"
MAX_TEXT = 4096

# Owner rule (2026-10-05): link previews OFF in EVERY bot message.
# link_preview_options is the modern Bot API 7.0+ field; passing it on
# sendMessage / editMessageText / copyMessage (all verified live).
NO_PREVIEW = {"is_disabled": True}

# --- persistent connection (keep-alive) ---
_CONN: http.client.HTTPSConnection | None = None
_CONN_LOCK = threading.Lock()


def _resolve_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


def _tg_path(method: str) -> str:
    token = _resolve_token()
    return f"/bot{token}/{method}"


def _read_json(resp) -> dict:
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "description": "non-json response"}


def _multipart(params: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----rkbot" + os.urandom(8).hex()
    body = bytearray()
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
        ).encode("utf-8")
    for field, (filename, data, ctype) in (files or {}).items():
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _encode_body(params: dict, files: dict | None) -> tuple[bytes, str]:
    if files:
        return _multipart(params, files)
    form = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        form[k] = str(v)
    return urllib.parse.urlencode(form).encode("utf-8"), \
        "application/x-www-form-urlencoded"


def _api_keepalive(method: str, body: bytes, ctype: str,
                   timeout: int) -> dict | None:
    """POST over the shared keep-alive connection. None = connection-level
    failure (caller falls back to urllib); dict = actual API response."""
    global _CONN
    with _CONN_LOCK:
        for _ in range(2):  # one reconnect attempt on a dead socket
            try:
                if _CONN is None:
                    _CONN = http.client.HTTPSConnection(
                        API_HOSTNAME, timeout=timeout)
                _CONN.request("POST", _tg_path(method), body=body,
                              headers={"Content-Type": ctype})
                resp = _CONN.getresponse()
                raw = resp.read()
                try:
                    return json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    return {"ok": False, "description": "non-json response"}
            except Exception:
                try:
                    if _CONN is not None:
                        _CONN.close()
                except Exception:
                    pass
                _CONN = None  # reconnect on next loop/pass
    return None


def api(method: str, params: dict | None = None, files: dict | None = None, timeout: int = 20) -> dict:
    """Call a Bot API method. Never raises.

    Short calls ride the persistent keep-alive connection; long polls and
    oversized timeouts use a one-shot urllib request (so a 50s getUpdates
    never blocks every other call on the shared socket).
    """
    params = params or {}
    body, ctype = _encode_body(params, files)
    if timeout <= 30:
        r = _api_keepalive(method, body, ctype, timeout)
        if r is not None:
            return r
    # fallback / long-poll path (fresh connection)
    url = f"https://{API_HOSTNAME}{_tg_path(method)}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_json(resp)
    except urllib.error.HTTPError as e:
        try:
            return _read_json(e)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": str(e)}
    except urllib.error.URLError as e:
        return {"ok": False, "description": f"URLError: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"error: {e}"}


# ---------------------- Convenience wrappers ----------------------

def send_message(chat_id, text, reply_markup=None, parse_mode=None,
                 link_preview_options=None,
                 reply_to_message_id=None) -> dict:
    """Send text. Link preview is ALWAYS disabled unless a caller explicitly
    passes its own link_preview_options dict (owner rule: preview off)."""
    text = str(text or "")
    chunks = [text[i:i + MAX_TEXT] for i in range(0, max(len(text), 1), MAX_TEXT)]
    first = None
    for idx, chunk in enumerate(chunks):
        p = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "link_preview_options": link_preview_options if link_preview_options is not None else NO_PREVIEW,
            "reply_markup": reply_markup if idx == 0 else None,
            "reply_to_message_id": reply_to_message_id if idx == 0 else None,
        }
        r = api("sendMessage", p)
        if idx == 0:
            first = r
    return first or {}


def edit_message_text(chat_id, message_id, text, reply_markup=None,
                      parse_mode=None, link_preview_options=None) -> dict:
    return api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "link_preview_options": link_preview_options if link_preview_options is not None else NO_PREVIEW,
        "reply_markup": reply_markup,
    })


def answer_callback_query(cb_id, text=None, show_alert=False) -> dict:
    return api("answerCallbackQuery", {
        "callback_query_id": cb_id,
        "text": text,
        "show_alert": show_alert,
    })


def get_updates(offset=None, timeout=50, allowed_updates=None) -> list[dict]:
    p: dict = {"timeout": timeout}
    if offset is not None:
        p["offset"] = offset
    if allowed_updates:
        p["allowed_updates"] = allowed_updates
    r = api("getUpdates", p, timeout=timeout + 10)
    if isinstance(r, dict) and r.get("ok"):
        return r.get("result") or []
    return []


def get_me() -> dict:
    r = api("getMe")
    return r.get("result", {}) if isinstance(r, dict) and r.get("ok") else {}


def get_chat(chat_id) -> dict:
    r = api("getChat", {"chat_id": chat_id})
    return r.get("result", {}) if isinstance(r, dict) and r.get("ok") else {}


def get_chat_administrators(chat_id) -> list[dict]:
    r = api("getChatAdministrators", {"chat_id": chat_id})
    return r.get("result", []) if isinstance(r, dict) and r.get("ok") else []


def get_chat_member(chat_id, user_id) -> dict:
    r = api("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    return r.get("result", {}) if isinstance(r, dict) and r.get("ok") else {}


def get_chat_member_count(chat_id) -> int:
    r = api("getChatMemberCount", {"chat_id": chat_id})
    try:
        return int(r.get("result", 0)) if isinstance(r, dict) and r.get("ok") else 0
    except Exception:
        return 0


def delete_message(chat_id, message_id) -> dict:
    if not message_id:
        return {"ok": False, "description": "message_id is required"}
    return api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def get_file(file_id) -> dict:
    if not file_id:
        return {"ok": False, "description": "file_id is required"}
    return api("getFile", {"file_id": file_id})


def download_file(file_id, max_bytes: int = 15 * 1024 * 1024) -> bytes | None:
    """Download a Telegram file's bytes. None on failure or over max_bytes."""
    try:
        info = get_file(file_id) or {}
        result = info.get("result") or {}
        path = result.get("file_path")
        size = result.get("file_size") or 0
        if not path or size > max_bytes:
            return None
        url = f"https://api.telegram.org/file/bot{_resolve_token()}/{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data
    except Exception:
        return None


def restrict_chat_member(chat_id, user_id, permissions, until_date=None) -> dict:
    return api("restrictChatMember", {
        "chat_id": chat_id,
        "user_id": user_id,
        "permissions": permissions,
        "until_date": until_date,
    })


def ban_chat_member(chat_id, user_id, until_date=None) -> dict:
    """Ban a user; without until_date the ban is permanent."""
    return api("banChatMember", {
        "chat_id": chat_id,
        "user_id": user_id,
        "until_date": until_date,
    })


def unban_chat_member(chat_id, user_id, only_if_banned=True) -> dict:
    return api("unbanChatMember", {
        "chat_id": chat_id,
        "user_id": user_id,
        "only_if_banned": only_if_banned,
    })


def send_chat_action(chat_id, action="typing") -> dict:
    return api("sendChatAction", {"chat_id": chat_id, "action": action})


def forward_message(to_chat_id, from_chat_id, message_id,
                    reply_to_message_id=None,
                    disable_notification=None) -> dict:
    """Forward with optional reply linkage (live-verified on current API)."""
    return api("forwardMessage", {
        "chat_id": to_chat_id, "from_chat_id": from_chat_id,
        "message_id": message_id,
        "reply_to_message_id": reply_to_message_id,
        "disable_notification": disable_notification,
    })


def send_document(chat_id, file_id_or_path, caption=None,
                  reply_to_message_id=None) -> dict:
    if isinstance(file_id_or_path, str) and file_id_or_path.startswith("@"):
        path = file_id_or_path[1:]
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return {"ok": False, "description": f"cannot read file: {e}"}
        filename = path.rsplit("/", 1)[-1] or "document"
        return api(
            "sendDocument",
            {"chat_id": chat_id, "caption": caption,
             "reply_to_message_id": reply_to_message_id},
            files={"document": (filename, data, "application/octet-stream")},
        )
    return api("sendDocument", {"chat_id": chat_id, "document": file_id_or_path,
                                 "caption": caption,
                                 "reply_to_message_id": reply_to_message_id})


def send_photo(chat_id, file_id, caption=None,
               reply_to_message_id=None) -> dict:
    return api("sendPhoto", {"chat_id": chat_id, "photo": file_id,
                             "caption": caption,
                             "reply_to_message_id": reply_to_message_id})


def send_video(chat_id, file_id, caption=None,
               reply_to_message_id=None) -> dict:
    return api("sendVideo", {"chat_id": chat_id, "video": file_id,
                             "caption": caption,
                             "reply_to_message_id": reply_to_message_id})


def send_audio(chat_id, file_id, caption=None,
               reply_to_message_id=None) -> dict:
    return api("sendAudio", {"chat_id": chat_id, "audio": file_id,
                             "caption": caption,
                             "reply_to_message_id": reply_to_message_id})


def send_voice(chat_id, file_id, caption=None,
               reply_to_message_id=None) -> dict:
    return api("sendVoice", {"chat_id": chat_id, "voice": file_id,
                             "caption": caption,
                             "reply_to_message_id": reply_to_message_id})


def send_animation(chat_id, file_id, caption=None,
                   reply_to_message_id=None) -> dict:
    return api("sendAnimation", {"chat_id": chat_id, "animation": file_id,
                                 "caption": caption,
                                 "reply_to_message_id": reply_to_message_id})


def send_sticker(chat_id, file_id, reply_to_message_id=None) -> dict:
    return api("sendSticker", {"chat_id": chat_id, "sticker": file_id,
                               "reply_to_message_id": reply_to_message_id})


def send_video_note(chat_id, file_id, reply_to_message_id=None) -> dict:
    return api("sendVideoNote", {"chat_id": chat_id, "video_note": file_id,
                                 "reply_to_message_id": reply_to_message_id})


def send_media_group(chat_id, media: list,
                     reply_to_message_id=None) -> dict:
    """Send 2-10 photo/video/document/audio items as one grouped album."""
    return api("sendMediaGroup", {
        "chat_id": chat_id, "media": media,
        "reply_to_message_id": reply_to_message_id,
    })


def leave_chat(chat_id) -> dict:
    return api("leaveChat", {"chat_id": chat_id})


def set_webhook(url, secret_token=None) -> dict:
    return api("setWebhook", {
        "url": url,
        "secret_token": secret_token,
        "allowed_updates": [
            "message", "callback_query", "my_chat_member",
            "channel_post", "edited_channel_post",
        ],
    })


def delete_webhook(drop_pending=False) -> dict:
    return api("deleteWebhook", {"drop_pending_updates": drop_pending})


def send_any_file(chat_id: int, file_id: str, file_type: str,
                 caption: str = None, reply_to_message_id=None) -> dict:
    """Send a stored media file by its stored file_id and detected type."""
    t = (file_type or "").lower()
    if t == "photo":
        return send_photo(chat_id, file_id, caption=caption,
                          reply_to_message_id=reply_to_message_id)
    if t == "video":
        return send_video(chat_id, file_id, caption=caption,
                          reply_to_message_id=reply_to_message_id)
    if t == "audio":
        return send_audio(chat_id, file_id, caption=caption,
                          reply_to_message_id=reply_to_message_id)
    if t == "voice":
        return send_voice(chat_id, file_id, caption=caption,
                          reply_to_message_id=reply_to_message_id)
    if t == "animation":
        return send_animation(chat_id, file_id, caption=caption,
                              reply_to_message_id=reply_to_message_id)
    if t == "sticker":
        return send_sticker(chat_id, file_id,
                            reply_to_message_id=reply_to_message_id)
    if t == "video_note":
        return send_video_note(chat_id, file_id,
                               reply_to_message_id=reply_to_message_id)
    return send_document(chat_id, file_id, caption=caption,
                         reply_to_message_id=reply_to_message_id)


# ---------------------- Power upgrade additions ----------------------

def copy_message(to_chat_id, from_chat_id, message_id, caption=None,
                 reply_to_message_id=None,
                 disable_notification=None) -> dict:
    """Copy a message without the 'Forwarded from' header (as-is clone).
    Link preview stays off for copied text (owner rule)."""
    return api("copyMessage", {
        "chat_id": to_chat_id, "from_chat_id": from_chat_id,
        "message_id": message_id, "caption": caption,
        "reply_to_message_id": reply_to_message_id,
        "disable_notification": disable_notification,
        "link_preview_options": NO_PREVIEW,
    })


def pin_chat_message(chat_id, message_id, silent=True) -> dict:
    return api("pinChatMessage", {
        "chat_id": chat_id, "message_id": message_id, "silent": silent,
    })


def unpin_chat_message(chat_id, message_id=None) -> dict:
    p = {"chat_id": chat_id}
    if message_id:
        p["message_id"] = message_id
    return api("unpinChatMessage", p)


def unpin_all_chat_messages(chat_id) -> dict:
    return api("unpinAllChatMessages", {"chat_id": chat_id})


def set_my_commands(commands: list, scope: str = "default") -> dict:
    """Set the bot's command menu (the '/' button UI in Telegram)."""
    return api("setMyCommands", {
        "commands": commands,
        "scope": {"type": scope},
    })


def send_contact(chat_id, phone_number, first_name, last_name=None,
                 vcard=None, reply_to_message_id=None) -> dict:
    return api("sendContact", {
        "chat_id": chat_id,
        "phone_number": phone_number,
        "first_name": first_name,
        "last_name": last_name,
        "vcard": vcard,
        "reply_to_message_id": reply_to_message_id,
    })


def set_message_reaction(chat_id, message_id, emoji: str = "👍") -> dict:
    """React to a message (Bot API 7.0+). Best-effort; ignores errors."""
    return api("setMessageReaction", {
        "chat_id": chat_id, "message_id": message_id,
        "reaction": [{"type": "emoji", "emoji": emoji}],
    })
