"""AI engine with multi-provider key rotation + failover.

Supported providers (loaded in this order; each becomes a pool of keys):
  - gemini:    uses Gemini generateContent, picks a random healthy key per request
  - deepseek:  uses DeepSeek chat-completions (openai-compatible)

Strategy:
  - Randomly pick a provider, then a random healthy key from that provider.
  - On any non-retryable error from a key, mark it dead for KEY_BACKOFF_SECONDS.
  - On 4xx/5xx that look retryable, try the next healthy key (or fall over
    to the other provider) before giving up.
  - Never print/log raw keys.
"""
from __future__ import annotations
import json
import random
import time
import urllib.error
import urllib.request

import config


class NoProvidersError(Exception):
    pass


# --- key pool ---

class _KeyPool:
    """One provider's key pool with random selection + per-key backoff."""

    def __init__(self, name: str, keys: list[str], backoff: int):
        self.name = name
        self.keys = list(keys)
        self._backoff_until: dict[str, float] = {}
        self._backoff = backoff

    def choose(self) -> str | None:
        if not self.keys:
            return None
        now = time.time()
        healthy = [k for k in self.keys if self._backoff_until.get(k, 0) <= now]
        if not healthy:
            # all backed off -> pick the one that frees up first
            soonest = min(self.keys, key=lambda k: self._backoff_until.get(k, 0))
            return soonest
        return random.choice(healthy)

    def report_dead(self, key: str) -> None:
        self._backoff_until[key] = time.time() + self._backoff

    def report_ok(self, key: str) -> None:
        self._backoff_until.pop(key, None)

    def status(self) -> list[dict]:
        now = time.time()
        out = []
        for i, k in enumerate(self.keys):
            until = self._backoff_until.get(k, 0)
            out.append({
                "index": i + 1,
                "masked": mask_key(k),
                "healthy": until <= now,
                "free_in": max(0, int(until - now)),
            })
        return out


def mask_key(k: str) -> str:
    if not k:
        return "—"
    s = str(k)
    if len(s) <= 8:
        return s[:4] + "..."
    return f"{s[:6]}...{s[-4:]}"


# --- providers ---

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEEPSEEK_BASE = "https://api.deepseek.com/v1/chat/completions"


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> tuple[int, dict, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), ""
        except Exception:
            return e.code or 0, {}, str(e)
    except urllib.error.URLError as e:
        return 0, {}, f"URLError: {e}"
    except Exception as e:  # noqa: BLE001
        return 0, {}, f"error: {e}"


def _extract_gemini_text(result: dict) -> str:
    parts: list[str] = []
    for cand in result.get("candidates") or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            t = part.get("text")
            if t:
                parts.append(t)
    if parts:
        return "".join(parts)
    # safety block / no candidates
    pf = result.get("promptFeedback") or {}
    return ""


def _gemini_call(key: str, model: str, prompt: str, system: str | None,
                 max_tokens: int, timeout: int) -> tuple[str, str]:
    """Return (text, error). Empty text + non-empty error means failure."""
    full_prompt = (f"{system.strip()}\n\n{prompt.strip()}" if system else prompt)
    body = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    url = f"{GEMINI_BASE}/{model}:generateContent?key={key}"
    status, payload, err = _post_json(url, body, {"Content-Type": "application/json"}, timeout)
    if err:
        return "", err
    if status >= 400:
        msg = (payload.get("error") or {}).get("message") or f"http {status}"
        return "", msg
    text = _extract_gemini_text(payload)
    if not text:
        return "", "empty response"
    return text, ""


def _deepseek_call(key: str, model: str, prompt: str, system: str | None,
                   max_tokens: int, timeout: int) -> tuple[str, str]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": 0.7}
    status, payload, err = _post_json(DEEPSEEK_BASE, body, headers, timeout)
    if err:
        return "", err
    if status >= 400:
        msg = (payload.get("error") or {}).get("message") or f"http {status}"
        return "", msg
    choices = payload.get("choices") or []
    if not choices:
        return "", "no choices"
    text = ((choices[0].get("message") or {}).get("content")) or ""
    return text, ""


# --- top-level pool: gemini + deepseek ---

class _Provider:
    def __init__(self, name: str, model: str, keys: list[str]):
        self.name = name
        self.model = model
        self.pool = _KeyPool(name, keys, config.KEY_BACKOFF_SECONDS)

    def call(self, prompt: str, system: str | None, max_tokens: int, timeout: int) -> tuple[str, str, str]:
        """Return (text, provider_name, error). Randomly pick a healthy key; one attempt."""
        key = self.pool.choose()
        if not key:
            return "", self.name, "no keys configured"
        if self.name == "gemini":
            text, err = _gemini_call(key, self.model, prompt, system, max_tokens, timeout)
        elif self.name == "deepseek":
            text, err = _deepseek_call(key, self.model, prompt, system, max_tokens, timeout)
        else:
            text, err = "", f"unknown provider {self.name}"
        if err:
            self.pool.report_dead(key)
        else:
            self.pool.report_ok(key)
        return text, self.name, err


class AIRouter:
    def __init__(self):
        self.providers: list[_Provider] = []
        if config.GEMINI_KEYS:
            self.providers.append(_Provider("gemini", config.DEFAULT_MODEL, config.GEMINI_KEYS))
        if config.DEEPSEEK_KEY:
            self.providers.append(_Provider("deepseek", "deepseek-chat", [config.DEEPSEEK_KEY]))

    def has_any(self) -> bool:
        return any(p.pool.keys for p in self.providers)

    def provider_count(self) -> int:
        return sum(1 for p in self.providers if p.pool.keys)

    def key_count(self) -> int:
        return sum(len(p.pool.keys) for p in self.providers)

    def status(self) -> list[dict]:
        rows = []
        for p in self.providers:
            for row in p.pool.status():
                row["provider"] = p.name
                row["model"] = p.model
                rows.append(row)
        return rows

    def generate(self, prompt: str, system: str | None = None,
                 max_tokens: int = 1024, timeout: int | None = None) -> str:
        """Try random providers in random key order, with failover."""
        if not self.providers:
            raise NoProvidersError("no AI providers configured")
        if timeout is None:
            timeout = config.AI_REQUEST_TIMEOUT
        # Build a randomized attempt order: shuffle providers, then for each
        # provider try up to N keys (where N = # of its keys) before moving on.
        providers = list(self.providers)
        random.shuffle(providers)
        last_err = "no providers"
        for prov in providers:
            # Within a provider, do up to len(keys) attempts.
            attempts = max(1, len(prov.pool.keys))
            for _ in range(attempts):
                key = prov.pool.choose()
                if not key:
                    break
                if prov.name == "gemini":
                    text, err = _gemini_call(key, prov.model, prompt, system, max_tokens, timeout)
                else:
                    text, err = _deepseek_call(key, prov.model, prompt, system, max_tokens, timeout)
                if not err:
                    prov.pool.report_ok(key)
                    return text
                prov.pool.report_dead(key)
                last_err = f"{prov.name}: {err}"
        raise RuntimeError(f"all AI providers failed: {last_err}")


_ROUTER: AIRouter | None = None


def get_router() -> AIRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = AIRouter()
    return _ROUTER


def list_gemini_models() -> list[str]:
    """Return Gemini model short names (uses first healthy key)."""
    pool = _KeyPool("gemini", config.GEMINI_KEYS, 0) if config.GEMINI_KEYS else None
    if not pool or not pool.keys:
        return []
    key = pool.choose()
    if not key:
        return []
    url = f"{GEMINI_BASE}?key={key}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    names: list[str] = []
    for m in data.get("models") or []:
        name = str(m.get("name", ""))
        if name.startswith("models/"):
            name = name[len("models/"):]
        if "gemini" in name.lower():
            names.append(name)
    return sorted(set(names))


def pick_default_model(models: list[str]) -> str:
    if not models:
        return config.DEFAULT_MODEL
    if "gemini-2.5-flash-lite" in models:
        return "gemini-2.5-flash-lite"
    for m in models:
        if "flash-lite" in m:
            return m
    for m in models:
        if "flash" in m and "image" not in m:
            return m
    return models[0]
