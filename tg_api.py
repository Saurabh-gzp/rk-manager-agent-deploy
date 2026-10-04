"""Telegram Bot API client (urllib only — no external deps)."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "https://api.telegram.org"
MAX_TEXT = 4096


def _resolve_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


def _tg_url(method: str) -> str:
    token = _resolve_token()
    url = f"https://api.telegram.org/bot{token}/{method}"
    host = urllib.parse.urlparse(url).hostname or ""
    if host != "api.telegram.org":
        raise ValueError(f"refusing non-Telegram host: {host}")
    return url


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


def api(method: str, params: dict | None = None, files: dict | None = None, timeout: int = 60) -> dict:
    """Call a Bot API method. Never raises."""
    params = params or {}
    url = _tg_url(method)
    if files:
        data, ctype = _multipart(params, files)
        headers = {"Content-Type": ctype}
    else:
        form = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            form[k] = str(v)
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
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
                 disable_web_page_preview=None) -> dict:
    text = str(text or "")
    chunks = [text[i:i + MAX_TEXT] for i in range(0, max(len(text), 1), MAX_TEXT)]
    first = None
    for idx, chunk in enumerate(chunks):
        p = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
            "reply_markup": reply_markup if idx == 0 else None,
        }
        r = api("sendMessage", p)
        if idx == 0:
            first = r
    return first or {}


def edit_message_text(chat_id, message_id, text, reply_markup=None,
                      parse_mode=None) -> dict:
    return api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
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


def get_chat_member_count(chat_id) -> int:
    r = api("getChatMemberCount", {"chat_id": chat_id})
    try:
        return int(r.get("result", 0)) if isinstance(r, dict) and r.get("ok") else 0
    except Exception:
        return 0


def send_chat_action(chat_id, action="typing") -> dict:
    return api("sendChatAction", {"chat_id": chat_id, "action": action})


def forward_message(to_chat_id, from_chat_id, message_id) -> dict:
    return api("forwardMessage", {
        "chat_id": to_chat_id, "from_chat_id": from_chat_id, "message_id": message_id,
    })


def send_document(chat_id, file_id_or_path, caption=None) -> dict:
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
            {"chat_id": chat_id, "caption": caption},
            files={"document": (filename, data, "application/octet-stream")},
        )
    return api("sendDocument", {"chat_id": chat_id, "document": file_id_or_path, "caption": caption})


def send_photo(chat_id, file_id, caption=None) -> dict:
    return api("sendPhoto", {"chat_id": chat_id, "photo": file_id, "caption": caption})


def send_video(chat_id, file_id, caption=None) -> dict:
    return api("sendVideo", {"chat_id": chat_id, "video": file_id, "caption": caption})


def send_audio(chat_id, file_id, caption=None) -> dict:
    return api("sendAudio", {"chat_id": chat_id, "audio": file_id, "caption": caption})


def send_voice(chat_id, file_id, caption=None) -> dict:
    return api("sendVoice", {"chat_id": chat_id, "voice": file_id, "caption": caption})


def leave_chat(chat_id) -> dict:
    return api("leaveChat", {"chat_id": chat_id})


def set_webhook(url) -> dict:
    return api("setWebhook", {"url": url, "allowed_updates": ["message", "callback_query", "my_chat_member", "channel_post", "edited_channel_post"]})


def delete_webhook(drop_pending=False) -> dict:
    return api("deleteWebhook", {"drop_pending_updates": drop_pending})


def send_any_file(chat_id: int, file_id: str, file_type: str, caption: str = None) -> dict:
    """Send a stored media file by its stored file_id and detected type."""
    t = (file_type or "").lower()
    if t == "photo":
        return send_photo(chat_id, file_id, caption=caption)
    if t == "video":
        return send_video(chat_id, file_id, caption=caption)
    if t == "audio":
        return send_audio(chat_id, file_id, caption=caption)
    if t == "voice":
        return send_voice(chat_id, file_id, caption=caption)
    return send_document(chat_id, file_id, caption=caption)
