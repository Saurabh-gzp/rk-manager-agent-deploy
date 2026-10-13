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
_WHERE_Q = r"(?:kahan|kahaan|kaha|kahaa|kha|kaha\s?se|kha\s?se|kaha\s?hai|where|how)"
_AVAIL_V = r"(?:milega|milegaa|milegi|milenge|milengi|milta\s?hai|milti\s?hai|milti\s?hain|milti\s?ho|mil\s?sakta\s?hai|mil\s?sakte\s?hai|mil\s?skta|available\s?hai|available)"
_TOPIC_N = r"(?:key|keys|app|apps|loader|loadar|file|files|link|links|panel|config|cfg|apk|apkm|mod|password|pass|game|cheez)"
# v3.5 SMART LISTENER: help-seeking questions. Live issue tha ki member
# group me genuinely help maang raha tha — 'key kha milega', 'hello koi
# btayega key kha milega' — aur bot pura SILENT reh gaya (v3.4 discipline
# itna strict ho gaya tha ki real sawal bhi nigal gaye). Ye tier un
# messages ko pakadta hai jo:
#   1. availability puchte hai — 'kaha milega / milega kya / kaise milega'
#   2. group ko khul kar pukarte hai — 'koi btayega / anyone know / help'
#   3. content-topic + kaha/how — 'key kaha hai', 'app kaha se lu'
# Past-tense ('mila tha', 'mil gaya') yahan KABHI match nahi hota — wo
# narration hai, sawal nahi. Bot in sawalo ka jawab CHANNEL/GROUP ke
# actual data se deta hai (grounding wahi rahegi).
_HELP_SEEKING_RE = re.compile(
    r"\b" + _WHERE_Q + r"\b.{0,40}\b" + _AVAIL_V + r"\b"
    r"|\b" + _AVAIL_V + r"\b.{0,40}\b" + _WHERE_Q + r"\b"
    # direct 'how/where do I get' shapes — avail verb ki zaroorat nahi
    r"|\b(where\s?can\s?i\s?get|where\s?can\s?we\s?get|where\s?do\s?i\s?get|"
    r"where\s?to\s?get|how\s?to\s?get|how\s?do\s?i\s?get|how\s?can\s?i\s?get|"
    r"kaise\s?milega|kese\s?milega|kaise\s?milta|kaise\s?milti|"
    r"kaise\s?lu|kaise\s?le|kaha\s?se\s?lu|kaha\s?se\s?le)\b"
    # topic + availability in one breath — 'key milega kya', 'app milta hai?'
    r"|\b" + _TOPIC_N + r"\b.{0,20}\b" + _AVAIL_V + r"\b"
    r"|\b" + _AVAIL_V + r"\b.{0,20}\b" + _TOPIC_N + r"\b"
    r"|\b(koi|koi\s?bhi|kisiko|kisi\s?ko|anyone|somebody|someone)\b"
    r".{0,40}\b(bta\s?do|bta|btayega|btaega|btaige|bata|batao|batayega|"
    r"bataega|btaega|btaiga|bata\s?do|bataye\s?ga|tell|help|"
    r"help\s?kar\s?do|help\s?kro)\b"
    r"|\b(help\s?chahiye|help\s?chahiye\s?hai|help\s?karo|help\s?kro|"
    r"help\s?kar\s?do|help\s?me|madad\s?chahiye|madad\s?karo)\b"
    r"|\b" + _TOPIC_N + r"\b.{0,30}\b(kahan|kahaan|kaha|kahaa|kha|"
    r"kaha\s?se|kha\s?se|where|how)\b",
    re.IGNORECASE,
)


def is_help_seeking_question(text: str) -> bool:
    """v3.5: True for genuine help-seeking questions the bot can answer
    from channel/group data.

    Ye '?'-bypass wala purana cheap-path NAHI hai — 'kal aayega?' jaisa
    member-to-member sawal yahan match NAHI hota (availability verb /
    help-call / topic+where inme se k kuch chahiye hi). Sirf wo sawal
    jo channel data se answer ho sakte hain ('key kha milega') ya group
    ko khul kar pukaarte hain ('koi btayega') is tier me aate hain.
    Routing me inka jawab reply-budget + exchange-participant check ke
    saath hota hai, isliye ye spam wapas nahi layega."""
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return False
    without_mentions = re.sub(r"@[\w]+", "", text).strip()
    if not without_mentions:
        return False
    if _PLAIN_GREETING.fullmatch(without_mentions):
        return False
    # Past-tense narration ('koi bata diya tha', 'mil gaya tha') is a
    # REPORT, not an ask — never treat it as a help question.
    if re.search(r"\b(diya|di|bata\s?diya|mila|mil\s?gaya|mil\s?gayi|"
                 r"bata\s?diya\s?tha|chuka|chuki)\s*(tha|thi|tha\s?hai|hai)\b",
                 without_mentions, re.IGNORECASE):
        return False
    return bool(_HELP_SEEKING_RE.search(without_mentions))


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