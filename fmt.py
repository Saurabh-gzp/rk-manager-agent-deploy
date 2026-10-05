"""Telegram rich-text formatting for bot messages (v3.3).

Why this exists: the LLM naturally writes markdown (**bold**, `code`,
[label](url)) but Telegram shows those symbols LITERALLY unless the
message is sent with a parse mode. The owner's complaint — 'formats
sablo add karo' — is exactly this: answers arrived with visible
asterisks and dead, un-clickable URLs.

This module converts LLM markdown into Telegram-valid HTML:
    **bold**      -> <b>bold</b>
    __underline__ -> <u>underline</u>
    *italic*      -> <i>italic</i>
    _italic_      -> <i>italic</i>
    ~~strike~~    -> <s>strike</s>
    `code`        -> <code>code</code>
    ```pre```     -> <pre>pre</pre>
    [label](url)  -> <a href="url">label</a>
    bare URLs     -> <a href="url">url</a>   (clickable)

Everything else is HTML-escaped so user/model text can never break the
parse. If anything unexpected happens the functions degrade to escaped
plain text — a message must never fail to send because of formatting.
"""
from __future__ import annotations

import html
import re

_URL_RE = re.compile(r"(?:https?://|t\.me/|www\.)([^\s<>\)\]]+)", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^\s)\n]{3,300}|t\.me/[^\s)\n]{2,200})\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_UNDER_RE = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.DOTALL)
_ITAL_STAR_RE = re.compile(r"(?<!\*)\*(?=[^\s*])([^*\n]+?)(?<!\s)\*(?!\*)")
_ITAL_UL_RE = re.compile(r"(?<![_\w])_(?=[^\s_])([^_\n]+?)(?<!\s)_(?![_\w])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_PRE_RE = re.compile(r"```([^`]*?)```", re.DOTALL)


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _esc_attr(s: str) -> str:
    return html.escape(s or "", quote=True)


def markdown_to_html(text: str | None) -> str:
    """LLM markdown -> Telegram HTML. Never raises; worst case returns
    fully-escaped plain text."""
    try:
        s = str(text or "")
        if not s:
            return ""
        slots: list[str] = []

        def _stash(html_frag: str) -> str:
            slots.append(html_frag)
            return f"\x00{len(slots) - 1}\x00"

        # 1) protect code/pre/link content FIRST (nothing else formats inside)
        s = _PRE_RE.sub(lambda m: _stash(f"<pre>{_esc(m.group(1).strip())}</pre>"), s)
        s = _CODE_RE.sub(lambda m: _stash(f"<code>{_esc(m.group(1))}</code>"), s)
        s = _MD_LINK_RE.sub(
            lambda m: _stash(
                f'<a href="{_esc_attr(m.group(2))}">{_esc(m.group(1))}</a>'), s)

        # 2) escape the remaining free text
        s = _esc(s)

        # 3) inline formatting on escaped text (tags contain no &<> so safe)
        s = _BOLD_RE.sub(r"<b>\1</b>", s)
        s = _UNDER_RE.sub(r"<u>\1</u>", s)
        s = _ITAL_STAR_RE.sub(r"<i>\1</i>", s)
        s = _ITAL_UL_RE.sub(r"<i>\1</i>", s)
        s = _STRIKE_RE.sub(r"<s>\1</s>", s)

        # 4) clickable bare URLs (only http(s)/t.me — never after a quote)
        def _linkify(m: re.Match) -> str:
            url = m.group(0)
            return f'<a href="{_esc_attr(url)}">{_esc(url)}</a>'
        s = re.sub(r"(?<![\w\"'>=])((?:https?://|t\.me/)[^\s<>\)\]]+)",
                   _linkify, s, flags=re.IGNORECASE)

        # 5) restore stashed fragments
        s = re.sub(r"\x00(\d+)\x00",
                   lambda m: slots[int(m.group(1))] if int(m.group(1)) < len(slots) else "",
                   s)
        return s
    except Exception:
        return _esc(str(text or ""))


def looks_formatted(text: str | None) -> bool:
    """Whether the text contains any markdown/URL worth HTML rendering."""
    s = str(text or "")
    if not s:
        return False
    return bool(re.search(r"\*\*|__|~~|`|\[[^\]]+\]\(|https?://|(?<![\w.])t\.me/", s))


def entity_html(text: str, entities: list[dict]) -> str | None:
    """Rebuild a caption/message body as Telegram HTML from stored Telegram
    entities (UTF-16 offsets). Restores clickable text_links (the 'KEY:
    Click me' problem) plus bold/italic/underline/strikethrough/code.
    Overlapping entities: outermost wins by priority order. Returns None
    when nothing could be applied (caller falls back to markdown_to_html).
    """
    try:
        if not text or not entities:
            return None
        prio = {"text_link": 0, "url": 0, "bold": 1, "italic": 2, "underline": 3,
                "strikethrough": 4, "code": 5, "pre": 6, "spoiler": 7}
        ents = [e for e in entities if isinstance(e, dict)
                and e.get("type") in prio and (e.get("length") or 0) > 0]
        if not ents:
            return None

        # UTF-16 offsets -> python indices
        def _py_index(s: str, off16: int) -> int:
            u = 0
            for i, ch in enumerate(s):
                if u >= off16:
                    return i
                u += 2 if ord(ch) > 0xFFFF else 1
            return len(s)

        spans: list[tuple[int, int, int, dict]] = []  # (start, end, prio, ent)
        for e in ents:
            st = _py_index(text, int(e.get("offset") or 0))
            en = _py_index(text, (int(e.get("offset") or 0)) + int(e.get("length") or 0))
            if en > st:
                spans.append((st, en, prio[e["type"]], e))
        # sort by start, then priority (outermost/earliest wins on overlap)
        spans.sort(key=lambda x: (x[0], x[2]))
        taken: list[tuple[int, int]] = []
        chosen: list[tuple[int, int, dict]] = []
        for st, en, _p, e in spans:
            if any(st < te and ts < en for ts, te in taken):
                continue  # overlaps an accepted span — skip
            taken.append((st, en))
            chosen.append((st, en, e))
        if not chosen:
            return None

        out: list[str] = []
        pos = 0
        for st, en, e in chosen:
            out.append(_esc(text[pos:st]))
            frag = _esc(text[st:en])
            t = e["type"]
            if t in ("text_link", "url") and e.get("url"):
                out.append(f'<a href="{_esc_attr(str(e["url"]))}">{frag}</a>')
            elif t == "bold":
                out.append(f"<b>{frag}</b>")
            elif t in ("italic",):
                out.append(f"<i>{frag}</i>")
            elif t == "underline":
                out.append(f"<u>{frag}</u>")
            elif t == "strikethrough":
                out.append(f"<s>{frag}</s>")
            elif t == "code":
                out.append(f"<code>{frag}</code>")
            elif t == "pre":
                out.append(f"<pre>{frag}</pre>")
            elif t == "spoiler":
                out.append(f'<tg-spoiler>{frag}</tg-spoiler>')
            else:
                out.append(frag)
            pos = en
        out.append(_esc(text[pos:]))
        return "".join(out)
    except Exception:
        return None
