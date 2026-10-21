"""AI engine with multi-provider key rotation + failover + owner-selected routing.

Supported providers (each becomes a pool of keys):
  - gemini:   Gemini generateContent — TEXT + VISION (photo/voice/video)
  - mercury:  Inception Labs Mercury 3 — OpenAI-compatible, TEXT-ONLY
              (blazing fast diffusion LLM; cannot see photos/audio)
  - deepseek: DeepSeek chat-completions (openai-compatible), TEXT-ONLY

Routing (v4.1 MULTI-PROVIDER):
  - The owner picks the active TEXT provider+model and the active VISION
    provider+model from the owner panel (stored in DB state).
  - generate()     -> selected text provider first, then failover to the
                      other text-capable providers (bot never goes down
                      because one provider died).
  - generate_mm()  -> selected vision provider+model first, then the other
                      vision-capable providers (photo/audio/video messages
                      auto-route here, so a text-only Mercury selection
                      still gives users a real media answer).
  - On any non-retryable error from a key, mark it dead for KEY_BACKOFF_SECONDS.
  - Never print/log raw keys.
"""
from __future__ import annotations
import json
import random
import re
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
        return "-"
    s = str(k)
    if len(s) <= 8:
        return s[:4] + "..."
    return f"{s[:6]}...{s[-4:]}"


# --- providers ---

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEEPSEEK_BASE = "https://api.deepseek.com/v1/chat/completions"
MERCURY_BASE = "https://api.inceptionlabs.ai/v1/chat/completions"
MERCURY_MODELS_URL = "https://api.inceptionlabs.ai/v1/models"

# Owner-facing labels (panel me dikhta hai).
PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "mercury": "Mercury 3 (Inception)",
    "deepseek": "DeepSeek",
}


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> tuple[int, dict, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            return e.code or 0, json.loads(e.read().decode("utf-8")), ""
        except Exception:
            return e.code or 0, {}, str(e)
    except urllib.error.URLError as e:
        return 0, {}, f"URLError: {e}"
    except Exception as e:  # noqa: BLE001
        return 0, {}, f"error: {e}"


def _get_json(url: str, headers: dict, timeout: int) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            return e.code or 0, json.loads(e.read().decode("utf-8")), ""
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
    return ""


def _gemini_post(key: str, model: str, body: dict, timeout: int) -> tuple[str, str]:
    """POST a generateContent body. Return (text, error)."""
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


# SPEED: Gemini 2.5+/3.x "flash" models can burn seconds on hidden
# chain-of-thought before the first visible token. thinkingBudget=0 turns
# it off. If a model/endpoint rejects the field, we flip the flag off for
# the process lifetime and retry once WITHOUT it — self-healing, zero risk.
_THINKING_OFF_OK = True


def _param_rejected(err: str) -> bool:
    e = (err or "").lower()
    return any(s in e for s in (
        "invalid", "unknown name", "unsupported", "thinking", "bad request"))


def _gemini_call(key: str, model: str, prompt: str, system: str | None,
                 max_tokens: int, timeout: int) -> tuple[str, str]:
    """Return (text, error). Empty text + non-empty error means failure."""
    global _THINKING_OFF_OK
    full_prompt = (f"{system.strip()}\n\n{prompt.strip()}" if system else prompt)
    body = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    if _THINKING_OFF_OK:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    text, err = _gemini_post(key, model, body, timeout)
    if err and _THINKING_OFF_OK and _param_rejected(err):
        _THINKING_OFF_OK = False  # never send it again this process
        body["generationConfig"].pop("thinkingConfig", None)
        text, err = _gemini_post(key, model, body, timeout)
    return text, err


def _gemini_call_mm(key: str, model: str, prompt: str, system: str | None,
                    max_tokens: int, timeout: int,
                    media: list[dict] | None) -> tuple[str, str]:
    """Multimodal generateContent. media items: {mime_type, data (base64), kind}.

    Mercury/DeepSeek have no vision/audio — this is Gemini-only by design.
    """
    global _THINKING_OFF_OK
    parts: list[dict] = []
    if system and system.strip():
        parts.append({"text": system.strip()})
    parts.append({"text": prompt.strip()})
    for m in media or []:
        mt, data = m.get("mime_type"), m.get("data")
        if mt and data:
            # REST wire format is camelCase (inlineData/mimeType).
            parts.append({"inlineData": {"mimeType": mt, "data": data}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    if _THINKING_OFF_OK:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    text, err = _gemini_post(key, model, body, timeout)
    if err and _THINKING_OFF_OK and _param_rejected(err):
        _THINKING_OFF_OK = False
        body["generationConfig"].pop("thinkingConfig", None)
        text, err = _gemini_post(key, model, body, timeout)
    return text, err


def _openai_chat_call(base: str, key: str, model: str, prompt: str,
                      system: str | None, max_tokens: int, timeout: int) -> tuple[str, str]:
    """Shared openai-compatible chat.completions call (deepseek / mercury)."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": 0.7}
    status, payload, err = _post_json(base, body, headers, timeout)
    if err:
        return "", err
    if status >= 400:
        msg = ((payload.get("error") or {}).get("message")
               or payload.get("message") or f"http {status}")
        return "", msg
    choices = payload.get("choices") or []
    if not choices:
        return "", "no choices"
    text = ((choices[0].get("message") or {}).get("content")) or ""
    return text, ""


def _deepseek_call(key: str, model: str, prompt: str, system: str | None,
                   max_tokens: int, timeout: int) -> tuple[str, str]:
    return _openai_chat_call(DEEPSEEK_BASE, key, model, prompt, system,
                             max_tokens, timeout)


def _mercury_call(key: str, model: str, prompt: str, system: str | None,
                  max_tokens: int, timeout: int) -> tuple[str, str]:
    """Mercury (Inception Labs) chat call — OpenAI-compatible, TEXT-ONLY.

    Mercury ek diffusion LLM hai: 1000+ tokens/sec, 1M context (mercury-3).
    reasoning_effort speed vs quality control karta hai (low/medium/high).

    v4.1.3 FIX: Mercury apne internal reasoning tokens bhi max_tokens budget
    me se kharch karta hai — chhote budget par content:null +
    finish_reason:"length" aa jata tha, jise caller "success" samajh leta tha
    (empty jawab, no failover). Ab ek self-healing retry bade budget ke saath
    hota hai; phir bhi khali ho to error return hota hai failover ke liye.
    """
    def _one(budget: int) -> tuple[str, str, str]:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {key}"}
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        body = {"model": model, "messages": msgs, "max_tokens": budget,
                "temperature": 0.7}
        effort = (config.MERCURY_REASONING or "medium").lower()
        if effort in ("low", "medium", "high"):
            body["reasoning_effort"] = effort
        status, payload, err = _post_json(MERCURY_BASE, body, headers, timeout)
        if err:
            return "", "", err
        if status >= 400:
            msg = ((payload.get("error") or {}).get("message")
                   or payload.get("message") or f"http {status}")
            return "", "", msg
        choices = payload.get("choices") or []
        if not choices:
            return "", "", "no choices"
        text = ((choices[0].get("message") or {}).get("content")) or ""
        finish = str(choices[0].get("finish_reason") or "")
        return text, finish, ""

    text, finish, err = _one(max_tokens)
    if not err and not text and finish == "length" and max_tokens < 1024:
        # reasoning ne budget kha liya — retry with headroom (max 1024).
        text, finish, err = _one(min(1024, max(2 * max_tokens, 512)))
    if err:
        return "", err
    if not text:
        return "", "empty response (mercury reasoning budget exhausted)"
    return text, ""


def _provider_call(provider: str, key: str, model: str, prompt: str,
                   system: str | None, max_tokens: int, timeout: int,
                   media: list[dict] | None = None) -> tuple[str, str]:
    """Dispatch one call to the right provider. media => gemini mm."""
    if provider == "gemini":
        if media:
            return _gemini_call_mm(key, model, prompt, system, max_tokens,
                                   timeout, media)
        return _gemini_call(key, model, prompt, system, max_tokens, timeout)
    if provider == "mercury":
        return _mercury_call(key, model, prompt, system, max_tokens, timeout)
    if provider == "deepseek":
        return _deepseek_call(key, model, prompt, system, max_tokens, timeout)
    return "", f"unknown provider {provider}"


def test_key(provider: str, key: str, model: str) -> tuple[str, str]:
    """Owner-panel key test: one tiny request through this provider."""
    return _provider_call(provider, key, model, "Reply with exactly: ok",
                           None, 256, 30)


# --- owner selection (DB state) ---

def _state_get(k: str, default=None):
    """DB state read (never raises — tests/local runs without DB work fine)."""
    try:
        from db import get_state
        return get_state(k, default)
    except Exception:
        return default


def _state_save(k: str, v) -> None:
    from db import save_state
    save_state(k, v)


# Selection state keys (owner panel inhe set karta hai):
TEXT_PROVIDER_KEY = "text_provider"
VISION_PROVIDER_KEY = "vision_provider"
VISION_MODEL_KEY = "vision_model"
# Per-provider saved TEXT models — provider switch karne par har provider
# apna last-chosen model yaad rakhta hai:
_TEXT_MODEL_KEYS = {
    "gemini": "text_model_gemini",
    "mercury": "text_model_mercury",
    "deepseek": "text_model_deepseek",
}


def _fallback_text_model(provider: str) -> str:
    if provider == "gemini":
        # legacy "model" state = purana single-model chooser → gemini ka model
        return (_state_get("model") or config.DEFAULT_MODEL or "gemini-3.5-flash-lite")
    if provider == "mercury":
        return config.MERCURY_MODEL or "mercury-3"
    return "deepseek-chat"


def get_text_model(provider: str) -> str:
    key = _TEXT_MODEL_KEYS.get(provider)
    if key:
        v = _state_get(key)
        if v:
            return str(v)
    return _fallback_text_model(provider)


def set_text_model(provider: str, model: str) -> None:
    key = _TEXT_MODEL_KEYS.get(provider)
    if key:
        _state_save(key, model)
    if provider == "gemini":
        # keep legacy single-model state in sync (older panel screens)
        _state_save("model", model)


def set_text_provider(provider: str) -> None:
    _state_save(TEXT_PROVIDER_KEY, provider)


def set_vision_selection(provider: str, model: str) -> None:
    _state_save(VISION_PROVIDER_KEY, provider)
    _state_save(VISION_MODEL_KEY, model)


# --- top-level pool: gemini + mercury + deepseek ---

class _Provider:
    """name/model/pool + kind: 'vision' providers media dekh sakte hain,
    'text' providers sirf text (media inpar kabhi route nahi hota)."""

    def __init__(self, name: str, model: str, keys: list[str], kind: str):
        self.name = name
        self.model = model
        self.kind = kind  # "vision" | "text"
        self.pool = _KeyPool(name, keys, config.KEY_BACKOFF_SECONDS)

    def current_model(self) -> str:
        """Live effective model: DB selection → instance default."""
        if self.name == "gemini":
            return get_text_model("gemini") or self.model
        if self.name == "mercury":
            return get_text_model("mercury") or self.model
        return get_text_model("deepseek") or self.model

    def call(self, prompt: str, system: str | None, max_tokens: int,
             timeout: int) -> tuple[str, str, str]:
        """One attempt with a random healthy key. (text only)"""
        key = self.pool.choose()
        if not key:
            return "", self.name, "no keys configured"
        text, err = _provider_call(self.name, key, self.current_model(),
                                   prompt, system, max_tokens, timeout)
        if err:
            self.pool.report_dead(key)
        else:
            self.pool.report_ok(key)
        return text, self.name, err


class AIRouter:
    def __init__(self):
        self.providers: list[_Provider] = []
        if config.GEMINI_KEYS:
            self.providers.append(_Provider(
                "gemini", config.DEFAULT_MODEL, config.GEMINI_KEYS, "vision"))
        if config.MERCURY_KEYS:
            self.providers.append(_Provider(
                "mercury", config.MERCURY_MODEL, config.MERCURY_KEYS, "text"))
        if config.DEEPSEEK_KEY:
            self.providers.append(_Provider(
                "deepseek", "deepseek-chat", [config.DEEPSEEK_KEY], "text"))

    # --- lookups ---

    def prov(self, name: str) -> _Provider | None:
        return next((p for p in self.providers if p.name == name), None)

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
                row["model"] = p.current_model()
                rows.append(row)
        return rows

    def text_providers(self) -> list[str]:
        """Available text-capable provider names (owner panel list)."""
        return [p.name for p in self.providers if p.pool.keys]

    def vision_providers(self) -> list[str]:
        """Available vision-capable provider names (media answers)."""
        return [p.name for p in self.providers
                if p.kind == "vision" and p.pool.keys]

    # --- owner selection ---

    def get_text_selection(self) -> tuple[str, str]:
        """(provider, model) currently answering text messages."""
        avail = self.text_providers()
        if not avail:
            raise NoProvidersError("no AI providers configured")
        sel = str(_state_get(TEXT_PROVIDER_KEY) or "")
        # v4.1 default: Mercury 3 primary (owner ka isliye add kiya —
        # Gemini se zyada powerful+fast), warna pehla available.
        if sel not in avail:
            sel = "mercury" if "mercury" in avail else avail[0]
        return sel, (get_text_model(sel) or _fallback_text_model(sel))

    def get_vision_selection(self) -> tuple[str, str]:
        """(provider, model) currently answering photo/audio/video."""
        avail = self.vision_providers()
        if not avail:
            raise NoProvidersError("no vision-capable provider configured "
                                  "(photo/audio ke liye Gemini key chahiye)")
        sel = str(_state_get(VISION_PROVIDER_KEY) or "")
        if sel not in avail:
            sel = "gemini" if "gemini" in avail else avail[0]
        model = str(_state_get(VISION_MODEL_KEY) or "")
        if not model:
            if sel == "gemini":
                model = str(_state_get("model") or config.DEFAULT_VISION_MODEL
                            or "gemini-3.5-flash-lite")
            else:
                model = self.prov(sel).model if self.prov(sel) else ""
        return sel, model

    # --- generation ---

    def generate(self, prompt: str, system: str | None = None,
                 max_tokens: int = 1024, timeout: int | None = None) -> str:
        """Owner-selected text provider first, then failover to others."""
        if not self.providers:
            raise NoProvidersError("no AI providers configured")
        if timeout is None:
            timeout = config.AI_REQUEST_TIMEOUT
        try:
            sel_prov, sel_model = self.get_text_selection()
        except NoProvidersError:
            raise
        ordered: list[tuple[_Provider, str]] = []
        p = self.prov(sel_prov)
        if p:
            ordered.append((p, sel_model))
        for other in self.providers:
            if other.name != sel_prov and other.pool.keys:
                ordered.append((other, other.current_model()))
        last_err = "no providers"
        for prov, model in ordered:
            attempts = max(1, len(prov.pool.keys))
            for _ in range(attempts):
                key = prov.pool.choose()
                if not key:
                    break
                text, err = _provider_call(prov.name, key, model, prompt,
                                           system, max_tokens, timeout)
                if not err:
                    prov.pool.report_ok(key)
                    return text
                prov.pool.report_dead(key)
                last_err = f"{prov.name}: {err}"
        raise RuntimeError(f"all AI providers failed: {last_err}")

    def generate_mm(self, prompt: str, media: list[dict],
                    system: str | None = None, max_tokens: int = 1024,
                    timeout: int | None = None) -> str:
        """Multimodal generate — selected VISION provider+model first.

        Photo/voice/video messages auto yahan aate hain, isliye text-only
        provider (jaise Mercury) select hone par bhi user ko media ka
        jawab vision provider (Gemini) se milta hai.
        """
        vision_provs = [p for p in self.providers
                        if p.kind == "vision" and p.pool.keys]
        if not vision_provs:
            raise NoProvidersError("no vision provider configured for media")
        if timeout is None:
            timeout = config.AI_REQUEST_TIMEOUT
        try:
            sel_prov, sel_model = self.get_vision_selection()
        except NoProvidersError:
            raise
        ordered: list[tuple[_Provider, str]] = []
        p = self.prov(sel_prov)
        if p and p.kind == "vision":
            ordered.append((p, sel_model))
        for other in vision_provs:
            if other.name != sel_prov:
                ordered.append((other, other.model))
        last_err = "no vision providers"
        for prov, model in ordered:
            attempts = max(1, len(prov.pool.keys))
            for _ in range(attempts):
                key = prov.pool.choose()
                if not key:
                    break
                text, err = _gemini_call_mm(key, model, prompt, system,
                                            max_tokens, timeout, media)
                if not err:
                    prov.pool.report_ok(key)
                    return text
                prov.pool.report_dead(key)
                last_err = f"{prov.name}: {err}"
        raise RuntimeError(f"all vision providers failed: {last_err}")


_ROUTER: AIRouter | None = None


def get_router() -> AIRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = AIRouter()
    return _ROUTER


# --- model listings (owner panel) ---

def list_gemini_models() -> list[str]:
    """Return cleaned Gemini model short names (uses first healthy key).

    Filters out specialized variants (image / tts / preview / transcribe /
    nano / omni / lyria / deep-research) so the owner panel only shows
    the actual chat models.
    """
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
    skip_suffixes = (
        "-image", "-tts", "-preview", "-transcribe", "-nano",
        "-omni", "-lyria", "-deep-research", "-customtools",
    )
    for m in data.get("models") or []:
        name = str(m.get("name", ""))
        if name.startswith("models/"):
            name = name[len("models/"):]
        nl = name.lower()
        if "gemini" not in nl:
            continue
        methods = m.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        if any(nl.endswith(s) for s in skip_suffixes):
            continue
        names.append(name)

    def _sort_key(n: str) -> tuple:
        nl = n.lower()
        m = re.search(r"(\d+\.\d+)", n)
        ver = float(m.group(1)) if m else 0.0
        is_lite = 0 if "flash-lite" in nl else 1
        is_flash = 0 if "flash" in nl else 1
        is_pro = 0 if "pro" in nl else 1
        return (-ver, is_lite, is_flash, is_pro, n)

    return sorted(set(names), key=_sort_key)[:25]


# Mercury ke chat models (Inception Labs /v1/models). FIM/edit/decide
# variants filter ho jaate hain — sirf chat.completions wale dikhate hain.
_MERCURY_STATIC_MODELS = ["mercury-3", "mercury-2.5"]


def list_mercury_models() -> list[str]:
    """Live Mercury model ids; static fallback if the listing fails."""
    if not config.MERCURY_KEYS:
        return []
    key = config.MERCURY_KEYS[0]
    status, payload, err = _get_json(
        MERCURY_MODELS_URL, {"Authorization": f"Bearer {key}"}, 20)
    if err or status >= 400 or not payload:
        return list(_MERCURY_STATIC_MODELS)
    out: list[str] = []
    for m in payload.get("data") or []:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        nl = mid.lower()
        # chat-completions only; decide/fim/edit variants skip
        if any(s in nl for s in ("decide", "fim", "edit", "voice", "coder")):
            continue
        endpoints = m.get("supported_endpoints") or []
        if endpoints and "chat.completions" not in endpoints:
            continue
        out.append(mid)

    def _rank(n: str) -> tuple:
        m = re.search(r"(\d+(?:\.\d+)?)", n)
        ver = float(m.group(1)) if m else 0.0
        return (-ver, n)

    out = sorted(set(out), key=_rank)
    return out or list(_MERCURY_STATIC_MODELS)


def list_models_for(provider: str) -> list[str]:
    """Owner-panel helper: model list for a provider."""
    if provider == "gemini":
        return list_gemini_models() or [config.DEFAULT_MODEL]
    if provider == "mercury":
        return list_mercury_models() or [config.MERCURY_MODEL]
    if provider == "deepseek":
        return ["deepseek-chat", "deepseek-reasoner"]
    return []


def pick_default_model(models: list[str]) -> str:
    if not models:
        return config.DEFAULT_MODEL
    if "gemini-3.5-flash-lite" in models:
        return "gemini-3.5-flash-lite"
    if "gemini-2.5-flash-lite" in models:
        return "gemini-2.5-flash-lite"
    for m in models:
        if "flash-lite" in m:
            return m
    for m in models:
        if "flash" in m and "image" not in m:
            return m
    return models[0]
