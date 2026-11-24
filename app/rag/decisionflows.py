"""Journeys discovered from a decision tree, starting at a given step path.

The path names the first screen, for example ``/steps/opening/start_or_continue``.
The step's controller names the decision tree, and that tree's destination
says which screen comes next. A screen with no destination is followed through
the Continue link in its template. The usual answer is the spine. Another
answer is drawn as the next screen. Headings come from the English locale.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from rag.flows import (
    _dedupe,
    _humanize,
    _mark_choices,
    _node,
    _place,
    _read,
    _route,
    _short,
    _yaml_scalar,
)

_SKIP_DIRS = {".git", "tmp", "vendor", "node_modules", "coverage", "log", "dist"}
_NOTE = (
    "Each box opens that step in the repository. "
    "The main path is the usual way on. Another arrow is the answer that leaves the page."
)
_STEP_DECL = re.compile(r"\b(edit_step|show_step|crud_step)\s+:(\w+)")
_NAMESPACE = re.compile(r"\bnamespace\s+:(\w+)")
_CLASS_SUPER = re.compile(r"class\s+(\w+)\s*<\s*([\w:]+)")
_TREE_CLASS = re.compile(r"class\s+(\w*DecisionTree)\b")
_DEF = re.compile(r"(?m)^([ \t]*)def\s+([A-Za-z_][A-Za-z0-9_?!]*)\b")
_GOTO = re.compile(
    r"\b(edit|show)\(\s*(?::([A-Za-z0-9_]+)|['\"](/steps/[A-Za-z0-9_/]+)['\"])"
)
_QUESTION = re.compile(r"question\(\s*:(\w+)")
_BARE = re.compile(r"^(?:return\s+)?([a-z_][A-Za-z0-9_]*[?!]?)$")
_POSTFIX = re.compile(r"^(.*?)\s+(if|unless)\s+(.+)$")
_HELPER = re.compile(r"\b((?:edit_)?steps_[A-Za-z0-9_]+)_path\b")
_LINK = re.compile(r"(?:link_button|link_to|button_to)\s*\(?\s*['\"]([^'\"]+)['\"]")
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_ERB = re.compile(r"<%=?.*?%>", re.S)
_NOT_A_CALL = {
    "if", "unless", "else", "elsif", "end", "return", "raise", "case", "when",
    "and", "or", "not", "true", "false", "nil", "self", "super", "do", "begin",
    "rescue", "next", "break",
}
_SPINE_LIMIT = 72
_ORDINARY_GAP = 8


def decision_entry(root: Path) -> str:
    """The opening step drawn when no start path was given."""
    opening_start = ""
    for route in _routes(_read(root / "config/routes.rb")):
        if route["namespace"] != "opening":
            continue
        if route["step"] == "start_or_continue":
            return route["path"]
        if route["step"] == "start" and not opening_start:
            opening_start = route["path"]
    return opening_start


def decision_chart(root: Path, start: str) -> dict | None:
    """The journey that begins at ``start``, or None when that step is not here."""
    path = _normalise(start)
    if not path:
        return None
    routes = _routes(_read(root / "config/routes.rb"))
    by_path = {route["path"]: route for route in routes}
    if path not in by_path:
        return None
    classes = _classes(root)
    modules = _modules(root)
    trees = _trees(root)
    locales = _locales(root)
    helpers = _helpers(routes)
    context = {
        "root": root,
        "routes": by_path,
        "classes": classes,
        "modules": modules,
        "trees": trees,
        "locales": locales,
        "helpers": helpers,
        "edges": {},
    }
    edges_from = _edges_from(context, path)
    if not edges_from.get(path):
        return None
    spine = _spine(path, context)
    if len(spine) < 2:
        return None
    included = list(spine)
    seen = set(spine)
    drawn: list[dict] = []
    for step in spine:
        for edge in context["edges"].get(step, []):
            target = edge["to"]
            drawn.append(edge)
            if target not in seen:
                included.append(target)
                seen.add(target)
    nodes: dict[str, dict] = {}
    for step in included:
        node = _step_node(context, step)
        if node:
            nodes[node["id"]] = node
    edges = []
    for edge in drawn:
        source = _node_id(edge["from"])
        target = _node_id(edge["to"])
        if source in nodes and target in nodes and source != target:
            edges.append({"from": source, "to": target, "label": edge.get("label") or ""})
    edges = _dedupe(edges)
    if len(nodes) < 2 or not edges:
        return None
    outgoing = {edge["from"] for edge in edges}
    full_outgoing = set()
    for step, items in context["edges"].items():
        if items:
            full_outgoing.add(_node_id(step))
    order = [_node_id(step) for step in included if _node_id(step) in nodes]
    for node_id, node in nodes.items():
        if node_id not in outgoing and node_id not in full_outgoing:
            node["kind"] = "end"
    _mark_choices(nodes, edges)
    _place(list(nodes.values()), order)
    _route(list(nodes.values()), edges)
    title = nodes[order[0]]["label"] if order else "Journey"
    return {
        "title": title,
        "note": _NOTE,
        "reader": "decision",
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _normalise(start: str) -> str:
    text = start.strip()
    if not text:
        return ""
    if "://" in text:
        text = urlparse(text).path or ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    if text and not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/")
    parts = [part for part in text.split("/") if part]
    if len(parts) < 3 or parts[0] != "steps" or any(part in {".", ".."} for part in parts):
        return ""
    return "/" + "/".join(parts[:3])


def _routes(source: str) -> list[dict]:
    routes: list[dict] = []
    if not source:
        return routes
    namespaces: list[tuple[int, str]] = []
    depth = 0
    for raw in source.splitlines():
        line = _code(raw)
        if not line:
            continue
        opened = len(re.findall(r"\bdo\b", line))
        name = _NAMESPACE.search(line)
        if name and opened:
            depth += opened
            namespaces.append((depth, name.group(1)))
        elif opened:
            depth += opened
        declared = _STEP_DECL.search(line)
        names = [item[1] for item in namespaces]
        if declared and names and names[0] == "steps" and len(names) >= 2:
            namespace = names[-1]
            step = declared.group(2)
            action = "show" if declared.group(1) == "show_step" else "edit"
            routes.append({
                "path": f"/steps/{namespace}/{step}",
                "namespace": namespace,
                "step": step,
                "action": action,
            })
        closed = len(re.findall(r"^\s*end\b", raw))
        if closed:
            depth = max(0, depth - closed)
            namespaces = [item for item in namespaces if item[0] <= depth]
    return routes


def _helpers(routes: list[dict]) -> dict[str, str]:
    found: dict[str, str] = {}
    for route in routes:
        flat = f"steps_{route['namespace']}_{route['step']}"
        found[flat] = route["path"]
        if route["action"] == "edit":
            found[f"edit_{flat}"] = route["path"]
    return found


def _modules(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in _ruby_files(root / "app" / "controllers"):
        text = _read(path)
        for match in re.finditer(r"(?m)^module\s+(\w+)", text):
            found.setdefault(match.group(1), text)
    return found


def _concern_text(context: dict, controller: str) -> str:
    parts = [controller]
    for name in re.findall(r"(?m)^[ \t]*include\s+(\w+)\b", controller):
        extra = context["modules"].get(name)
        if extra:
            parts.append(extra)
    return "\n".join(parts)


def _classes(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in _ruby_files(root / "app" / "controllers"):
        text = _read(path)
        for match in _CLASS_SUPER.finditer(text):
            found.setdefault(match.group(1), path)
        for match in re.finditer(r"(?m)^[ \t]*class\s+(\w+)\b", text):
            found.setdefault(match.group(1), path)
    return found


def _trees(root: Path) -> dict[str, tuple[Path, dict[str, str]]]:
    found: dict[str, tuple[Path, dict[str, str]]] = {}
    for path in _ruby_files(root / "app" / "services"):
        text = _read(path)
        match = _TREE_CLASS.search(text)
        if not match:
            continue
        found[match.group(1)] = (path, _methods(text))
    return found


def _edges_from(context: dict, path: str) -> dict[str, list[dict]]:
    cached = context["edges"]
    if path in cached:
        return cached
    cached[path] = []
    route = context["routes"].get(path)
    if route is None:
        return cached
    tree = _tree_for(context, route)
    leaves: list[tuple[str, str, bool]] = []
    if tree is not None:
        _path, methods = tree
        controller = (
            context["root"] / "app" / "controllers" / "steps"
            / route["namespace"] / f"{route['step']}_controller.rb"
        )
        arm = _when_body(
            methods.get("destination", ""),
            route,
            _concern_text(context, _read(controller)),
        )
        if arm.strip():
            leaves = _leaves(
                arm,
                methods,
                route["namespace"],
                context["locales"],
                context["helpers"],
                context["routes"],
                _origin_namespace(context, path),
                "",
                True,
                (),
            )
    if not leaves:
        leaves = _view_leaves(context, route)
    if not leaves:
        leaves = _controller_leaves(context, route)
    edges = []
    for label, target, ordinary in leaves:
        if target == path or target not in context["routes"]:
            continue
        edges.append({
            "from": path,
            "to": target,
            "label": "" if label == "Otherwise" else _short(label, 48),
            "ordinary": ordinary,
        })
    cached[path] = _dedupe(edges)
    for edge in cached[path]:
        edge["ordinary"] = edge.get("ordinary", False)
    return cached


def _tree_for(context: dict, route: dict) -> tuple[Path, dict[str, str]] | None:
    root: Path = context["root"]
    controller = (
        root / "app" / "controllers" / "steps" / route["namespace"] / f"{route['step']}_controller.rb"
    )
    seen: set[str] = set()
    current = controller if controller.is_file() else None
    for _ in range(8):
        if current is None:
            return None
        text = _read(current)
        declared = re.search(r"def\s+decision_tree_class\s*\n\s*([A-Z][\w:]*)", text)
        if declared:
            name = declared.group(1).split("::")[-1]
            found = context["trees"].get(name)
            if found is None:
                return None
            return found[0], _methods_with_ancestors(context, name)
        match = _CLASS_SUPER.search(text)
        if not match:
            return None
        parent = match.group(2).split("::")[-1]
        if parent in seen:
            return None
        seen.add(parent)
        current = context["classes"].get(parent)
    return None


def _controller_leaves(context: dict, route: dict) -> list[tuple[str, str, bool]]:
    """Follow a Continue link whose path is chosen in the controller."""
    root: Path = context["root"]
    controller = root / "app" / "controllers" / "steps" / route["namespace"] / f"{route['step']}_controller.rb"
    text = _read(controller)
    if not text:
        return []
    view = _view_source(root, route["namespace"], route["step"])
    methods = _methods(text)
    names: list[str] = []
    for ivar in re.findall(r"(?:link_button|link_to|button_to)\s+[^,\n]+,\s*@(\w+)", view):
        if ivar in methods:
            names.append(ivar)
            continue
        assigned = re.search(rf"@{ivar}\s*=\s*(\w+)", text)
        if assigned and assigned.group(1) in methods:
            names.append(assigned.group(1))
    found: list[tuple[str, str, bool]] = []
    for name in names:
        found.extend(_leaves(
            methods[name],
            methods,
            route["namespace"],
            context["locales"],
            context["helpers"],
            context["routes"],
            _origin_namespace(context, route["path"]),
            "",
            True,
            (name,),
        ))
    return found


def _view_leaves(context: dict, route: dict) -> list[tuple[str, str, bool]]:
    folder = (
        context["root"] / "app" / "views" / "steps" / route["namespace"] / route["step"]
    )
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.erb")):
        if ".cy." in path.name:
            continue
        for raw in _read(path).splitlines():
            line = raw.strip()
            helper = _HELPER.search(line)
            if helper is None or ("link_button" not in line and "link_to" not in line and "button_to" not in line):
                continue
            target = context["helpers"].get(helper.group(1))
            if not target:
                continue
            label_match = _LINK.search(line)
            label = re.sub(r"\s+", " ", label_match.group(1)).strip() if label_match else "Continue"
            ordinary = label.lower() == "continue" or label.lower().startswith("continue ")
            found.append((label, target, ordinary))
    return found


def _leaves(
    body: str,
    methods: dict[str, str],
    namespace: str,
    locales: dict[str, str],
    helpers: dict[str, str],
    routes: dict[str, dict],
    origin: str,
    edge_label: str,
    ordinary: bool,
    stack: tuple[str, ...],
) -> list[tuple[str, str, bool]]:
    if len(stack) > 12:
        return []
    found: list[tuple[str, str, bool]] = []
    for kind, payload in _statements(body):
        if kind == "line":
            found.extend(_line_leaves(
                payload, methods, namespace, locales, helpers, routes, origin, edge_label, ordinary, stack,
            ))
        elif kind == "rescue":
            found.extend(_leaves(
                payload, methods, namespace, locales, helpers, routes, origin, "Error", False, stack,
            ))
        elif kind == "if":
            cond, then_body, elifs, else_body = payload
            then_label, else_label, ordinary_arm = _classify(cond, locales)
            found.extend(_leaves(
                then_body,
                methods,
                namespace,
                locales,
                helpers,
                routes,
                origin,
                _next_label(edge_label, then_label, ordinary_arm == "then", ordinary),
                ordinary and ordinary_arm == "then",
                stack,
            ))
            for elif_cond, elif_body in elifs:
                elif_label, _ignored, _arm = _classify(elif_cond, locales)
                found.extend(_leaves(
                    elif_body, methods, namespace, locales, helpers, routes, origin,
                    elif_label, False, stack,
                ))
            if else_body.strip():
                found.extend(_leaves(
                    else_body,
                    methods,
                    namespace,
                    locales,
                    helpers,
                    routes,
                    origin,
                    _next_label(edge_label, else_label, ordinary_arm == "else", ordinary),
                    ordinary and ordinary_arm == "else",
                    stack,
                ))
    return found


def _line_leaves(
    line: str,
    methods: dict[str, str],
    namespace: str,
    locales: dict[str, str],
    helpers: dict[str, str],
    routes: dict[str, dict],
    origin: str,
    edge_label: str,
    ordinary: bool,
    stack: tuple[str, ...],
) -> list[tuple[str, str, bool]]:
    postfix = _POSTFIX.match(line)
    if postfix and (_GOTO.search(postfix.group(1)) or _bare_name(postfix.group(1)) or _HELPER.search(postfix.group(1))):
        left, kind, cond = postfix.group(1), postfix.group(2), postfix.group(3)
        then_label, else_label, ordinary_arm = _classify(cond, locales)
        if kind == "unless":
            label = else_label if ordinary_arm == "then" else then_label
            is_ordinary = ordinary and ordinary_arm == "else"
        else:
            label = then_label
            is_ordinary = ordinary and ordinary_arm == "then"
        return _line_leaves(
            left.strip(), methods, namespace, locales, helpers, routes, origin, label, is_ordinary, stack,
        )
    destination = (
        _goto(line, namespace)
        or _helper_path(line, helpers)
        or _person_target(line, origin, routes)
        or _interpolated_step(line, namespace, routes)
    )
    if destination:
        return [(edge_label, destination, ordinary)]
    name = _bare_name(line)
    if name and name in methods and name not in stack and name not in _NOT_A_CALL:
        return _leaves(
            methods[name], methods, namespace, locales, helpers, routes, origin,
            edge_label, ordinary, stack + (name,),
        )
    return []


def _interpolated_step(line: str, namespace: str, routes: dict[str, dict]) -> str:
    """``edit("/steps/#{namespace}/address_details")`` stays in the section already being read."""
    if not namespace or namespace == "address":
        return ""
    match = re.search(r"""['\"]/steps/#\{[^}]+\}/(\w+)['\"]""", line)
    if not match:
        return ""
    path = f"/steps/{namespace}/{match.group(1)}"
    return path if path in routes else ""


def _person_target(line: str, origin: str, routes: dict[str, dict]) -> str:
    """A person's next step, resolved in the namespace that led into the address lookup."""
    if "person_url_for" not in line or not origin:
        return ""
    match = re.search(r"step:\s*:(\w+)", line)
    if not match:
        return ""
    path = f"/steps/{origin}/{match.group(1)}"
    return path if path in routes else ""


def _origin_namespace(context: dict, path: str, seen: tuple[str, ...] = ()) -> str:
    """The section that sent the journey into a shared step such as address lookup."""
    parts = [part for part in path.split("/") if part and part != "steps"]
    if parts and parts[0] not in {"address"}:
        return parts[0]
    if path in seen:
        return ""
    for source, edges in context["edges"].items():
        if any(edge["to"] == path for edge in edges):
            found = _origin_namespace(context, source, seen + (path,))
            if found:
                return found
    return ""


def _next_label(current: str, arm_label: str, arm_usual: bool, incoming_ordinary: bool) -> str:
    """Keep the earlier answer while it is still the usual way on."""
    if arm_usual and incoming_ordinary:
        return current or arm_label
    return arm_label or current


def _classify(cond: str, locales: dict[str, str]) -> tuple[str, str, str]:
    """Return the then label, the else label, and which arm is the usual way on."""
    text = cond.strip()
    attribute = ""
    question = _QUESTION.search(text)
    if question:
        attribute = question.group(1)
    if ".yes?" in text:
        return "Yes", "No", "else"
    if ".no?" in text:
        return "No", "Yes", "then"
    if ".new?" in text:
        return _option(locales, attribute, "new"), _option(locales, attribute, "continue"), "then"
    if ".continue?" in text:
        return _option(locales, attribute, "continue"), _option(locales, attribute, "new"), "else"
    bare = text[1:] if text.startswith("!") else text
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bare):
        if bare == "court":
            return ("No court", "Court found", "else") if text.startswith("!") else ("Court found", "No court", "then")
        pretty = _pretty(bare)
        if text.startswith("!"):
            return f"No {pretty.lower()}", pretty, "else"
        return pretty, f"No {pretty.lower()}", "then"
    if "?" in text:
        found = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\?", text)
        word = found.group(1) if found else ""
        for prefix in ("show_", "is_", "check_", "has_"):
            if word.startswith(prefix):
                word = word[len(prefix):]
                break
        pretty = _pretty(word) if word else _short(text, 40)
        return pretty, f"No {pretty[:1].lower()}{pretty[1:]}", "else"
    return _short(text, 40), "", "else"


def _pretty(word: str) -> str:
    return _humanize(word.replace("eligable", "eligible"))


def _option(locales: dict[str, str], attribute: str, value: str) -> str:
    if attribute:
        suffix = f"{attribute}_options.{value}"
        for key, label in locales.items():
            if key.endswith(suffix) and label:
                return label
    return _humanize(value)


def _helper_path(line: str, helpers: dict[str, str]) -> str:
    match = _HELPER.search(line)
    if not match:
        return ""
    return helpers.get(match.group(1), "")


def _goto(line: str, namespace: str) -> str:
    match = _GOTO.search(line)
    if not match:
        return ""
    if match.group(3):
        return _normalise(match.group(3))
    if match.group(2) and namespace:
        return f"/steps/{namespace}/{match.group(2)}"
    return ""


def _bare_name(line: str) -> str:
    if "=" in line and "==" not in line:
        return ""
    match = _BARE.match(line.strip())
    if not match or match.group(1) in _NOT_A_CALL:
        return ""
    return match.group(1)


def _statements(body: str) -> list[tuple]:
    lines = [line for line in (_code(raw) for raw in body.splitlines()) if line]
    statements = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("rescue"):
            statements.append(("rescue", "\n".join(lines[index + 1:])))
            break
        if _opens(line) and line.startswith(("if ", "if(", "unless ", "unless(")):
            block, index = _take_block(lines, index)
            statements.append(block)
            continue
        statements.append(("line", line))
        index += 1
    return statements


def _take_block(lines: list[str], start: int) -> tuple[tuple, int]:
    header = lines[start]
    inverted = header.startswith("unless")
    cond = re.sub(r"^(if|unless)\b\s*", "", header).strip()
    then_lines: list[str] = []
    elifs: list[tuple[str, list[str]]] = []
    else_lines: list[str] = []
    current = then_lines
    depth = 1
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if depth == 1 and (line.startswith("elsif ") or line == "else" or line.startswith("else ")):
            if line.startswith("elsif "):
                bucket: list[str] = []
                elifs.append((line[len("elsif "):].strip(), bucket))
                current = bucket
            else:
                current = else_lines
            index += 1
            continue
        if depth == 1 and line.startswith("end"):
            index += 1
            break
        current.append(line)
        depth += _opens(line)
        if line.startswith("end"):
            depth = max(1, depth - 1)
        index += 1
    then_text = "\n".join(then_lines)
    else_text = "\n".join(else_lines)
    elif_text = [(item_cond, "\n".join(item_lines)) for item_cond, item_lines in elifs]
    if inverted:
        return ("if", (cond, else_text, [], then_text)), index
    return ("if", (cond, then_text, elif_text, else_text)), index


def _opens(line: str) -> int:
    if re.match(r"^(if|unless|case|begin|while|until|for)\b", line):
        return 1
    if re.search(r"\bdo\b(?:\s*\|[^|]*\|)?\s*$", line):
        return 1
    return 0


def _when_body(destination: str, route: dict, controller: str = "") -> str:
    """The destination arm for this screen.

    The symbol is usually the route name. Some trees prefix it with the
    namespace, so ``/steps/miam/acknowledgement`` is ``:miam_acknowledgement``.
    A controller can name it with ``as: :court_acknowledgement``.
    """
    names = [route["step"], f"{route['namespace']}_{route['step']}"]
    for match in re.finditer(r"params\.fetch\(\s*:button\s*,\s*:(\w+)", controller):
        if match.group(1) not in names:
            names.append(match.group(1))
    for match in re.finditer(r"\bas:\s*:(\w+)", controller):
        if match.group(1) not in names:
            names.append(match.group(1))
    for name in names:
        body = _when_named(destination, name)
        if body.strip():
            return body
    return ""


def _when_named(destination: str, step: str) -> str:
    lines = destination.splitlines()
    collecting = False
    indent = 0
    chosen: list[str] = []
    for raw in lines:
        line = _code(raw)
        if not line:
            continue
        if not collecting:
            if re.match(rf"when\s+:{re.escape(step)}\b", line):
                collecting = True
                indent = len(raw) - len(raw.lstrip())
            continue
        if re.match(r"(when|else|end)\b", line) and (len(raw) - len(raw.lstrip())) <= indent:
            break
        chosen.append(raw)
    return "\n".join(chosen)


def _methods_with_ancestors(context: dict, name: str, seen: tuple[str, ...] = ()) -> dict[str, str]:
    """Methods of a decision tree, including those inherited from its parent."""
    found = context["trees"].get(name)
    if found is None or name in seen:
        return {}
    path, methods = found
    match = _CLASS_SUPER.search(_read(path))
    merged: dict[str, str] = {}
    if match:
        parent = match.group(2).split("::")[-1]
        merged.update(_methods_with_ancestors(context, parent, seen + (name,)))
    merged.update(methods)
    return merged


def _methods(text: str) -> dict[str, str]:
    matches = list(_DEF.finditer(text))
    found: dict[str, str] = {}
    for index, match in enumerate(matches):
        indent = len(match.group(1))
        end = len(text)
        for later in matches[index + 1:]:
            if len(later.group(1)) <= indent:
                end = later.start()
                break
        lines = text[match.end():end].splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and re.match(r"end\b", lines[-1].strip()) and (len(lines[-1]) - len(lines[-1].lstrip())) <= indent:
            lines.pop()
        found[match.group(2)] = "\n".join(lines)
    return found


def _spine(start: str, context: dict) -> list[str]:
    """Follow the usual answer, unless that answer ends and another one carries on."""
    def score(path: str, blocked: set[str]) -> int:
        """How far this step continues, ignoring steps already on the spine."""
        cache: dict[str, int] = {}
        visiting: set[str] = set()

        def walk(node: str) -> int:
            if node in blocked or node in visiting:
                return 0
            if node in cache:
                return cache[node]
            visiting.add(node)
            _edges_from(context, node)
            edges = [edge for edge in context["edges"].get(node, []) if edge["to"] != node]
            if not edges:
                visiting.remove(node)
                cache[node] = 1
                return 1
            best = 0
            opened = False
            for edge in edges:
                if edge["to"] in blocked:
                    continue
                opened = True
                best = max(best, walk(edge["to"]))
            visiting.remove(node)
            found = 1 + best if opened else 0
            cache[node] = found
            return found

        return walk(path)

    order = [start]
    seen = {start}
    current = start
    while len(order) < _SPINE_LIMIT:
        _edges_from(context, current)
        fresh = [
            edge for edge in context["edges"].get(current, [])
            if edge["to"] not in seen and edge["to"] != edge["from"]
        ]
        if not fresh:
            break
        live = [edge for edge in fresh if score(edge["to"], seen) > 0]
        ordinary = [edge for edge in fresh if edge.get("ordinary")]
        if live:
            best = max(score(edge["to"], seen) for edge in live)
            # The usual answer stays on the spine while it still reaches about as far.
            close = [
                edge for edge in live
                if edge.get("ordinary") and score(edge["to"], seen) >= best - _ORDINARY_GAP
            ]
            pool = close or live
        else:
            pool = ordinary or fresh
        pool.sort(
            key=lambda edge: (score(edge["to"], seen), 1 if edge.get("ordinary") else 0),
            reverse=True,
        )
        current = pool[0]["to"]
        order.append(current)
        seen.add(current)
    return order


def _step_node(context: dict, path: str) -> dict | None:
    route = context["routes"].get(path)
    if route is None:
        return None
    root: Path = context["root"]
    relative = f"app/controllers/steps/{route['namespace']}/{route['step']}_controller.rb"
    text = _read(root / relative)
    view = _view_source(root, route["namespace"], route["step"])
    label = _heading(context["locales"], route, view)
    step_id = _node_id(path)
    if text:
        class_name = ""
        match = re.search(r"class\s+(\w+)", text)
        if match:
            class_name = match.group(1)
        return _node(
            step_id,
            _short(label, 72),
            relative,
            text,
            [item for item in (f"class {class_name}", f"def {route['action']}") if item.strip()],
            detail=f"{label}\n{path}",
        )
    tree = _tree_for(context, route)
    if tree is None:
        return _node(step_id, _short(label, 72), relative, "", [], detail=f"{label}\n{path}")
    tree_path, _methods_of = tree
    return _node(
        step_id,
        _short(label, 72),
        tree_path.relative_to(root).as_posix(),
        _read(tree_path),
        [f"when :{route['step']}"],
        detail=f"{label}\n{path}",
    )


def _heading(locales: dict[str, str], route: dict, view: str) -> str:
    namespace = route["namespace"]
    step = route["step"]
    action = route["action"]
    for key in (
        f"steps.{namespace}.{step}.{action}.heading",
        f"steps.{namespace}.{step}.{action}.page_title",
        f"steps.{namespace}.{step}.heading",
    ):
        value = locales.get(key, "")
        if value and not value.startswith(("*", "{", "<")):
            text = re.sub(r"\s*%\{[^}]+\}(?:['’]s)?", "", value)
            text = re.sub(r"\s+(?:for|of)\s*$", "", text).strip()
            if text:
                return text
    found = _H1.search(view)
    if found:
        text = _ERB.sub("", found.group(1))
        text = _TAG.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    return _humanize(step)


def _view_source(root: Path, namespace: str, step: str) -> str:
    folder = root / "app" / "views" / "steps" / namespace / step
    if not folder.is_dir():
        return ""
    parts = []
    for path in sorted(folder.glob("*.erb")):
        if ".cy." in path.name:
            continue
        parts.append(_read(path))
    return "\n".join(parts)


def _node_id(path: str) -> str:
    parts = [part for part in path.split("/") if part and part != "steps"]
    return "/".join(parts)


def _locales(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    folder = root / "config" / "locales"
    if not folder.is_dir():
        return found
    for path in sorted(folder.rglob("*.yml")):
        name = path.name
        if name.endswith((".cy.yml", "_cy.yml")) or ".cy." in name:
            continue
        if not (name == "en.yml" or name.endswith(".en.yml") or name.endswith("_en.yml")):
            continue
        for key, value in _flatten_yaml(_read(path)).items():
            if key.startswith("en."):
                key = key[3:]
            found.setdefault(key, value)
    return found


def _flatten_yaml(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if line.startswith("-") or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip().strip("'\"")
        if not re.fullmatch(r"[A-Za-z0-9_]+", key):
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        rest = rest.strip()
        if not rest or rest[0] in "|>":
            stack.append((indent, key))
            continue
        value = _yaml_scalar(rest.split(" #", 1)[0])
        if not value:
            stack.append((indent, key))
            continue
        found.setdefault(".".join([item[1] for item in stack] + [key]), value)
    return found


def _code(line: str) -> str:
    stripped = re.sub(r"\s+#.*$", "", line).strip()
    return "" if stripped.startswith("#") else stripped


def _ruby_files(root: Path):
    if not root.is_dir():
        return
    for path in root.rglob("*.rb"):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path
