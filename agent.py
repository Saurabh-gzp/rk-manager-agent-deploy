"""Q&A + content delivery engine.

Uses the AI router (multi-provider with key rotation) over knowledge.search().
If grounded answer isn't possible, escalates to admin first, then owner.
"""
from __future__ import annotations

import re
from typing import Any

import ai_engine
import config
import knowledge
import links
import rules
import tg_api


# The bot's real moderation powers. Stated in the prompt so the model never
# invents a refusal like "mere paas power nahi hai".
_CAPABILITIES = (
    "Your real group moderation tools — never deny having these: "
    "/filter (auto-reply or auto-delete on a word), /filters (list them), "
    "/stop (remove a filter), /mute, /tmute (temp mute like 30m/2h/1d), "
    "/unmute, /delete (delete a replied message), /warn /warnings "
    "/resetwarns (warning system: 3 warns = 1h auto-mute), /purge (bulk "
    "delete), /pin /unpin, /kick (owner), /ban and /unban (OWNER ONLY), "
    "/files (list channel files), /stats, /id, /report, /help, /rules, "
    "/sikhao (owner teaches you tasks). Mute/unmute targets work by REPLY, "
    "user ID, @username OR plain naam (jaise 'rich'). Your auto-moderation "
    "also acts by itself: abuse words get deleted (warn first, mute on "
    "repeat — never auto-ban), and filters set by the owner trigger "
    "automatically. If someone asks about your commands or powers, list "
    "these briefly and truthfully. Never invent a missing ability. "
    "You can also SEE photos, LISTEN to voice messages, and WATCH short "
    "videos users send — they arrive attached to the message, use them. "
    "You can deliver files from the linked channel on request."
)


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
    "and chat-like — like a group member, not a robot. "
    "You are also the group's MANAGER, not just a Q&A bot — think like one: "
    "if a task needs checking something first, say you'll check, check it, "
    "then report back. "
    "Who-info: the sender's group role and YOUR admin rights in this group "
    "are given in context — always respect them. If someone asks "
    "'me kon hu / main kaun hun', answer with their actual role from context. "
    "Before promising to delete/mute/ban, check your rights in context; if "
    "you lack them, say clearly which admin permission you need instead of "
    "pretending. "
    "You may reply with MULTIPLE messages when natural (e.g. an "
    "acknowledgement first, then details): separate messages with a line "
    "containing only <SPLIT>. "
    "If the user asks something you should verify first (e.g. 'new loader "
    "konsa aaya') and the context above is not enough, do NOT guess — reply "
    "with exactly: LOOKUP: <search query>. The system will then say "
    "'Ruko bhai, dekhta hu', run a deeper search, and ask you again with "
    "fresh results. Use LOOKUP only when a real check is needed, never for "
    "chit-chat. "
    "Action honesty — CRITICAL: you have NO power to delete/mute/ban/"
    "unmute/pin/kick by writing about it. NEVER write 'delete kar diya', "
    "'mute kar diya', 'unmute kar diya', 'uda diya' — and NEVER promise "
    "future actions like 'karta hu', 'kar dunga', 'nikal dunga', 'uda "
    "dunga'. If the owner asks you in WORDS to act on someone (mute/unmute/"
    "delete/kick), the SYSTEM turns it into a real command and executes it "
    "separately — you do NOT confirm it happened; if the system executed "
    "it, a separate system notice will announce the real result. If no "
    "system notice appeared, the action did NOT happen — reply by telling "
    "the owner to tag the user, reply to their message, or use the exact "
    "command (e.g. '/unmute rich'). If a member asks you to act against "
    "someone, refuse briefly: ye sirf owner ke kehne pe hota hai. "
    "Channels: you NEVER post to the channel on your own — not now, not "
    "later, not even if asked by a member; only the owner's explicit /post "
    "command posts there. "
    "Standing rules: the OWNER may teach you permanent instructions (e.g. "
    "'agar koi bole app install nahi ho raha to Play Protect OFF karna "
    "sikhana'). When the OWNER gives you such a standing rule in chat, "
    "include EXACTLY ONE line in your reply: RULE: <the rule rewritten as a "
    "clear instruction to yourself, 1-2 lines>. The system saves it "
    "permanently and will ALWAYS show it to you before answering. Only use "
    "RULE: for genuine standing instructions from the OWNER — never for "
    "normal chat, never for members. After the RULE: line, confirm briefly "
    "in your own words that you've noted it. "
    "Also: the owner's instructional messages in context ('agar koi X to Y "
    "karna') are BINDING — follow them exactly, never improvise the "
    "opposite. "
    + _CAPABILITIES
)

GENERAL_PROMPT = (
    "You are RK Manager, a concise Telegram assistant. Answer ordinary "
    "general-knowledge questions in the user's language. The sender's group "
    "role and your admin rights may be given in context — you may use exactly "
    "what is stated there, never guess beyond it. Never claim to know a "
    "user's past conversation unless that information is explicitly present "
    "in context. If asked about a person's past messages and there is no "
    "matching context, say you do not have that history. "
    + _CAPABILITIES
)


def _role_context(user_ctx: dict) -> str:
    """Human-readable lines about the sender's group role and the bot's own
    admin rights, so the model answers 'me kon hu' truthfully and never
    promises an action it cannot perform."""
    lines: list[str] = []
    role = user_ctx.get("group_role")
    if role and role != "unknown":
        human = {"owner": "the group OWNER",
                 "administrator": "a group ADMIN",
                 "member": "a group MEMBER"}.get(role, role)
        lines.append(f"- The person messaging you right now is {human}.")
    ba = user_ctx.get("bot_admin")
    if isinstance(ba, dict) and ba.get("status") not in (None, "unknown"):
        status = ba.get("status")
        if status in ("creator", "administrator"):
            can = []
            if ba.get("can_delete"):
                can.append("delete messages")
            if ba.get("can_restrict"):
                can.append("mute/restrict and ban members")
            rights = ", ".join(can) if can else "no special rights recorded"
            lines.append(
                f"- YOUR status in this group: admin ({status}). "
                f"You CAN: {rights}.")
        else:
            lines.append(
                "- YOUR status in this group: NOT admin — you cannot "
                "delete/mute/ban anyone until an admin gives you those rights.")
    return "\n".join(lines)

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
        if h.get("kind") == "post" and (h.get("file_id")
                                        or h.get("file_type") == "contact"):
            return h
    return None


def _maybe_deep_lookup(first_text: str | None, question: str,
                       user_ctx: dict, system: str) -> tuple[str | None, str | None]:
    """Honor a `LOOKUP: <query>` request from the model.

    Sends an interim acknowledgement ("Ruko bhai, dekhta hu"), runs a
    deeper knowledge search with the model's own query, then asks the
    model again with the fresh results. Single round only — no loops.
    Returns (final_text, interim_text_or_None).
    """
    t = (first_text or "").strip()
    if t[:7].upper() != "LOOKUP:":
        return first_text, None
    query = t[7:].strip()[:200]
    chat_id = user_ctx.get("chat_id")
    if not query or not chat_id:
        return first_text, None
    interim = "Ruko bhai, dekhta hu 🔍"
    try:
        tg_api.send_message(chat_id, interim)
    except Exception:
        pass
    try:
        hits = knowledge.search(query, limit=10,
                                user_id=user_ctx.get("user_id"))
        ctx2 = _format_context(hits)
    except Exception:
        ctx2 = ""
    prompt = (
        f"User ka original sawal: {question}\n"
        f"Tumne deeper search manga tha: '{query}'\n"
        f"Deeper search results:\n{ctx2 if ctx2 else '(kuch khaas nahi mila)'}\n\n"
        "Ab final jawab do — user ki language me, concise, apne shabdon me. "
        "Agar results me jawab nahi hai to apne general knowledge se do. "
        "'Check kiya' jaisi robotic line mat likho, seedha jawab do. "
        "IMPORTANT: Be honest about what you checked — you searched the group's "
        "learned knowledge/messages, NOT live Telegram state. Never claim you "
        "fetched the live member list, verified who is online, or checked live "
        "group info."
    )
    try:
        final = ai_engine.get_router().generate(prompt, system=system)
    except Exception:
        final = ""
    if not (final or "").strip():
        final = ("Dekha maine, lekin is par pakki info nahi mili. "
                 "Thoda detail batao, phir batata hu.")
    return final, interim


_RULE_LINE_RE = re.compile(r"^RULE:\s*(.+?)\s*$", re.MULTILINE)


def _extract_rule(text: str | None, user_ctx: dict) -> tuple[str | None, int]:
    """Save the owner's RULE: lines as standing instructions.

    Only the owner's lines are honored (members can never plant rules).
    Returns (cleaned_text, saved_count).
    """
    if not text or user_ctx.get("user_id") != config.OWNER_CHAT_ID:
        return text, 0
    saved = 0

    def _save(match: re.Match) -> str:
        nonlocal saved
        if rules.add_rule(match.group(1), config.OWNER_CHAT_ID) != 0:
            saved += 1
        return ""

    text = _RULE_LINE_RE.sub(_save, text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text and saved:
        text = "Note kar liya boss ✅ — ab se hamesha yaad rahega."
    return text, saved


_MEDIA_NOTE = {
    "photo": "ek photo (image)",
    "voice": "ek voice message (audio)",
    "video": "ek video",
}


def _generate_answer(prompt: str, system: str, user_ctx: dict) -> str:
    """Generate via the router; uses Gemini multimodal when media attached."""
    media = user_ctx.get("media") or []
    if media:
        kinds = ", ".join(_MEDIA_NOTE.get(m.get("kind"), "media") for m in media)
        prompt = (f"[User ne {kinds} bheja hai — use dhyan se dekh/sun ke jawab do. "
                  f"User ka sawal/caption neeche hai.]\n\n" + prompt)
        return ai_engine.get_router().generate_mm(prompt, media, system=system)
    return ai_engine.get_router().generate(prompt, system=system)


def _deliver_channel_item(chat_id: int, item: dict) -> dict:
    """Deliver a channel item to a chat: file, media, or contact.

    Contacts (vCards) have no file_id — they are delivered by forwarding
    the original channel post, or by sending the stored contact card.
    """
    file_id = item.get("file_id")
    if file_id:
        cap = (item.get("caption") or item.get("text")
               or "Channel se file")[:1000]
        return tg_api.send_any_file(chat_id, file_id,
                                    item.get("file_type") or "document",
                                    caption=cap)
    # Contact / forward-only post
    ch = item.get("channel_id") or links.get_channel_id()
    mid = item.get("message_id")
    if ch and mid:
        r = tg_api.forward_message(chat_id, ch, mid)
        if r.get("ok"):
            return r
        r = tg_api.copy_message(chat_id, ch, mid)
        if r.get("ok"):
            return r
    contact = item.get("contact") or {}
    if contact.get("phone_number"):
        return tg_api.send_contact(
            chat_id, contact["phone_number"],
            contact.get("first_name") or "Contact",
            last_name=contact.get("last_name") or None,
            vcard=contact.get("vcard") or None)
    return {"ok": False, "description": "item not deliverable"}


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
            r = _deliver_channel_item(chat_id, file_hit)
            if r.get("ok"):
                return {
                    "text": "Bhej diya — channel se 📎",
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
    who_block = _role_context(user_ctx)
    standing = rules.rules_block()
    if hits and ctx_block:
        prompt = (
            (standing + "\n\n" if standing else "")
            + f"Context (learned from channel + group):\n{ctx_block}\n\n"
            + (f"Who-info:\n{who_block}\n\n" if who_block else "")
            + f"User question ({user_ctx.get('first_name') or user_ctx.get('username') or 'user'}): {question}\n"
            "Answer:"
        )
        try:
            text = _generate_answer(prompt, SYSTEM_PROMPT, user_ctx)
            text, interim = _maybe_deep_lookup(text, question, user_ctx,
                                               SYSTEM_PROMPT)
            text, _ = _extract_rule(text, user_ctx)
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
        if interim:
            result["interim"] = interim
        # If we also have a related file and the answer mentions it, attach.
        file_hit = _has_file_in_context(hits)
        if file_hit and any(w in text.lower() for w in ("file", "download", "bhej", "link", "send", "attached", "document", "contact", "number")):
            result["file_offer"] = {
                "file_id": file_hit.get("file_id"),
                "file_type": file_hit.get("file_type", "document"),
                "caption": (file_hit.get("caption") or "")[:200],
                "channel_id": file_hit.get("channel_id"),
                "message_id": file_hit.get("message_id"),
                "contact": file_hit.get("contact"),
            }
        return result

    # 3. No matching memory. Answer from general knowledge — the GENERAL
    # prompt already forbids fabricating personal history. Never escalate:
    # the bot always replies itself.
    try:
        who_block = _role_context(user_ctx)
        standing = rules.rules_block()
        qprompt = (
            (standing + "\n\n" if standing else "")
            + (f"Who-info:\n{who_block}\n\n" if who_block else "")
            + f"User ({user_ctx.get('username') or 'user'}): {question}\nAnswer concisely:"
        )
        text = _generate_answer(qprompt, GENERAL_PROMPT, user_ctx)
        text, interim = _maybe_deep_lookup(text, question, user_ctx,
                                           GENERAL_PROMPT)
        text, _ = _extract_rule(text, user_ctx)
        return {
            "text": text,
            "grounded": False,
            "sources": 0,
            "general": True,
            "needs_escalation": False,
            "interim": interim,
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
