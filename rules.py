"""Owner's standing instructions — permanent rules the bot must always follow.

Unlike learned chat knowledge (retrieved by similarity), these are injected
into EVERY prompt at the top, so an owner's explicit teaching (e.g. "app
install na ho to Play Protect OFF karna sikhana") can never be buried or
improvised into its opposite.
"""
from __future__ import annotations

import time

from db import get_state, save_state


_KEY = "owner_rules"
_MAX_RULES = 50
_MAX_LEN = 500


def get_rules() -> list[dict]:
    value = get_state(_KEY, [])
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and str(item.get("rule") or "").strip():
            out.append({"rule": str(item["rule"]).strip()[:_MAX_LEN],
                        "by": item.get("by", 0),
                        "ts": item.get("ts", 0)})
    return out


def add_rule(rule: str, by: int) -> int:
    """Save a rule. Returns 1-based index, -1 if duplicate, 0 if invalid."""
    rule = (rule or "").strip()
    if not rule or len(rule) > _MAX_LEN:
        return 0
    rules = get_rules()
    if any(r["rule"].casefold() == rule.casefold() for r in rules):
        return -1
    if len(rules) >= _MAX_RULES:
        rules = rules[1:]
    rules.append({"rule": rule, "by": by, "ts": time.time()})
    save_state(_KEY, rules)
    return len(rules)


def remove_rule(idx: int) -> str | None:
    """Remove rule by 1-based index. Returns the removed rule text or None."""
    rules = get_rules()
    if idx < 1 or idx > len(rules):
        return None
    removed = rules.pop(idx - 1)
    save_state(_KEY, rules)
    return removed["rule"]


def rules_block() -> str:
    """Prompt block, always injected. Empty string when no rules exist."""
    rules = get_rules()
    if not rules:
        return ""
    lines = ["STANDING INSTRUCTIONS FROM THE OWNER/ADMINS — highest "
             "priority. Follow these EXACTLY, never improvise the opposite:"]
    lines.extend(f"{i}. {r['rule']}" for i, r in enumerate(rules, 1))
    return "\n".join(lines)
