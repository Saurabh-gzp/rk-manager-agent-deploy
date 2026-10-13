"""Configuration for RK Agent Bot — env-driven, Render-ready.

All secrets come from env vars. Same code runs locally and on Render.
"""
from __future__ import annotations
import os
import secrets


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
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)

# --- Behaviour ---
DB_NAME = os.environ.get("DB_NAME", "rk_agent").strip() or "rk_agent"
DEFAULT_MODEL = (
    os.environ.get("DEFAULT_MODEL", "gemini-3.5-flash-lite").strip()
    or "gemini-3.5-flash-lite"
)
POLL_TIMEOUT = _int("POLL_TIMEOUT", 50)
PRUNE_EVERY_HOURS = _int("PRUNE_EVERY_HOURS", 12)
CHANNEL_RECONCILE_HOURS = _int("CHANNEL_RECONCILE_HOURS", 6)
PRUNE_IMPORTANCE_FLOOR = float(os.environ.get("PRUNE_IMPORTANCE_FLOOR", "0.25"))
MESSAGE_TTL_DAYS = _int("MESSAGE_TTL_DAYS", 60)
POST_TTL_DAYS = _int("POST_TTL_DAYS", 120)
FILE_POST_TTL_DAYS = _int("FILE_POST_TTL_DAYS", 240)
KEY_BACKOFF_SECONDS = _int("KEY_BACKOFF_SECONDS", 600)
AI_REQUEST_TIMEOUT = _int("AI_REQUEST_TIMEOUT", 25)
# SPEED: chat replies need a few sentences, not 1024 tokens. Output length
# drives generation time — this cap cuts several seconds off every answer.
ANSWER_MAX_TOKENS = _int("ANSWER_MAX_TOKENS", 350)
# SPEED: the reply-gate is a one-word YES/NO — never let a stuck gate call
# hold the answer for the full AI_REQUEST_TIMEOUT.
GATE_TIMEOUT_SECONDS = _int("GATE_TIMEOUT_SECONDS", 10)
# User ki request: 1 hafta se zyada idle user ki memory delete hone se
# pehle owner ko ek baar DM notice zaroor jata hai.
USER_MEMORY_IDLE_DAYS = max(1, _int("USER_MEMORY_IDLE_DAYS", 7))
# v3.5 MEMORY MANAGEMENT (owner ka live concern: 'data bahot bada ho
# jayega to agent slow/stuck ho jayega'):
#   - ek chat ke raw messages ka HARD CAP (iske upar wale purane messages
#     compact LLM digest — short summary — ban jaate hain, community
#     facts digest me zinda rehte hain; importance>=0.5 messages kabhi
#     delete nahi hote)
#   - per-user Q&A history ka HARD CAP
#   - DIGEST_ENABLED=0 karne par digest-generation band (compaction bhi
#     band — sirf importance/TTL wala purana prune chalega)
MEMORY_MAX_MESSAGES_PER_CHAT = _int("MEMORY_MAX_MESSAGES_PER_CHAT", 2000)
MEMORY_MAX_QA_PER_USER = _int("MEMORY_MAX_QA_PER_USER", 400)
DIGEST_ENABLED = _int("DIGEST_ENABLED", 1)
# --- v4.0 TRUTH LAYER (real-time channel verification) ---
# Public channel ke t.me/<user>/<N> posts bina login probe hote hain;
# confirmed-deleted posts DB se purge ho jaate hain. VERIFY_ENABLED=0
# karne par answer-link verification + background sweep dono band.
VERIFY_ENABLED = _int("VERIFY_ENABLED", 1)
VERIFY_SWEEP_MINUTES = max(5, _int("VERIFY_SWEEP_MINUTES", 30))
VERIFY_SWEEP_BATCH = _int("VERIFY_SWEEP_BATCH", 60)