"""Configuration for RK Agent Bot — env-driven, Render-ready.

All secrets come from env vars. Same code runs locally and on Render.
"""
from __future__ import annotations
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _split(name: str) -> list[str]:
    """Split a multi-line / comma env var into a clean list."""
    raw = os.environ.get(name, "")
    out: list[str] = []
    for part in raw.replace("\r", "\n").split("\n"):
        part = part.strip()
        if not part:
            continue
        if "," in part:
            for sub in part.split(","):
                sub = sub.strip()
                if sub:
                    out.append(sub)
        else:
            out.append(part)
    return out


# --- Project paths ---
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_KEYS = _split("GEMINI_KEYS")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "").strip()
MONGODB_URI = os.environ.get("MONGODB_URI", "").strip()

# --- People / roles ---
OWNER_CHAT_ID = _int("OWNER_CHAT_ID", 0)
ADMIN_CHAT_ID = _int("ADMIN_CHAT_ID", 0)  # 0 -> falls back to owner

# --- Run mode ---
PORT = _int("PORT", 10000)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = (
    os.environ.get("WEBHOOK_SECRET", "rk-secret-2026").strip() or "rk-secret-2026"
)

# --- Behaviour ---
DB_NAME = os.environ.get("DB_NAME", "rk_agent").strip() or "rk_agent"
DEFAULT_MODEL = (
    os.environ.get("DEFAULT_MODEL", "gemini-2.5-flash-lite").strip()
    or "gemini-2.5-flash-lite"
)
POLL_TIMEOUT = _int("POLL_TIMEOUT", 50)
PRUNE_EVERY_HOURS = _int("PRUNE_EVERY_HOURS", 12)
CHANNEL_RECONCILE_HOURS = _int("CHANNEL_RECONCILE_HOURS", 6)
PRUNE_IMPORTANCE_FLOOR = float(os.environ.get("PRUNE_IMPORTANCE_FLOOR", "0.25"))
MESSAGE_TTL_DAYS = _int("MESSAGE_TTL_DAYS", 60)
POST_TTL_DAYS = _int("POST_TTL_DAYS", 120)
FILE_POST_TTL_DAYS = _int("FILE_POST_TTL_DAYS", 240)
KEY_BACKOFF_SECONDS = _int("KEY_BACKOFF_SECONDS", 600)
AI_REQUEST_TIMEOUT = _int("AI_REQUEST_TIMEOUT", 45)