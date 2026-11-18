"""Questions, feature visits, and commit diffs for a flow chart.

The chart itself comes from the journey readers. This module does not name a
repository. A question that names a step and an answer follows that arrow.
Any other question is limited to the files for the steps on the chart. A
corner mark is a step a feature or step definition names. When a later commit
is available, the two charts are compared for steps added, removed, or rewired.
"""

from __future__ import annotations

import re
from pathlib import Path

_STEP_CONTROLLER = re.compile(r"^app/controllers/steps/([^/]+)/([^/]+)_controller\.rb$")
_TOKEN = re.compile(r"[a-z0-9]+")
_VISIT_NOTE = "A corner mark is a step a feature or step definition names."
_CHANGE_CAP = 12
_SCAN_SUFFIXES = {".feature", ".rb"}
_STOP = {
    "what", "whats", "happens", "happen", "happened", "if", "the", "a", "an",
    "applicant", "says", "say", "said", "to", "does", "do", "did", "when",
    "about", "on", "for", "is", "are", "was", "were", "there", "this", "that",
    "i", "me", "my", "of", "in", "at", "and", "or", "answer", "answers", "next",
    "step", "steps", "page", "goes", "go", "choose", "chooses", "chosen",
    "selected", "select", "then", "from", "with", "into", "it", "its", "we",
    "they", "user", "someone", "please", "show", "tell",
}


def branch_answers(nodes: list[dict], edges: list[dict], question: str) -> list[dict]:
    """Next steps named by the question, taken from the chart's arrows.

    A multi-word arrow label that appears in the question is used as written.
    Otherwise the question has to name a step and say yes or no, and the arrow
    with that exact answer is returned. An empty list means the question is
    not an arrow on this chart.
    """
    asked = _normalise(question)
    if not asked:
        return []
    by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    phrases = _phrase_hits(edges, by_id, asked)
    if phrases:
        return phrases[:_CHANGE_CAP]
    polarity = _polarity(_TOKEN.findall(asked))
    if not polarity:
        return []
    question_tokens = _significant(asked)
    if not question_tokens:
        return []
    scored: list[tuple[float, str]] = []
    for node_id, node in by_id.items():
        score = _label_score(str(node.get("label") or ""), question_tokens)
        if score >= 0.2:
            scored.append((score, node_id))
    if not scored:
        return []
    best = max(score for score, _node_id in scored)
    chosen = {node_id for score, node_id in scored if score >= best - 0.05}
    found: list[dict] = []
    for edge in edges:
        source = str(edge.get("from") or "")
        if source not in chosen:
            continue
        if _normalise(str(edge.get("label") or "")) != polarity:
            continue
        target = by_id.get(str(edge.get("to") or ""))
        source_node = by_id.get(source)
        if target is None or source_node is None:
            continue
        found.append(_answer(source_node, edge, target))
        if len(found) >= _CHANGE_CAP:
            break
    return found


def journey_scopes(nodes: list[dict], root: Path | None = None) -> list[str]:
    """Files and directories a question about this chart may search.

    A step controller also includes its view directory. A locale file is
    included when one of its folders is that step's namespace or name. The
    shared ``en.yml`` at the top of ``config/locales`` is left out, because
    that file matches ordinary English questions ahead of the code.
    """
    scopes: list[str] = []
    steps: list[tuple[str, str]] = []
    for node in nodes:
        path = _safe_path(str(node.get("path") or ""))
        if not path:
            continue
        _add(scopes, path)
        match = _STEP_CONTROLLER.match(path)
        if match is None:
            continue
        namespace, step = match.group(1), match.group(2)
        steps.append((namespace, step))
        _add(scopes, f"app/views/steps/{namespace}/{step}")
    if root is not None and steps:
        _locale_scopes(scopes, root, steps)
    return scopes


def mark_visits(root: Path, chart: dict) -> None:
    """Set ``visited`` on steps named by a feature or a step definition."""
    nodes = [node for node in chart.get("nodes") or [] if isinstance(node, dict)]
    needles = [(node, _needles(node)) for node in nodes]
    needles = [(node, items) for node, items in needles if items]
    if not needles:
        return
    files = _feature_files(root)
    if not files:
        return
    for node, items in needles:
        own = _safe_path(str(node.get("path") or ""))
        hits = [
            path for path, text in files
            if path != own and any(_contains_name(text, needle) for needle in items)
        ]
        if not hits:
            continue
        node["visited"] = True
        node["visit_count"] = len(hits)
        node["visits"] = hits[:4]
    if any(node.get("visited") for node in nodes):
        note = str(chart.get("note") or "").strip()
        if _VISIT_NOTE not in note:
            chart["note"] = f"{note} {_VISIT_NOTE}".strip()


def apply_journey_diff(indexed: list[dict], tip: list[dict], remote: str) -> None:
    """Attach a change summary to each indexed chart, compared with ``tip``."""
    if not tip:
        for chart in indexed:
            chart["changes"] = _empty_changes(remote, unavailable=True)
        return
    used: set[int] = set()
    by_title: dict[str, list[dict]] = {}
    for chart in tip:
        by_title.setdefault(str(chart.get("title") or ""), []).append(chart)
    for chart in indexed:
        title = str(chart.get("title") or "")
        match = next((item for item in by_title.get(title, []) if id(item) not in used), None)
        if match is None and len(indexed) == 1 and len(tip) == 1:
            match = tip[0]
        if match is not None:
            used.add(id(match))
        chart["changes"] = diff_chart(chart, match, remote)


def diff_chart(indexed: dict | None, tip: dict | None, remote: str) -> dict:
    """Steps added, removed, or given different outgoing arrows."""
    if indexed is None or tip is None:
        return _empty_changes(remote, unavailable=tip is None)
    left = {str(node.get("id")): node for node in indexed.get("nodes") or [] if node.get("id")}
    right = {str(node.get("id")): node for node in tip.get("nodes") or [] if node.get("id")}
    added = [str(right[node_id].get("label") or node_id) for node_id in right if node_id not in left]
    removed = [str(left[node_id].get("label") or node_id) for node_id in left if node_id not in right]
    rewired = []
    for node_id in sorted(left.keys() & right.keys()):
        before = _outgoing(indexed, node_id)
        after = _outgoing(tip, node_id)
        if before == after:
            continue
        rewired.append({
            "id": node_id,
            "label": str(left[node_id].get("label") or node_id),
            "before": [_arrow(indexed, item) for item in before],
            "after": [_arrow(tip, item) for item in after],
        })
    unchanged = not added and not removed and not rewired
    return {
        "remote": remote[:12],
        "added": added[:_CHANGE_CAP],
        "removed": removed[:_CHANGE_CAP],
        "rewired": rewired[:_CHANGE_CAP],
        "added_count": len(added),
        "removed_count": len(removed),
        "rewired_count": len(rewired),
        "unchanged": unchanged,
    }


def _phrase_hits(edges: list[dict], by_id: dict[str, dict], asked: str) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        label = _normalise(str(edge.get("label") or ""))
        if " " not in label or label not in asked:
            continue
        source = by_id.get(str(edge.get("from") or ""))
        target = by_id.get(str(edge.get("to") or ""))
        if source is None or target is None:
            continue
        key = (str(source.get("id")), label, str(target.get("id")))
        if key in seen:
            continue
        seen.add(key)
        found.append(_answer(source, edge, target))
    return found


def _answer(source: dict, edge: dict, target: dict) -> dict:
    url = str(target.get("url") or "")
    return {
        "from_id": str(source.get("id") or ""),
        "from": str(source.get("label") or source.get("id") or ""),
        "label": str(edge.get("label") or ""),
        "to_id": str(target.get("id") or ""),
        "to": str(target.get("label") or target.get("id") or ""),
        "path": _safe_path(str(target.get("path") or "")),
        "url": url if url.startswith("https://github.com/") else "",
    }


def _label_score(label: str, question_tokens: set[str]) -> float:
    tokens = _significant(label)
    if not tokens:
        return 0.0
    shared = tokens & question_tokens
    if not shared or not any(len(token) >= 4 for token in shared):
        return 0.0
    return len(shared) / len(tokens)


def _significant(text: str) -> set[str]:
    return {
        token for token in _TOKEN.findall(_normalise(text))
        if token not in _STOP and token not in {"yes", "no"} and len(token) > 2
    }


def _polarity(tokens: list[str]) -> str:
    says_no = "no" in tokens
    says_yes = "yes" in tokens
    if says_no and not says_yes:
        return "no"
    if says_yes and not says_no:
        return "yes"
    return ""


def _normalise(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").replace("`", "'").split())


def _needles(node: dict) -> list[str]:
    found: list[str] = []
    node_id = str(node.get("id") or "")
    if "/" in node_id and ".." not in node_id.split("/"):
        found.append(f"/steps/{node_id}")
        found.append("steps_" + node_id.replace("/", "_"))
    path = _safe_path(str(node.get("path") or ""))
    name = path.rsplit("/", 1)[-1]
    page = "/page_objects/" in f"/{path}" or name.endswith("_page.rb")
    if page and name.endswith(".rb"):
        stem = name[:-3]
        if len(stem) >= 6:
            found.append(stem)
        class_name = "".join(part.capitalize() for part in stem.split("_") if part)
        if len(class_name) >= 6:
            found.append(class_name)
    return found


def _contains_name(text: str, needle: str) -> bool:
    """True when ``needle`` is present and not the start of a longer name."""
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return False
        before = text[found - 1] if found else ""
        end = found + len(needle)
        after = text[end] if end < len(text) else ""
        if before not in _NAME_CHAR and after not in _NAME_CHAR:
            return True
        start = end


_NAME_CHAR = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _feature_files(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for folder_name in ("features", "spec"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _SCAN_SUFFIXES:
                continue
            if path.stat().st_size > 1_000_000:
                continue
            relative = path.relative_to(root).as_posix()
            if any(part in {".git", "vendor", "node_modules"} for part in path.parts):
                continue
            try:
                found.append((relative, path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return found


def _locale_scopes(scopes: list[str], root: Path, steps: list[tuple[str, str]]) -> None:
    folder = root / "config" / "locales"
    if not folder.is_dir():
        return
    names = {part for step in steps for part in step}
    for path in sorted(folder.rglob("*.yml")):
        if path.name in {"en.yml", "cy.yml"} and path.parent == folder:
            continue
        if path.name.endswith((".cy.yml", "_cy.yml")) or ".cy." in path.name:
            continue
        relative = path.relative_to(root)
        if names.intersection(relative.parts):
            _add(scopes, relative.as_posix())


def _outgoing(chart: dict, node_id: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(
        (_normalise(str(edge.get("label") or "")), str(edge.get("to") or ""))
        for edge in chart.get("edges") or []
        if edge.get("from") == node_id
    ))


def _arrow(chart: dict, item: tuple[str, str]) -> str:
    label, target = item
    names = {
        str(node.get("id")): str(node.get("label") or node.get("id"))
        for node in chart.get("nodes") or []
        if node.get("id")
    }
    text = names.get(target, target)
    return f"{label} → {text}" if label else text


def _empty_changes(remote: str, *, unavailable: bool) -> dict:
    return {
        "remote": remote[:12],
        "added": [],
        "removed": [],
        "rewired": [],
        "added_count": 0,
        "removed_count": 0,
        "rewired_count": 0,
        "unchanged": False,
        "unavailable": unavailable,
    }


def _safe_path(path: str) -> str:
    relative = path.replace("\\", "/").lstrip("/")
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        return ""
    return relative


def _add(scopes: list[str], path: str) -> None:
    if path and path not in scopes:
        scopes.append(path)
