"""Q&A + content delivery engine.

Uses the AI router (multi-provider with key rotation) over knowledge.search().
If grounded answer isn't possible, escalates to admin first, then owner.
"""
from __future__ import annotations
from typing import Any

import ai_engine
import config
import knowledge
import tg_api


SYSTEM_PROMPT = (
    "You are RK Manager — the AI agent for a Telegram gaming community "
    "(BGMI mods, loaders, APKs, tech support). "
    "Below is context learned from the channel + group. Use it as background "
    "knowledge, but ALWAYS answer in your own words — NEVER copy-paste lines "
    "from the context verbatim, even if the user seems to quote them. "
    "If the context does not contain the answer, use your own general "
    "knowledge instead of refusing. "
    "Never invent a specific user's past messages or personal history: if "
    "asked about someone's past conversation and the context has no record "
    "of it, say naturally in the user's language that you don't have that "
    "record — do not refuse with a canned line. "
    "Reply in the user's language (Hindi, Hinglish ok). Be concise, friendly "
    "and chat-like — like a group member, not a robot."
)

GENERAL_PROMPT = (
    "You are RK Manager, a concise Telegram assistant. Answer ordinary "
    "general-knowledge questions in the user's language. Never claim to know "
    "this group's live membership, permissions, messages, or a user's past "
    "conversation unless that information is explicitly present in context. "
    "If asked about a person's past messages and there is no matching context, "
    "say you do not have that history."
)

def _format_context(hits: list[dict]) -> str:
    out: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        t = (h.get("text") or "").strip()
        if not t:
            continue
        snippet = f"[{i}] {t}"
        if used + len(snippet) > 6000:
            break
        out.append(snippet)
        used += len(snippet)
    return "\n".join(out)


def _has_file_in_context(hits: list[dict]) -> dict | None:
    for h in hits:
        if h.get("kind") == "post" and h.get("file_id"):
            return h
    return None


def answer(question: str, user_ctx: dict) -> dict:
    """Return dict:
        {text, grounded, sources, provider, file: optional, error: optional}
    """
    # 1. If user is asking for a file by name, try direct lookup first.
    file_hit = knowledge.find_file_for(question)
    if file_hit and _looks_like_file_request(question):
        # Send the file in the user_ctx["chat_id"]
        chat_id = user_ctx.get("chat_id")
        if chat_id:
            cap = (file_hit.get("caption") or file_hit.get("text") or "Channel se file")[:1000]
            r = tg_api.send_any_file(chat_id, file_hit["file_id"], file_hit.get("file_type", "document"), caption=cap)
            if r.get("ok"):
                return {
                    "text": "File bhej diya hai — channel se 📎",
                    "grounded": True,
                    "sources": 1,
                    "file_sent": True,
                }

    # 2. Knowledge-grounded answer
    hits = knowledge.search(
        question,
        limit=10,
        user_id=user_ctx.get("user_id"),
    )
    ctx_block = _format_context(hits)
    if hits and ctx_block:
        prompt = (
            f"Context (learned from channel + group):\n{ctx_block}\n\n"
            f"User question ({user_ctx.get('first_name') or user_ctx.get('username') or 'user'}): {question}\n"
            "Answer:"
        )
        try:
            text = ai_engine.get_router().generate(prompt, system=SYSTEM_PROMPT)
        except ai_engine.NoProvidersError:
            return {
                "text": "AI keys configure nahi hain. Owner se bolo set karein.",
                "grounded": False,
                "sources": len(hits),
                "error": "no_ai_providers",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "text": None,
                "grounded": False,
                "sources": len(hits),
                "error": str(exc)[:200],
            }
        # The bot ALWAYS answers itself — never refuses with a canned line,
        # never escalates automatically.
        result = {
            "text": text,
            "grounded": True,
            "sources": len(hits),
            "needs_escalation": False,
        }
        # If we also have a related file and the answer mentions it, attach.
        file_hit = _has_file_in_context(hits)
        if file_hit and any(w in text.lower() for w in ("file", "download", "bhej", "link", "send", "attached", "document")):
            result["file_offer"] = {
                "file_id": file_hit["file_id"],
                "file_type": file_hit.get("file_type", "document"),
                "caption": (file_hit.get("caption") or "")[:200],
            }
        return result

    # 3. No matching memory. Answer from general knowledge — the GENERAL
    # prompt already forbids fabricating personal history. Never escalate:
    # the bot always replies itself.
    try:
        text = ai_engine.get_router().generate(
            f"User ({user_ctx.get('username') or 'user'}): {question}\nAnswer concisely:",
            system=GENERAL_PROMPT,
        )
        return {
            "text": text,
            "grounded": False,
            "sources": 0,
            "general": True,
            "needs_escalation": False,
        }
    except ai_engine.NoProvidersError:
        return {
            "text": "AI keys configure nahi hain. Owner se bolo set karein.",
            "grounded": False,
            "sources": 0,
            "needs_escalation": False,
            "error": "no_ai_providers",
        }
    except Exception as exc:  # noqa: BLE001
        return {"text": None, "grounded": False, "sources": 0, "error": str(exc)[:200]}


def _looks_like_file_request(q: str) -> bool:
    ql = q.lower()
    keywords = ("file", "apk", "download", "bhej", "send", "do file", "de do",
                "link", "document", "pdf", "image", "photo", "video", "audio", "zip")
    return any(k in ql for k in keywords)
