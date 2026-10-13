"""Low-noise trigger rules for natural group questions and requests."""
from __future__ import annotations

import re


_QUESTION_START = re.compile(
    r"^(?:kya|kaise|kab|kahan|kaha|kyu|kyun|kyon|kaun|kis|kitna|kitni|kitne|"
    r"what|when|where|why|who|how|which|can you|could you|do you|are you|"
    r"is there|tell me|help me)\b",
    re.IGNORECASE,
)
_REQUEST_WORD = re.compile(
    r"\b(?:help|scan|check|status|search|find|explain|batao|bataiye|bata\s?do|"
    r"samjhao|bhejo|bhej\s?do|bhej\s?dena|send|download|dikhao|show\s?me|are\s?you\s?admin|"
    r"tum\s?admin|admin\s?ho|admin\s?hai|bot\s?admin)\b"
    # v3.3: real Hinglish request shapes — 'app do', 'key dedo', 'wala de do',
    # 'do na' — isse 'are twitter wala app do...' ka direct answer hota hai
    # (pehle gate chalta tha aur kabhi kabhi chup reh jata tha).
    r"|\b(?:dedo|de\s?do|dijiye|do\s?na|dena|chahiye|doooo+)\b"
    r"|\b(?:app|file|loader|key|link|photo|video|post|cheez|loadar|apkm|apk)\s+"
    r"(?:do|dena|dedo|de\s?do|dijiye|do\s?na)\b",
    re.IGNORECASE,
)
# v3.4 STRICT action-request shapes — is_question_or_request ka subset jo
# sirf tab fire hota hai jab user bot se kuch KARWANA/LENA chahta hai.
# 'check', 'scan', 'status', 'find', 'explain' jaise akele chatty words
# yahan NAHI hain — wahi live spam ka root cause the (do members ki
# baat-cheet 'check kar' / 'status?' par bot direct reply kar deta tha).
_ACTION_REQUEST_RE = re.compile(
    r"\b(?:help|batao|bataiye|bata\s?do|samjhao|bhejo|bhej\s?do|bhejdo|"
    r"bhej\s?dena|send|forward|forword|download|dikhao|dikha\s?do|"
    r"show\s?me|chahiye|mila\s?do|mila\s?dena)\b"
    r"|\b(?:are\s?you\s?admin|tum\s?admin|admin\s?ho|admin\s?hai|bot\s?admin)\b"
    r"|\b(?:dedo|de\s?do|dijiye|do\s?na|dena|doooo+)\b"
    r"|\b(?:app|apps|file|files|loader|loadar|key|link|photo|video|post|"
    r"cheez|apkm|apk|panel|config|cfg|doc|document|loader)\s+"
    r"(?:do|dena|dedo|de\s?do|dijiye|do\s?na)\b"
    r"|\b(?:scan|status)\s+(?:karo|kro|krdo|kardo|kru)\b"
    r"|\bgroup\s+(?:scan|status|check)\b"
    r"|\b(?:permissions?|member\s?count|kitne\s+member)\b",
    re.IGNORECASE,
)
_PLAIN_GREETING = re.compile(
    r"^(?:hi+|h[eao]+y+|hyy+|hello+|hey+|yo+|sup|good morning|good evening|"
    r"thanks?|thank you|ok(?:ay)?|hmm+)[.!?]*$",
    re.IGNORECASE,
)


def is_plain_chatter(text: str) -> bool:
    """True for greetings/thanks/acknowledgements with nothing else.

    These never need the bot's reply — checked before the LLM gate so we
    don't burn an API call on 'hyy' / 'ok' / 'thanks'.
    """
    text = (text or "").strip()
    if not text:
        return True
    without_mentions = re.sub(r"@\w+", "", text).strip()
    return bool(_PLAIN_GREETING.fullmatch(without_mentions))


def is_question_or_request(text: str) -> bool:
    """Return True for explicit questions/actions, not greetings or chatter."""
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return False
    without_mentions = re.sub(r"@\w+", "", text).strip()
    if _PLAIN_GREETING.fullmatch(without_mentions):
        return False
    if "?" in text or "؟" in text:
        return True
    if _QUESTION_START.match(without_mentions):
        return True
    return bool(_REQUEST_WORD.search(without_mentions))


def is_explicit_request(text: str) -> bool:
    """v3.4: TRUE only for messages that EXPLICITLY ask the bot to DO
    something or GIVE something — an action/content request.

    Ye is_question_or_request ka strict subset hai: '?', 'kya/kab/kyu...'
    jaise question-start words YAHAN trigger NAHI hote. Wajah: live issue
    tha ki do members apas me baat karte the ('kal aayega?', 'mil gaya?')
    aur bot un sab par reply kar deta tha — koi bhi '?' cheap-path se
    gate ko bypass kar ke seedha answer de deta tha. Ab questions ko LLM
    gate se hona padta hai (wo context dekh kar decide karta hai), aur
    sirf real requests (bhejo/dedo/app do/help/status...) direct route
    hote hain."""
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return False
    without_mentions = re.sub(r"@\w+", "", text).strip()
    if _PLAIN_GREETING.fullmatch(without_mentions):
        return False
    return bool(_ACTION_REQUEST_RE.search(without_mentions))


def is_group_scan_request(text: str) -> bool:
    """Detect read-only questions that should use Telegram's live group APIs."""
    q = (text or "").casefold()
    terms = (
        "scan", "group info", "group status", "group check", "group audit",
        "admin ho", "admin hai", "admin status", "permissions", "permission",
        "member count", "members kitne", "kitne member", "kitne members",
        "tum admin", "bot admin", "group ke admin", "are you admin",
    )
    live_check = any(word in q for word in ("scan", "check", "status", "audit", "permission"))
    target = any(word in q for word in ("admin", "permission", "member", "count"))
    return any(term in q for term in terms) or (live_check and target)