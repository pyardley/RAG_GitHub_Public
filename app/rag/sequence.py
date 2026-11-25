"""Sequence diagram for one step of a flow chart.

The messages are the calls in the method the step points at, in the order
the file writes them. A call to another method in the same class is followed
one level, and so is a call into one other class in the checkout. Branches
stay in source order: the diagram shows both arms, not the one that runs.
"""

from __future__ import annotations

import re
from pathlib import Path

_METHOD = re.compile(
    r"(?m)^(?P<indent>[ \t]*)def[ \t]+(?P<self>self\.)?(?P<name>[A-Za-z_]\w*[?!=]?)"
)
_CLASS = re.compile(r"(?m)^\s*class\s+([A-Z]\w*)")
_COMMENT = re.compile(r"#.*")
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_CALL = re.compile(
    r"\b(?P<verb>redirect_to|render)\b(?:\s*\(?\s*(?P<target>[A-Za-z_]\w*[?!]*))?"
    r"|\b(?P<const>[A-Z][\w:]*)\.(?P<cmethod>[a-z_]\w*[?!]?)"
    r"|\b(?P<recv>@?[a-z_]\w*)\.(?P<rmethod>[a-z_]\w*[?!]?)"
)
_CHAIN = re.compile(r"\s*(?:\([^)]*\))?\s*\.([a-z_]\w*[?!]?)")
_BARE = re.compile(r"(?<![\w.:@])([a-z_]\w*[?!]?)(?=\s*\()")
_BARE_STATEMENT = re.compile(r"^\s*([a-z_]\w*[?!]?)\s*$")
# update_and_advance(ConsentOrderForm) is a call on that class, not a path helper.
_BARE_CONST = re.compile(
    r"(?<![\w.:@])(?P<name>[a-z_]\w*[?!]?)\s*\(\s*(?P<const>[A-Z][\w:]*)"
)
_KEYWORDS = frozenset({
    "alias", "and", "begin", "break", "case", "class", "def", "defined?",
    "do", "else", "elsif", "end", "ensure", "false", "for", "if", "in",
    "module", "next", "nil", "not", "or", "redo", "rescue", "retry",
    "return", "self", "super", "then", "true", "undef", "unless", "until",
    "when", "while", "yield",
})
_SKIP_METHODS = frozenset({
    "present?", "blank?", "nil?", "empty?", "to_s", "to_i", "to_a", "to_h",
    "each", "map", "select", "reject", "first", "last", "where", "find",
    "order", "tap", "itself", "include?", "keys", "values", "any?", "all?",
    "none?", "size", "length", "count", "strip", "gsub", "sub", "split",
    "join", "freeze", "dup", "clone", "inspect", "hash", "fetch", "dig",
    "merge", "compact", "flatten", "presence", "try", "class", "is_a?",
    "kind_of?", "respond_to?", "blank", "empty", "present", "new",
})
_SKIP_RECEIVERS = frozenset({
    "RSpec", "FactoryBot", "SitePrism", "Capybara", "Rails", "ActiveRecord",
    "ApplicationRecord", "Time", "Date", "DateTime", "File", "Dir", "Pathname",
    "JSON", "YAML", "Logger", "BigDecimal", "URI", "SecureRandom", "Digest",
    "Base64", "CSV", "ERB", "Nokogiri", "ENV",
})
_SKIP_WHEN_LISTING = frozenset({"initialize", "inherited", "method_missing"})
_MESSAGE_LIMIT = 24
_FILE_LIMIT = 500_000


def sequence_diagram(text: str, path: str, line: int = 1, root: Path | None = None) -> dict:
    """Participants and ordered messages for the Ruby at ``path``."""
    relative = path.replace("\\", "/")
    if not relative.endswith(".rb"):
        return _empty(
            "This step is not a Ruby file, so there is no call sequence to draw.",
            relative,
        )
    owner = _class_name(text) or _stem(relative)
    methods = _methods(text)
    chosen = _chosen(methods, line)
    if not chosen:
        return _empty("This step's file has no method calls to draw.", relative, owner)
    messages: list[dict] = []
    followed = False
    show_entry = len(chosen) > 1
    for method in chosen:
        if len(messages) >= _MESSAGE_LIMIT:
            break
        if show_entry:
            _add(messages, "Request", owner, method["name"], method["line"])
        before = len(messages)
        _calls(
            method["body"],
            method["line"] + 1,
            owner,
            methods,
            messages,
            depth=0,
            stack=frozenset({method["name"]}),
            root=root,
            followed_flag=[],
        )
        if len(messages) == before and show_entry:
            messages.pop()
    truncated = False
    for item in messages:
        if item.pop("followed", False):
            followed = True
        if item.pop("_capped", False):
            truncated = True
    if not messages:
        return _empty("This step's file has no method calls to draw.", relative, owner)
    messages = messages[:_MESSAGE_LIMIT]
    participants = _participants(owner, messages)
    symbol = f"{owner}#{chosen[0]['name']}" if len(chosen) == 1 else owner
    note = f"Calls in {symbol}, in the order that file writes them."
    if followed:
        note += " A call into another class in this repository is followed one level."
    if truncated:
        note += " Further calls were left out."
    return {
        "symbol": symbol,
        "participants": participants,
        "messages": messages,
        "note": note,
        "start_line": chosen[0]["line"],
        "end_line": chosen[-1]["end"],
    }


def read_class(root: Path, name: str) -> str:
    """The text of the first Ruby file under app or features that defines ``name``."""
    if not re.fullmatch(r"[A-Z]\w*", name):
        return ""
    pattern = re.compile(rf"^\s*class\s+{name}\b", re.M)
    for folder in ("app", "features"):
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.rb")):
            if not path.is_file() or "vendor" in path.parts:
                continue
            try:
                if path.stat().st_size > _FILE_LIMIT:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                return text
    return ""


def _empty(note: str, path: str, symbol: str = "") -> dict:
    return {
        "symbol": symbol or _stem(path),
        "participants": [],
        "messages": [],
        "note": note,
        "start_line": 1,
        "end_line": 1,
    }


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".rb"):
        name = name[:-3]
    return name or "Step"


def _class_name(text: str) -> str:
    match = _CLASS.search(text)
    return match.group(1) if match else ""


def _indent(match: re.Match) -> int:
    return len(match.group("indent").replace("\t", "    "))


def _methods(text: str) -> list[dict]:
    found = list(_METHOD.finditer(text))
    lines = text.splitlines()
    methods: list[dict] = []
    for index, match in enumerate(found):
        indent = _indent(match)
        end_at = len(text)
        for later in found[index + 1:]:
            if _indent(later) <= indent:
                end_at = later.start()
                break
        start = text[: match.start()].count("\n") + 1
        end = text[:end_at].count("\n")
        body = "\n".join(lines[start:end])
        methods.append({
            "name": match.group("name"),
            "line": start,
            "end": max(start, end),
            "body": body,
            "singleton": bool(match.group("self")),
        })
    return methods


def _chosen(methods: list[dict], line: int) -> list[dict]:
    """The method that contains ``line``, or every instance method with a body."""
    if line > 0:
        for method in methods:
            if method["line"] <= line <= method["end"]:
                return [method]
    listed = [
        method for method in methods
        if not method["singleton"] and method["name"] not in _SKIP_WHEN_LISTING
    ]
    return listed


def _calls(
    body: str,
    first_line: int,
    owner: str,
    methods: list[dict],
    messages: list[dict],
    depth: int,
    stack: frozenset[str],
    root: Path | None,
    followed_flag: list,
) -> None:
    by_name = {method["name"]: method for method in methods}
    for offset, raw in enumerate(body.splitlines()):
        line_no = first_line + offset
        cleaned = _STRING.sub('""', _COMMENT.sub("", raw))
        statement = _BARE_STATEMENT.match(cleaned)
        if statement:
            _bare(
                statement.group(1), line_no, owner, by_name, messages,
                depth, stack, root, followed_flag,
            )
            continue
        position = 0
        while position < len(cleaned):
            match = _CALL.search(cleaned, position)
            bare = _BARE.search(cleaned, position)
            aimed = _BARE_CONST.search(cleaned, position)
            if match is None and bare is None and aimed is None:
                break
            if aimed is not None and (match is None or aimed.start() < match.start()) and (
                bare is None or aimed.start() <= bare.start()
            ):
                position = aimed.end()
                const = aimed.group("const").split("::")[-1]
                name = aimed.group("name")
                if (
                    name not in _KEYWORDS
                    and name not in _SKIP_METHODS
                    and const not in _SKIP_RECEIVERS
                    and name not in stack
                ):
                    _add(messages, owner, const, name, line_no)
                continue
            if match is None and bare is None:
                break
            if bare is not None and (match is None or bare.start() < match.start()):
                position = bare.end()
                _bare(
                    bare.group(1), line_no, owner, by_name, messages,
                    depth, stack, root, followed_flag,
                )
                continue
            if match is None:
                break
            position = match.end()
            if match.group("verb"):
                target = match.group("target") or ""
                label = f"{match.group('verb')} {target}".strip()
                _add(messages, owner, "Browser", label, line_no)
                continue
            if match.group("const"):
                const = match.group("const").split("::")[-1]
                called = match.group("cmethod")
                if const in _SKIP_RECEIVERS:
                    continue
                chained = _CHAIN.match(cleaned, position)
                chained_name = chained.group(1) if chained else ""
                if chained:
                    position = chained.end()
                if called == "new":
                    _add(messages, owner, const, "new", line_no)
                elif called not in _SKIP_METHODS:
                    _add(messages, owner, const, called, line_no)
                if chained_name and chained_name not in _SKIP_METHODS:
                    if _follow_class(
                        const, chained_name, line_no, messages, depth, root, followed_flag,
                    ):
                        continue
                    _add(messages, owner, const, chained_name, line_no)
                continue
            receiver = (match.group("recv") or "").lstrip("@")
            called = match.group("rmethod") or ""
            if not receiver or receiver in _KEYWORDS or called in _SKIP_METHODS:
                continue
            if receiver == "self":
                _bare(called, line_no, owner, by_name, messages, depth, stack, root, followed_flag)
                continue
            _add(messages, owner, receiver, called, line_no)


def _bare(
    name: str,
    line_no: int,
    owner: str,
    by_name: dict[str, dict],
    messages: list[dict],
    depth: int,
    stack: frozenset[str],
    root: Path | None,
    followed_flag: list,
) -> None:
    if name in _KEYWORDS or name in _SKIP_METHODS or name in stack:
        return
    method = by_name.get(name)
    if method and depth == 0:
        _calls(
            method["body"],
            method["line"] + 1,
            owner,
            list(by_name.values()),
            messages,
            depth + 1,
            stack | {name},
            root,
            followed_flag,
        )
        return
    if method:
        _add(messages, owner, owner, name, line_no)
        return
    if name.endswith("_path") or name.endswith("_url"):
        _add(messages, owner, "Routes", name, line_no)


def _follow_class(
    const: str,
    name: str,
    line_no: int,
    messages: list[dict],
    depth: int,
    root: Path | None,
    followed_flag: list,
) -> bool:
    """Replace a call into ``const`` with the calls inside that method. One level."""
    if depth != 0 or root is None:
        return False
    text = read_class(root, const)
    if not text:
        return False
    methods = _methods(text)
    method = next((item for item in methods if item["name"] == name), None)
    if method is None:
        return False
    before = len(messages)
    _calls(
        method["body"],
        method["line"] + 1,
        const,
        methods,
        messages,
        depth + 1,
        frozenset({name}),
        root,
        followed_flag,
    )
    if len(messages) == before:
        return False
    for item in messages[before:]:
        item["followed"] = True
    followed_flag.append(True)
    return True


def _add(messages: list[dict], source: str, target: str, label: str, line: int) -> None:
    if len(messages) >= _MESSAGE_LIMIT:
        if messages:
            messages[-1]["_capped"] = True
        return
    messages.append({
        "from": source,
        "to": target,
        "label": label[:160],
        "line": line,
    })


def _participants(owner: str, messages: list[dict]) -> list[str]:
    names: list[str] = []

    def add(name: str) -> None:
        if name and name not in names:
            if name == "Request":
                names.insert(0, name)
            else:
                names.append(name)

    add(owner)
    for message in messages:
        add(str(message["from"]))
        add(str(message["to"]))
    return names
