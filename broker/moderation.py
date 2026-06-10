"""Content filter for public offer fields (see CONTENT_POLICY.md).

Deliberately conservative starter list, keyed by policy section. This catches
the blatant cases cheaply at ingestion; the report endpoint + blocklist handle
what slips through. A semantic (LLM) check can be added behind the same hook
later without touching the API.
"""
from __future__ import annotations

import re
from typing import Optional

# category -> patterns (case-insensitive). Word boundaries to limit false
# positives; German + English variants for the open-local launch audience.
_RULES: dict = {
    "illegal": [
        r"\b(kokain|cocaine|heroin|fentanyl|mdma|ecstasy|amphetamin|crystal\s*meth)\b",
        r"\b(verkaufe|kaufe|suche|biete|sell(ing)?|buy(ing)?)\b[^.]{0,40}\b(gras|weed|cannabis|hasch(isch)?|drogen|drugs)\b",
        r"\b(pistole|gewehr|schusswaffe|munition|sprengstoff|handgranate)\b",
        r"\b(hehlerware|gestohlen(e|es)?\s+(ware|handy|fahrrad))\b",
    ],
    "sexual": [
        r"\bsex\b",
        r"\b(escort|sexdate|sex[-\s]?treffen|prostitution|taschengeld[-\s]?treffen)\b",
        r"\b(blowjob|gangbang|fkk[-\s]?club)\b",
    ],
    "spam": [
        r"https?://",
        r"\b(crypto|bitcoin|gewinn)[^.]{0,30}\b(verdoppeln|verdreifachen|garantiert)\b",
        r"\b(affiliate|klick\s+hier|jetzt\s+kaufen)\b",
    ],
    "fraud": [
        r"\b(geldwäsche|money\s*mule|schnelles\s+geld|finanzagent)\b",
    ],
    "pii": [
        # phone numbers in public fields — contacts belong in the sealed channel.
        # 9+ digits with optional separators, so dates ("10.06.2026", 8 digits)
        # don't false-positive.
        r"\+?\d(?:[\s/().-]?\d){8,}",
    ],
}

_COMPILED = [(category, re.compile(p, re.IGNORECASE)) for category, patterns in _RULES.items()
             for p in patterns]


def check(*texts) -> Optional[str]:
    """Return the violated policy category, or None if all texts are clean."""
    for text in texts:
        if not text:
            continue
        for category, rx in _COMPILED:
            if rx.search(str(text)):
                return category
    return None
