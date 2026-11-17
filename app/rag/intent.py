"""Detect when a Query question should use Impact instead of vector search."""

from __future__ import annotations

import re

# Callers, "what is affected", and "what breaks" are Impact questions. Ordinary
# "where is X defined" questions stay on search.
_IMPACT_INTENT = re.compile(
    r"(?i)\b(?:"
    r"affected|callers?|"
    r"what calls|who calls|what uses|"
    r"which .{0,60}?\b(?:call|calls|use|uses|instantiate|instantiates|are affected|break|breaks)\b|"
    r"where is \S+ run\b|"
    r"would (?:be )?affected|would break|break if|"
    r"instantiates? or calls?"
    r")\b"
)

# Prefer the most specific code shape first.
_TARGET_PATTERNS = (
    re.compile(
        r"\b([A-Z][\w]*(?:::[A-Z][\w]*)+#[\w?!]+)\b"
    ),
    re.compile(
        r"\b([A-Z][\w]*(?:::[A-Z][\w]*)+)\b"
    ),
    re.compile(
        r"\b([A-Z][\w]*#[\w?!]+)\b"
    ),
    re.compile(
        r"\b([A-Z][\w]*\.[a-z_][\w?!]*)\b"
    ),
    # "DependentPage method submit_child_bands" / "ContactPage page object method valid_email"
    re.compile(
        r"\b([A-Z][\w]*)\b(?:\s+page object)?\s+method\s+([a-z_][\w?!]*)\b"
    ),
    re.compile(
        r"\b([A-Z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]+)+)\b"
    ),
    re.compile(
        r"\b([a-z_][\w]*#[\w?!]+)\b"
    ),
    re.compile(
        r"\b([a-z_][\w]{2,})\b"
    ),
)

_STOPWORDS = {
    "what", "which", "who", "where", "when", "how", "the", "a", "an", "if", "i",
    "change", "changed", "changes", "calls", "call", "called", "caller", "callers",
    "affected", "affect", "files", "file", "code", "method", "methods", "module",
    "class", "helper", "helpers", "page", "pages", "object", "objects", "step",
    "definitions", "definition", "views", "view", "breaks", "break", "would",
    "be", "its", "for", "example", "and", "or", "to", "of", "in", "on", "is",
    "are", "does", "run", "uses", "use", "using", "used", "instantiates",
    "instantiate", "lookup", "calculates", "calculate", "renamed", "removed",
    "remove", "value", "values", "such", "as", "from", "with", "that", "this",
    "those", "these", "their", "them", "about", "into", "admin", "claim",
    "court", "applicant", "delivery", "emails", "queue", "list", "lists",
    "sections", "section", "summary", "presenters", "presenter", "pdf",
    "navigation", "formats", "today", "date", "office", "login", "users",
    "user", "new", "find", "load", "check", "json", "welsh", "english",
    "refunds", "refund", "average", "months", "month", "default", "asked",
    "people", "aged", "over", "special", "rule", "high", "too", "savings",
    "income", "fee", "fees", "application", "applications", "processed",
    "waiting", "evidence", "payment", "part", "personal", "data", "wiped",
    "retention", "period", "roles", "role", "manager", "allowed", "staff",
    "member", "edit", "someone", "works", "different", "office", "jurisdiction",
    "name", "names", "accounting", "existing", "record", "end", "create",
    "how", "does", "system", "decide", "whether", "including", "after",
}


_DEFINITION_ASK = re.compile(
    r"(?i)\bwhere (?:is|are)\b.{0,100}\bdefined\b"
)


def impact_target(question: str) -> str | None:
    """Return the symbol an Impact lookup should use, or None for ordinary search."""
    text = (question or "").strip()
    if not text or _IMPACT_INTENT.search(text) is None:
        return None
    # "Where is X defined, and which form uses …" is an identifier question.
    if _DEFINITION_ASK.search(text) is not None:
        return None
    for pattern in _TARGET_PATTERNS:
        for match in pattern.finditer(text):
            if match.lastindex and match.lastindex >= 2 and match.group(2):
                candidate = f"{match.group(1)}#{match.group(2)}"
            else:
                candidate = match.group(1)
            if _usable_target(candidate):
                return candidate
    return None


def _usable_target(candidate: str) -> bool:
    lowered = candidate.lower()
    if lowered in _STOPWORDS:
        return False
    if "#" in candidate or "::" in candidate or "." in candidate:
        return True
    if candidate[:1].isupper() and any(char.islower() for char in candidate[1:]):
        # CamelCase class or constant name.
        return any(char.isupper() for char in candidate[1:]) or len(candidate) >= 6
    if "_" in candidate and candidate[:1].islower():
        return len(candidate) >= 6
    return False
