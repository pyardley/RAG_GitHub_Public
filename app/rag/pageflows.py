"""Journeys discovered from page objects and the code that clicks through them.

A page is a Ruby class that names a heading and at least one submit button.
A journey starts on a page that sets its own URL and offers two or more of
those buttons, which is the usual Start and Return pair. The longest path
that begins there is the spine. Another button on that first page is a second
arrow, and an if/else that reaches a different page is a branch. Labels and
headings come from the English locale files in the checkout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rag.flows import _dedupe, _mark_choices, _node, _place, _route, _short, seal_chart

_SKIP_DIRS = {".git", "tmp", "vendor", "node_modules", "coverage", "log", "dist"}
_HEADER = re.compile(
    r"element\s+:(?:page_header|header)\s*,\s*:(?:page_title|main_header|content_header)\s*,\s*:?['\"]([^'\"]+)['\"]"
)
_DESCRIPTION = re.compile(
    r"element\s+:description\s*,[^,\n]*,\s*:?['\"]([^'\"]+)['\"]"
)
_BUTTON = re.compile(
    r"gds_submit_button\s+:(\w+)\s*,\s*(?:t\(\s*)?:?['\"]([^'\"]+)['\"]"
)
_FIELD = re.compile(
    r"gds_(?!submit)\w+\s+:\w+\s*,\s*(?:t\(\s*)?:?['\"]([^'\"]+)['\"]"
)
_SET_URL = re.compile(r"set_url\s+(\S+)")
_CLASS = re.compile(r"(?m)^([ \t]*)class\s+(\w+)")
_MODULE = re.compile(r"(?m)^[ \t]*module\s+(\w+)")
_ACCESSOR = re.compile(
    r"(?m)^[ \t]*def\s+(\w+)\b[^\n]*\n(?:[ \t]*#[^\n]*\n)*[ \t]*((?:::)?(?:\w+::)*\w+)\.new\b"
)
_BLOCK = re.compile(
    r"(?m)^([ \t]*)(?:def\s+([A-Za-z_]\w*)\b|(?:Given|When|Then|And)\b.*\bdo)\b"
)
_TOKEN = re.compile(r"(?<![\w.@:])([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?")
_YES = re.compile(r"\.set\(\s*:yes\b")
_NO = re.compile(r"\.set\(\s*:no\b")
_SKIP_CALLS = {
    "if", "unless", "else", "elsif", "end", "do", "def", "class", "module",
    "return", "raise", "case", "when", "and", "or", "not", "true", "false",
    "nil", "self", "super", "yield", "begin", "rescue", "ensure", "while",
    "until", "for", "next", "break", "retry", "then", "in", "expect", "eq",
    "be", "include", "change", "match", "raise", "page", "log_event",
}

_NOTE = "Each box opens that page in the repository. An arrow is the button that leaves the page."


@dataclass
class Page:
    class_name: str
    full_name: str
    path: str
    text: str
    header_key: str
    description_key: str
    field_key: str
    buttons: dict[str, str]
    actions: dict[str, str]
    entry: bool
    id: str = ""
    label: str = ""


@dataclass
class Routine:
    name: str
    body: str
    path: str
    order: int
    follower: Routine | None = None


def page_object_charts(root: Path) -> list[dict]:
    """One chart for each start page that a journey actually clicks through."""
    found = _pages(root)
    if not found:
        return []
    locales = _locales(root)
    seen: set[int] = set()
    unique: list[Page] = []
    for page in found.values():
        if id(page) in seen:
            continue
        seen.add(id(page))
        unique.append(page)
        page.label = _page_label(page, locales)
    accessors = _accessors(root, found)
    if not accessors:
        return []
    for page in found.values():
        names = [name for name, target in accessors.items() if target is page]
        page.id = names[0] if names else page.class_name
    routines = _routines(root, found)
    charts = []
    for page in unique:
        if not page.entry or not page.id:
            continue
        chart = _chart_for(page, found, accessors, routines, locales)
        if chart:
            charts.append(chart)
    return charts


def _chart_for(
    entry: Page,
    pages: dict[str, Page],
    accessors: dict[str, Page],
    routines: list[Routine],
    locales: dict[str, str],
) -> dict | None:
    by_name = {routine.name: routine for routine in routines if routine.name}
    sequences_of: dict[int, list[list[tuple[str, str]]]] = {}

    def sequences(routine: Routine, stack: tuple[str, ...]) -> list[list[tuple[str, str]]]:
        key = id(routine)
        if key in sequences_of and not stack:
            return sequences_of[key]
        if routine.name and routine.name in stack:
            return [[]]
        walked = _walk(routine.body, stack + ((routine.name,) if routine.name else ()), by_name, accessors)
        if not stack:
            sequences_of[key] = walked
        return walked

    best_score = 0
    best: tuple[Routine, list[tuple[str, str]]] | None = None
    best_sequences: list[list[tuple[str, str]]] = []
    for routine in routines:
        if not _reaches(routine.body, entry, by_name):
            continue
        walked = sequences(routine, ())
        for seq in walked:
            if not seq or seq[0][0] != entry.id:
                continue
            score = len({page_id for page_id, _action in seq})
            if score > best_score:
                best_score = score
                best = (routine, seq)
                best_sequences = [item for item in walked if item and item[0][0] == entry.id]
    if best is None or best_score < 4:
        return None
    _routine, spine = best
    spine = max(best_sequences, key=lambda seq: len({page_id for page_id, _action in seq}))
    edges: list[dict] = []
    for seq in best_sequences:
        edges.extend(_edges_from(seq, pages, locales))
    _alternate_starts(edges, entry, routines, by_name, accessors, pages, locales)
    _prefer_yes_no(edges)
    _name_unlabeled(edges, pages, locales)
    for edge in edges:
        edge.pop("hint", None)
    edges = _dedupe(edges)
    edges = [edge for edge in edges if edge["from"] != edge["to"]]
    if len(edges) < 3:
        return None
    order: list[str] = []
    for page_id, _action in spine:
        if page_id not in order:
            order.append(page_id)
    seen = set(order)
    for edge in edges:
        for page_id in (edge["from"], edge["to"]):
            if page_id not in seen:
                order.append(page_id)
                seen.add(page_id)
    nodes: dict[str, dict] = {}
    id_to_page = {page.id: page for page in pages.values() if page.id}
    for page_id in order:
        page = id_to_page.get(page_id)
        if page is None:
            continue
        full = locales.get(page.header_key) or page.label
        label = _distinct_label(page, locales, full, {node["label"] for node in nodes.values()})
        nodes[page_id] = _node(
            page_id,
            label,
            page.path,
            page.text,
            [item for item in (page.header_key, page.field_key, f"class {page.class_name}") if item],
            detail=f"{full}\n{page.path}",
        )
    if len(nodes) < 4:
        return None
    last = next((page_id for page_id in reversed(order) if page_id in nodes and page_id in {item[0] for item in spine}), "")
    outgoing = {edge["from"] for edge in edges}
    if last and last not in outgoing:
        nodes[last]["kind"] = "end"
    side = {
        edge["to"]: (entry.id, 1)
        for edge in edges
        if edge["from"] == entry.id and edge["to"] not in {item[0] for item in spine}
    }
    _mark_choices(nodes, edges)
    placed = [page_id for page_id in order if page_id in nodes]
    _place(list(nodes.values()), placed, shared=side)
    _route(list(nodes.values()), edges)
    description = locales.get(entry.description_key, "")
    note = f"{description} {_NOTE}" if description else _NOTE
    spine_ids: list[str] = []
    for page_id, _action in spine:
        if page_id not in spine_ids:
            spine_ids.append(page_id)
    return seal_chart({
        "title": entry.label,
        "note": note,
        "nodes": list(nodes.values()),
        "edges": edges,
    }, spine_ids)


def _reaches(body: str, entry: Page, by_name: dict[str, Routine], depth: int = 0) -> bool:
    """True when this text, or a method it calls, clicks the start page."""
    if entry.id in body:
        return True
    if depth > 4:
        return False
    for match in _TOKEN.finditer(body):
        name, method = match.group(1), match.group(2)
        if method or name not in by_name or name in _SKIP_CALLS:
            continue
        if _reaches(by_name[name].body, entry, by_name, depth + 1):
            return True
    return False


def _alternate_starts(
    edges: list[dict],
    entry: Page,
    routines: list[Routine],
    by_name: dict[str, Routine],
    accessors: dict[str, Page],
    pages: dict[str, Page],
    locales: dict[str, str],
) -> None:
    """A second button on the start page, followed into the next step if needed."""
    used = {edge["label"] for edge in edges if edge["from"] == entry.id}
    for routine in routines:
        if entry.id not in routine.body and not any(
            action in routine.body for action in entry.actions
        ):
            continue
        for action, button in entry.actions.items():
            label = locales.get(entry.buttons.get(button, ""), "") or _human_action(action)
            if label in used or action not in routine.body:
                continue
            if not re.search(rf"(?<![\w]){re.escape(entry.id)}\.{re.escape(action)}\b", routine.body):
                continue
            target = _destination_after(routine, action, entry.id, by_name, accessors)
            if not target or target == entry.id:
                continue
            edges.append({"from": entry.id, "to": target, "label": label})
            used.add(label)


def _destination_after(
    routine: Routine,
    action: str,
    entry_id: str,
    by_name: dict[str, Routine],
    accessors: dict[str, Page],
) -> str:
    walked = _walk(routine.body, (routine.name,) if routine.name else (), by_name, accessors)
    for seq in walked:
        seen = False
        for page_id, called in seq:
            if page_id == entry_id and called == action:
                seen = True
                continue
            if seen and page_id != entry_id:
                return page_id
    nxt = _next_routine(routine)
    if nxt is None:
        return ""
    for match in _TOKEN.finditer(nxt.body):
        name, method = match.group(1), match.group(2)
        if method and name in accessors and accessors[name].id != entry_id:
            return accessors[name].id
    return ""


def _next_routine(routine: Routine) -> Routine | None:
    # The following step in the same file is filled in by the caller via file_index.
    return getattr(routine, "follower", None)


def _edges_from(seq: list[tuple[str, str]], pages: dict[str, Page], locales: dict[str, str]) -> list[dict]:
    edges = []
    previous = ""
    label = ""
    hint = ""
    id_to_page = {page.id: page for page in pages.values() if page.id}
    for page_id, raw_action in seq:
        action, action_hint = _split_action(raw_action)
        if previous and page_id != previous:
            edge = {"from": previous, "to": page_id, "label": label}
            if hint:
                edge["hint"] = hint
            edges.append(edge)
            label = ""
            hint = ""
        previous = page_id
        if action_hint:
            hint = action_hint
        page = id_to_page.get(page_id)
        if page and action in page.actions:
            button = page.actions[action]
            label = locales.get(page.buttons.get(button, ""), "") or _human_action(action)
    return edges


def _split_action(action: str) -> tuple[str, str]:
    if "\0" not in action:
        return action, ""
    called, hint = action.split("\0", 1)
    return called, hint


def _prefer_yes_no(edges: list[dict]) -> None:
    """When both arms leave on the same button, name them from the :yes arm."""
    grouped: dict[str, list[dict]] = {}
    for edge in edges:
        grouped.setdefault(edge["from"], []).append(edge)
    for group in grouped.values():
        labels = {edge["label"] for edge in group}
        hints = [edge.get("hint", "") for edge in group]
        if len(group) < 2 or len(labels) != 1 or "Yes" not in hints:
            continue
        if not next(iter(labels)):
            continue
        for edge in group:
            edge["label"] = "Yes" if edge.get("hint") == "Yes" else "No"


def _name_unlabeled(edges: list[dict], pages: dict[str, Page], locales: dict[str, str]) -> None:
    """An arrow with no button click takes the page's other submit button."""
    id_to_page = {page.id: page for page in pages.values() if page.id}
    used: dict[str, set[str]] = {}
    for edge in edges:
        if edge["label"]:
            used.setdefault(edge["from"], set()).add(edge["label"])
    for edge in edges:
        if edge["label"]:
            continue
        page = id_to_page.get(edge["from"])
        if page is None:
            continue
        spare = []
        for button, key in page.buttons.items():
            text = locales.get(key, "")
            if text and text not in used.get(edge["from"], set()):
                spare.append(text)
        if len(spare) == 1:
            edge["label"] = spare[0]
            used.setdefault(edge["from"], set()).add(spare[0])


def _walk(
    body: str,
    stack: tuple[str, ...],
    routines: dict[str, Routine],
    accessors: dict[str, Page],
) -> list[list[tuple[str, str]]]:
    if len(stack) > 16:
        return [[]]
    variants = _branch_variants(body, accessors)
    if len(variants) > 1:
        found: list[list[tuple[str, str]]] = []
        for text, hint, hint_page in variants:
            for seq in _walk_linear(text, stack, routines, accessors):
                if hint:
                    seq = _tag(seq, hint, hint_page)
                found.append(seq)
                if len(found) >= 8:
                    return _longest(found)
        return _longest(found) or [[]]
    return _walk_linear(body, stack, routines, accessors)


def _tag(seq: list[tuple[str, str]], hint: str, hint_page: str) -> list[tuple[str, str]]:
    """Stash the branch answer on the first action that leaves the branched page."""
    tagged = []
    armed = bool(hint_page)
    for page_id, action in seq:
        if armed and page_id == hint_page:
            tagged.append((page_id, f"{action}\0{hint}"))
            armed = False
        else:
            tagged.append((page_id, action))
    return tagged


def _walk_linear(
    body: str,
    stack: tuple[str, ...],
    routines: dict[str, Routine],
    accessors: dict[str, Page],
) -> list[list[tuple[str, str]]]:
    for match in _TOKEN.finditer(body):
        name, method = match.group(1), match.group(2)
        rest = body[match.end():]
        if method and name in accessors:
            event = (accessors[name].id, method)
            tails = _walk(rest, stack, routines, accessors)
            return _combine([[event]], tails)
        if method or name in _SKIP_CALLS or name not in routines or name in stack:
            continue
        heads = _walk(routines[name].body, stack + (name,), routines, accessors)
        tails = _walk(rest, stack, routines, accessors)
        return _combine(heads, tails)
    return [[]]


def _combine(
    heads: list[list[tuple[str, str]]],
    tails: list[list[tuple[str, str]]],
) -> list[list[tuple[str, str]]]:
    heads = _longest(heads)
    tails = _longest(tails)
    if len(heads) * len(tails) > 8:
        heads = heads[:3]
        tails = tails[:3]
    return _longest([head + tail for head in heads for tail in tails]) or [[]]


def _longest(sequences: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    """Keep the longest paths, and a shorter one when it visits a different set of pages."""
    unique: dict[tuple, list[tuple[str, str]]] = {}
    for seq in sequences:
        unique.setdefault(tuple(seq), seq)
    ordered = sorted(unique.values(), key=lambda seq: len({page for page, _action in seq}), reverse=True)
    kept: list[list[tuple[str, str]]] = []
    seen: set[frozenset[str]] = set()
    for seq in ordered:
        pages = frozenset(page for page, _action in seq)
        if pages in seen and len(kept) >= 2:
            continue
        seen.add(pages)
        kept.append(seq)
        if len(kept) >= 6:
            break
    return kept


def _branch_variants(body: str, accessors: dict[str, Page]) -> list[tuple[str, str, str]]:
    """Split an if/else whose arms visit different pages. Each item is text, hint, page."""
    split = _first_page_if(body, accessors)
    if split is None:
        return [(body, "", "")]
    before, yes, no, after, yes_hint, no_hint, hint_page = split
    found = []
    for mid, hint in ((yes, yes_hint), (no, no_hint)):
        text = "\n".join(part for part in (before, mid, after) if part)
        arm = []
        for variant, nested_hint, nested_page in _branch_variants(text, accessors):
            arm.append((variant, hint or nested_hint, hint_page or nested_page))
            if len(arm) >= 4:
                break
        found.extend(arm)
    return found or [(body, "", "")]


def _first_page_if(body: str, accessors: dict[str, Page]):
    lines = body.splitlines()
    depth = 0
    start = else_at = end_at = None
    for index, line in enumerate(lines):
        stripped = _code(line)
        if not stripped:
            continue
        if start is None:
            if depth == 0 and _is_if(stripped):
                start = index
                depth = 1
                continue
            depth = max(0, depth + _opens(stripped) - _closes(stripped))
            continue
        if _is_else(stripped) and depth == 1 and else_at is None:
            else_at = index
            continue
        depth += _opens(stripped)
        depth -= _closes(stripped)
        if depth <= 0:
            end_at = index
            break
    if start is None or end_at is None:
        return None
    yes_lines = lines[start + 1: else_at if else_at is not None else end_at]
    no_lines = lines[else_at + 1: end_at] if else_at is not None else []
    yes = "\n".join(yes_lines)
    no = "\n".join(no_lines)
    if _page_ids(yes, accessors) == _page_ids(no, accessors):
        # This if only fills fields. Keep looking for one that changes the page.
        skipped = "\n".join([*lines[:start], *yes_lines, *no_lines, *lines[end_at + 1:]])
        return _first_page_if(skipped, accessors)
    before = "\n".join(lines[:start])
    after = "\n".join(lines[end_at + 1:])
    hint_page = _first_page(yes, accessors) or _first_page(no, accessors)
    yes_hint = "Yes" if _YES.search(yes) and not _NO.search(yes) else ""
    no_hint = "No" if _NO.search(no) and not _YES.search(no) else ""
    return before, yes, no, after, yes_hint, no_hint, hint_page


def _page_ids(text: str, accessors: dict[str, Page]) -> set[str]:
    return {
        accessors[match.group(1)].id
        for match in _TOKEN.finditer(text)
        if match.group(2) and match.group(1) in accessors
    }


def _first_page(text: str, accessors: dict[str, Page]) -> str:
    for match in _TOKEN.finditer(text):
        if match.group(2) and match.group(1) in accessors:
            return accessors[match.group(1)].id
    return ""


def _code(line: str) -> str:
    return re.sub(r"\s+#.*$", "", line).strip()


def _is_if(stripped: str) -> bool:
    return stripped.startswith("if ") or stripped.startswith("if(") or stripped.startswith("unless ") or stripped == "if"


def _is_else(stripped: str) -> bool:
    return stripped == "else" or stripped.startswith("else ") or stripped.startswith("elsif ")


def _opens(stripped: str) -> int:
    opens = 0
    if _is_if(stripped) or stripped.startswith("case ") or stripped == "case":
        opens += 1
    if re.search(r"\bdo\b(?:\s*\|[^|]*\|)?\s*$", stripped):
        opens += 1
    return opens


def _closes(stripped: str) -> int:
    return 1 if re.match(r"end\b", stripped) else 0


def _pages(root: Path) -> dict[str, Page]:
    found: dict[str, Page] = {}
    short: dict[str, list[str]] = {}
    for path in _ruby_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        modules = _MODULE.findall(text)
        for class_name, body in _classes(text):
            buttons = {match.group(1): match.group(2) for match in _BUTTON.finditer(body)}
            header = _HEADER.search(body)
            field = _FIELD.search(body)
            in_page_objects = "page_objects" in path.relative_to(root).parts
            if not buttons and not header and not field and not in_page_objects:
                continue
            full = "::".join([*modules, class_name])
            url = _SET_URL.search(body)
            external = url is not None and ("http://" in url.group(1) or "https://" in url.group(1))
            quoted = url is not None and url.group(1).startswith(("'", '"'))
            entry = len(buttons) >= 2 and url is not None and not external and not quoted
            description = _DESCRIPTION.search(body)
            page = Page(
                class_name=class_name,
                full_name=full,
                path=path.relative_to(root).as_posix(),
                text=text,
                header_key=header.group(1) if header else "",
                description_key=description.group(1) if description else "",
                field_key=field.group(1) if field else "",
                buttons=buttons,
                actions=_actions(body, buttons),
                entry=entry,
            )
            found[full] = page
            short.setdefault(class_name, []).append(full)
    for name, fulls in short.items():
        if len(fulls) == 1:
            found[name] = found[fulls[0]]
    return found


def _classes(text: str):
    matches = list(_CLASS.finditer(text))
    for index, match in enumerate(matches):
        indent = len(match.group(1))
        end = len(text)
        for later in matches[index + 1:]:
            if len(later.group(1)) <= indent:
                end = later.start()
                break
        yield match.group(2), text[match.end():end]


def _actions(body: str, buttons: dict[str, str]) -> dict[str, str]:
    actions = {name: name for name in buttons}
    for name, method_body in _method_bodies(body):
        for button in buttons:
            if re.search(rf"(?<![\w]){re.escape(button)}\.(?:click|submit)\b", method_body):
                actions[name] = button
                break
    return actions


def _method_bodies(text: str) -> list[tuple[str, str]]:
    bodies = []
    for match in _BLOCK.finditer(text):
        if not match.group(2):
            continue
        indent = len(match.group(1))
        line_end = text.find("\n", match.end())
        start = match.end() if line_end < 0 else line_end + 1
        end = _block_end(text, start, indent, defining=True)
        bodies.append((match.group(2), text[start:end]))
    return bodies


def _routines(root: Path, pages: dict[str, Page]) -> list[Routine]:
    page_files = {page.path for page in pages.values()}
    routines: list[Routine] = []
    order = 0
    for path in _ruby_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in page_files:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        file_routines: list[Routine] = []
        for match in _BLOCK.finditer(text):
            indent = len(match.group(1))
            line_end = text.find("\n", match.end())
            start = match.end() if line_end < 0 else line_end + 1
            end = _block_end(text, start, indent, defining=bool(match.group(2)))
            routine = Routine(match.group(2) or "", text[start:end], relative, order)
            order += 1
            file_routines.append(routine)
            routines.append(routine)
        for earlier, later in zip(file_routines, file_routines[1:]):
            earlier.follower = later
    return routines


def _block_end(text: str, start: int, indent: int, defining: bool) -> int:
    offset = start
    while offset < len(text):
        line_end = text.find("\n", offset)
        if line_end < 0:
            line_end = len(text)
        line = text[offset:line_end]
        stripped = line.strip()
        own = len(line) - len(line.lstrip(" "))
        if defining and own <= indent and re.match(r"def\s+", stripped):
            return offset
        if own <= indent and re.match(r"end\b", stripped):
            return offset
        offset = line_end + 1
    return len(text)


def _accessors(root: Path, pages: dict[str, Page]) -> dict[str, Page]:
    found: dict[str, Page] = {}
    for path in _ruby_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _ACCESSOR.finditer(text):
            constant = match.group(2).lstrip(":")
            page = pages.get(constant)
            if page is not None:
                found[match.group(1)] = page
    return found


def _locales(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for path in _locale_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for key, value, alias in _flatten_locale(text):
            if alias:
                aliases.setdefault(key, value)
            else:
                values.setdefault(key, value)
    for _turn in range(5):
        pending = False
        for key, target in aliases.items():
            if key in values:
                continue
            if target in values:
                values[key] = values[target]
            else:
                pending = True
        if not pending:
            break
    return values


def _flatten_locale(text: str) -> list[tuple[str, str, bool]]:
    rows = []
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.lstrip().startswith("-"):
            continue
        match = re.match(r"^(\s*)(?:\"([^\"]+)\"|'([^']+)'|([^:'\"#][^:]*?)):\s*(.*?)\s*$", raw)
        if not match:
            continue
        indent = len(match.group(1).replace("\t", "  "))
        key = match.group(2) or match.group(3) or (match.group(4) or "").strip()
        value = match.group(5).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        parts = [part for _indent, part in stack]
        if parts and parts[0] == "en":
            parts = parts[1:]
        if not parts:
            continue
        dotted = ".".join(parts)
        if not value or value.startswith(("|", ">")):
            continue
        alias = value.startswith(":")
        scalar = _scalar(value[1:].strip() if alias else value)
        if scalar:
            rows.append((dotted, scalar, alias))
    return rows


def _scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in "'\"":
        quote = value[0]
        end = value.find(quote, 1)
        if end > 1:
            return value[1:end]
        return ""
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    return value


def _locale_files(root: Path):
    paths = []
    for path in root.rglob("*.yml"):
        if _skipped(root, path):
            continue
        name = path.name
        if ".cy." in name or name.endswith("_cy.yml"):
            continue
        if name == "en.yml" or ".en." in name or name.endswith("_en.yml"):
            paths.append(path)
    return sorted(paths)


def _skipped(root: Path, path: Path) -> bool:
    """A skipped directory inside the checkout, not an ancestor such as /tmp."""
    return any(part in _SKIP_DIRS for part in path.relative_to(root).parts)


def _ruby_files(root: Path):
    paths = []
    for path in root.rglob("*.rb"):
        if _skipped(root, path):
            continue
        paths.append(path)
    return sorted(paths)


def _page_label(page: Page, locales: dict[str, str]) -> str:
    text = locales.get(page.header_key, "")
    if not text and page.field_key:
        text = locales.get(f"{page.field_key}.label", "") or locales.get(page.field_key, "")
        if len(text) > 120:
            text = ""
    return _short(text or _class_label(page.class_name))


def _distinct_label(page: Page, locales: dict[str, str], full: str, used: set[str]) -> str:
    """A second page with the same heading uses another heading from its file."""
    label = _short(full) if full else page.label
    if label not in used:
        return label
    ranked: list[tuple[int, str]] = []
    for key in re.findall(r":?['\"]([^'\"]*header)['\"]", page.text):
        value = locales.get(key, "")
        if not value or value == full or len(value) > 80:
            continue
        if key.endswith("steps_header"):
            rank = 0
        elif key.endswith("subheader") or "sub_header" in key:
            rank = 2
        else:
            rank = 1
        ranked.append((rank, value))
    if not ranked:
        return label
    ranked.sort()
    return _short(ranked[0][1])


def _class_label(name: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip()
    if not words:
        return name
    return words[:1].upper() + words[1:].lower()


def _human_action(action: str) -> str:
    return action.replace("_", " ").strip().capitalize()
