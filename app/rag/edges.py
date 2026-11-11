"""Call edges used by impact analysis.

A direct call, a FactoryBot factory, a SitePrism section, an alias, or a
Cucumber step match becomes an edge from the caller to the callee. send,
public_send, define_method, and method_missing are recorded as gaps with no
target. Impact walks the direct edges a few hops and reports the gaps instead
of guessing a target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag.follow import definition_matches, page_symbol, step_needles, step_phrases
from rag.log import logger

_FACTORY_VERBS = r"build_list|create_list|build_stubbed|attributes_for|build|create"
_FACTORY_ARGS = (
    r":(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*,\s*\d+)?"
    r"(?P<traits>(?:\s*,\s*:[A-Za-z_]\w*)*)"
)
_FACTORY_CALL = re.compile(rf"\b(?:{_FACTORY_VERBS})\s*\(\s*{_FACTORY_ARGS}")
_FACTORY_TARGET = re.compile(
    rf"^(?:FactoryBot\s*(?:\.|::)\s*)?(?:{_FACTORY_VERBS})\s*\(\s*{_FACTORY_ARGS}(?P<rest>.*)\)\s*$"
)
# Bump when edge extraction changes so the next startup rebuilds stored edges.
_EDGE_VERSION = "8"
_FACTORY_VERB_NAMES = frozenset(_FACTORY_VERBS.split("|"))
# A bare name that more methods than this define is left unlinked.
_AMBIGUOUS_LIMIT = 5
CALL_ARROW = "→"
# Joins a caller to a method when the name is defined in more than one place.
POSSIBLE_ARROW = "⇢"
_ALIAS_METHOD = re.compile(
    r"alias_method\s+:([A-Za-z_]\w*[?!]?)\s*,\s*:([A-Za-z_]\w*[?!]?)"
)
_ALIAS = re.compile(
    r"(?m)^[ \t]*alias\s+([A-Za-z_]\w*[?!]?)\s+([A-Za-z_]\w*[?!]?)\s*$"
)
_CONST_CALL = re.compile(r"\b([A-Z][\w:]*)\.([a-z_][\w?!]*)")
_RECEIVER_CALL = re.compile(r"\b([a-z_][\w]*)\.([a-z_][\w?!]*)")
_BARE_CALL = re.compile(r"(?m)^[ \t]*([a-z_][\w?!]*)\s*(?:\(|$)")
# Used to find a bare call that shares its line with other code.
_HEREDOC = re.compile(r"(?ms)<<[~-]?['\"]?(\w+)['\"]?[^\n]*\n.*?^[ \t]*\1[ \t]*$")
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_INTERPOLATION = re.compile(r"#\{([^}]*)\}")
_WORD_LIST = re.compile(r"%[wWiI]\s*[\[({<][^\])}>]*[\])}>]")
_REGEX_LITERAL = re.compile(r"(?<=[(,=~\s])/(?=\S)(?:\\.|[^/\\\n])+/[imxo]*")
_COMMENT = re.compile(r"(?m)#.*$")
_STEP_HEADER = re.compile(r"(?m)^[ \t]*(?:Given|When|Then|And|But)\b.*$")
_DEF_HEAD = re.compile(r"\bdef\s+(?:self\.)?[A-Za-z_]\w*[?!=]?")
_DEF_PARAMS = re.compile(r"\bdef\s+(?:self\.)?[A-Za-z_]\w*[?!=]?\s*\(([^)]*)\)")
_BLOCK_PARAMS = re.compile(r"\|([^|\n]*)\|")
_ASSIGNED = re.compile(r"(?<![\w.@$:])([a-z_]\w*)\s*(?:\|\||&&|[-+*/%])?=(?![=~>])")
_MULTI_ASSIGNED = re.compile(r"(?m)^[ \t]*((?:[a-z_]\w*\s*,\s*)+[a-z_]\w*)\s*=(?!=)")
_RESCUED = re.compile(r"\brescue\b[^\n]*=>\s*([a-z_]\w*)")
_FOR_VARIABLE = re.compile(r"\bfor\s+([a-z_]\w*)\s+in\b")
_LET = re.compile(r"\b(?:let!?|subject)\s*[( ]\s*:([a-z_]\w*)")
_NAME = re.compile(r"[a-z_]\w*")
_CONSTANT = re.compile(r"[A-Z][\w:]*")
_INLINE_CALL = re.compile(r"(?<![\w.:@$&])([a-z_]\w*[?!]?)(?![\w:])")
_SUPERCLASS = re.compile(r"\bclass\s+([A-Z][\w:]*)\s*<\s*((?:::)?[A-Z][\w:]*)")
_TEST_FOLDERS = frozenset({"spec", "features", "test", "tests"})
# Names Capybara, RSpec, Rack, and Rails already define. A bare call by one
# of these is only linked inside the class that declares its own.
_FRAMEWORK_CALLS = frozenset({
    "page", "visit", "expect", "params", "request", "response", "session",
    "flash", "cookies", "render", "redirect_to", "get", "post", "put", "patch",
    "delete", "head", "raise", "puts", "require", "require_relative",
    "attribute", "validates", "validate", "describe", "context", "it",
    "before", "after", "let", "subject", "within", "have_content", "have_text",
    "included", "extended", "inherited",
})
_DYNAMIC = re.compile(
    r"(?m)^(?P<line>[ \t]*.*\b(?:public_send|__send__|define_method|method_missing)\b.*|"
    r"[ \t]*.*\bsend\s*[\(:].*)$"
)
_ELEMENT = re.compile(r"^\s*elements?\s+:([A-Za-z_]\w*)", re.MULTILINE)
_SECTION_SYMBOL = re.compile(r"^(?P<owner>.+) section :(?P<name>[A-Za-z_]\w*)$")
_SKIP_CALLS = {
    "click", "set", "text", "visible", "present", "blank", "nil", "empty",
    "each", "map", "select", "first", "last", "where", "find", "order",
    "to_s", "to_i", "call", "new", "tap", "itself", "present?", "blank?",
    "nil?", "empty?", "include", "exclude", "send", "public_send", "__send__",
}
_RUBY_KEYWORDS = {
    "alias", "and", "begin", "break", "case", "class", "def", "defined?",
    "do", "else", "elsif", "end", "ensure", "false", "for", "if", "in",
    "module", "next", "nil", "not", "or", "redo", "rescue", "retry",
    "return", "self", "super", "then", "true", "undef", "unless", "until",
    "when", "while", "yield",
}
_SKIP_RECEIVERS = {
    "RSpec", "FactoryBot", "SitePrism", "Capybara", "Rails", "ActiveRecord",
    "ApplicationRecord", "Time", "Date", "DateTime", "File", "Dir", "Pathname",
    "JSON", "YAML", "Logger", "BigDecimal", "URI", "SecureRandom", "Digest",
    "Base64", "CSV", "ERB", "Nokogiri", "ENV",
}


@dataclass(frozen=True)
class CallEdge:
    dst_symbol: str | None
    dst_chunk_id: int | None
    edge_type: str
    confidence: str
    detail: str


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: int
    path: str
    symbol: str | None
    kind: str
    content: str


def factory_symbols(target: str) -> list[str]:
    """Factory symbols named by an impact target written as a FactoryBot call.

    FactoryBot.create(:claim), create(:claim), and create_list(:claim, 3) all
    name factory :claim. A symbol argument is a trait. A list count is not.
    """
    match = _FACTORY_TARGET.match(target.strip())
    if not match:
        return []
    name = match.group("name")
    symbols = [f"factory :{name}"]
    for trait in re.findall(r":([A-Za-z_]\w*)", match.group("traits") or ""):
        symbols.append(f"factory :{name} trait :{trait}")
    return symbols


def extract_edges(path: str, chunks: list) -> list[tuple[int, CallEdge]]:
    """Edges inside one file. The index is the chunk's position in `chunks`."""
    if not path.endswith(".rb"):
        return []
    sections, elements, methods = _declarations(chunks)
    lets = {name for chunk in chunks for name in _LET.findall(chunk.content)}
    found: list[tuple[int, CallEdge]] = []
    seen: set[tuple] = set()

    def add(index: int, edge: CallEdge) -> None:
        if edge.dst_symbol and edge.dst_symbol == getattr(chunks[index], "symbol", None):
            return
        key = (index, edge.edge_type, edge.dst_symbol, edge.detail)
        if key in seen:
            return
        seen.add(key)
        found.append((index, edge))

    for index, chunk in enumerate(chunks):
        owner = _owner_name(chunk.symbol)
        for match in _DYNAMIC.finditer(chunk.content):
            add(index, CallEdge(None, None, "gap", "none", " ".join(match.group("line").split())[:160]))
        for match in _FACTORY_CALL.finditer(chunk.content):
            name = match.group("name")
            add(index, CallEdge(f"factory :{name}", None, "factory", "high", f"factory :{name}"))
            for trait in re.findall(r":([A-Za-z_]\w*)", match.group("traits") or ""):
                symbol = f"factory :{name} trait :{trait}"
                add(index, CallEdge(symbol, None, "factory", "high", symbol))
        for _new_name, old_name in _ALIAS_METHOD.findall(chunk.content) + _ALIAS.findall(chunk.content):
            target = f"{owner}#{old_name}" if owner else old_name
            add(index, CallEdge(target, None, "alias", "high", f"alias {old_name}"))
        for receiver, method in _CONST_CALL.findall(chunk.content):
            if receiver.split("::")[-1] in _SKIP_RECEIVERS or method in _SKIP_CALLS:
                continue
            symbol = f"{receiver}.{method}"
            add(index, CallEdge(symbol, None, "call", "high", symbol))
        for receiver, method in _RECEIVER_CALL.findall(chunk.content):
            if method in _SKIP_CALLS:
                continue
            section = sections.get((owner, receiver)) if owner else None
            if section and method in elements.get(section, ()):
                add(index, CallEdge(section, None, "call", "high", f"{receiver}.{method}"))
                continue
            if "step_definitions" in path.replace("\\", "/") and receiver.endswith("_page"):
                symbol = page_symbol(receiver, method)
                add(index, CallEdge(symbol, None, "call", "high", symbol))
        # A bare name in a factory sets an attribute. It is not a call.
        if (getattr(chunk, "kind", "") or "").startswith("factory"):
            continue
        own_line: set[str] = set()
        # An RSpec block is not a class, so its name is not a method owner.
        if owner and not _CONSTANT.fullmatch(owner):
            owner = None
        for method in _BARE_CALL.findall(chunk.content):
            if method in _SKIP_CALLS or method in _RUBY_KEYWORDS:
                continue
            own_line.add(method)
            # A method declared in this file keeps its Class#method name.
            # Any other bare call is stored by name and linked later to the
            # indexed methods that have that name.
            if owner:
                symbol = f"{owner}#{method}"
                if symbol in methods:
                    add(index, CallEdge(symbol, None, "call", "high", method))
                    continue
            if method in _FACTORY_VERB_NAMES:
                continue
            add(index, CallEdge(method, None, "call", "high", method))
        # A name inside a longer line, such as `total = fee_amount`, is only
        # kept later if an indexed method has that name.
        for method in inline_calls(chunk.content, lets):
            if method in own_line:
                continue
            if owner:
                symbol = f"{owner}#{method}"
                if symbol in methods:
                    add(index, CallEdge(symbol, None, "call", "high", method))
                    continue
            add(index, CallEdge(method, None, "inline", "high", method))
    return found


def inline_calls(content: str, known_locals: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Bare names in Ruby code that can be method calls.

    Strings, comments, symbols, keyword arguments, and a name with a receiver
    are left out. So is a name the code assigns, takes as a parameter, or
    declares with let, because that is a local value, not a call.
    """
    text = _HEREDOC.sub(" ", content)
    text = _STRING.sub(_string_calls, text)
    text = _WORD_LIST.sub(" ", text)
    text = _REGEX_LITERAL.sub(" ", text)
    text = _COMMENT.sub("", text)
    local = set(known_locals)
    for params in _DEF_PARAMS.findall(text) + _BLOCK_PARAMS.findall(text) + _MULTI_ASSIGNED.findall(text):
        local.update(_NAME.findall(params))
    local.update(_ASSIGNED.findall(text))
    local.update(_RESCUED.findall(text))
    local.update(_FOR_VARIABLE.findall(text))
    text = _STEP_HEADER.sub("", text)
    text = _DEF_HEAD.sub(" ", text)
    names: list[str] = []
    for name in _INLINE_CALL.findall(text):
        if (
            name in local
            or name in names
            or name in _SKIP_CALLS
            or name in _RUBY_KEYWORDS
            or name in _FACTORY_VERB_NAMES
        ):
            continue
        names.append(name)
    return names


def _string_calls(match: re.Match) -> str:
    """Keep the code inside #{...} of a double-quoted string and drop the text."""
    literal = match.group(0)
    if not literal.startswith('"'):
        return " "
    return " " + " ".join(_INTERPOLATION.findall(literal)) + " "


def link_steps(chunks: list[IndexedChunk]) -> list[tuple[int, CallEdge]]:
    """A feature step points at the step definition whose pattern matches it."""
    steps = [chunk for chunk in chunks if "step_definitions" in chunk.path.replace("\\", "/")]
    if not steps:
        return []
    found: list[tuple[int, CallEdge]] = []
    seen: set[tuple[int, int]] = set()
    for feature in chunks:
        if not feature.path.endswith(".feature"):
            continue
        for phrase in step_phrases(feature.content)[:30]:
            needles = [needle.lower() for needle in step_needles(phrase)]
            if not needles:
                continue
            for step in steps:
                haystack = step.content.lower()
                if not all(needle in haystack for needle in needles):
                    continue
                if not definition_matches(step.content, phrase):
                    continue
                key = (feature.chunk_id, step.chunk_id)
                if key in seen:
                    break
                seen.add(key)
                found.append((
                    feature.chunk_id,
                    CallEdge(step.symbol, step.chunk_id, "step", "high", phrase[:160]),
                ))
                break
    return found


def walk_callers(
    start_ids: set[int],
    incoming: dict[int, list[tuple[int, str]]],
    labels: dict[int, str],
    *,
    max_depth: int = 4,
    limit: int = 80,
    uncertain: set[tuple[int, int]] | None = None,
) -> tuple[list[tuple[int, str]], int]:
    """Callers of the start chunks, then callers of those, out to `max_depth`.

    Each result is the caller chunk and the path from that caller back to the
    start, using the chunk symbols. A cycle is not followed again. The second
    value is how many further callers were left out of the list. A
    (caller, callee) pair in `uncertain` is joined with the possible arrow.
    """
    uncertain = uncertain or set()
    visited = set(start_ids)
    frontier = [(chunk_id, labels.get(chunk_id, "")) for chunk_id in start_ids]
    found: list[tuple[int, str]] = []
    omitted = 0
    depth = 0
    while frontier and depth < max_depth:
        nxt: list[tuple[int, str]] = []
        for node, path in frontier:
            for src, _detail in incoming.get(node, []):
                if src in visited:
                    continue
                visited.add(src)
                label = labels.get(src) or "caller"
                arrow = POSSIBLE_ARROW if (src, node) in uncertain else CALL_ARROW
                via = f"{label} {arrow} {path}" if path else label
                if len(found) < limit:
                    found.append((src, via))
                else:
                    omitted += 1
                nxt.append((src, via))
        frontier = nxt
        depth += 1
    return found, omitted


def rebuild_edges(conn, repository_id: int) -> int:
    """Replace one repository's call edges from the chunks already stored."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, path, symbol, kind, content
            FROM chunks
            WHERE repository_id = %s
            ORDER BY path, start_line, id
            """,
            (repository_id,),
        )
        rows = cur.fetchall()
    grouped: dict[str, list[IndexedChunk]] = {}
    for row in rows:
        chunk = IndexedChunk(row[0], row[1], row[2], row[3], row[4])
        grouped.setdefault(chunk.path, []).append(chunk)
    index_of_calls = CallIndex([chunk for chunks in grouped.values() for chunk in chunks])
    pending: list[tuple] = []
    for path, chunks in grouped.items():
        for index, edge in extract_edges(path, chunks):
            src = chunks[index].chunk_id
            targets: list[int | None] = [edge.dst_chunk_id]
            confidence = edge.confidence
            if edge.dst_chunk_id is None and edge.dst_symbol:
                found, certain = index_of_calls.targets(
                    edge.dst_symbol, _owner_name(chunks[index].symbol), path,
                )
                found = [chunk_id for chunk_id in found if chunk_id != src]
                if found:
                    targets = list(found)
                    if not certain:
                        confidence = "low"
                elif edge.edge_type == "inline":
                    continue
            edge_type = "call" if edge.edge_type == "inline" else edge.edge_type
            for dst in targets:
                pending.append((
                    repository_id, src, dst, edge.dst_symbol,
                    edge_type, confidence, edge.detail,
                ))
    flat = [chunk for chunks in grouped.values() for chunk in chunks]
    for src, edge in link_steps(flat):
        pending.append((
            repository_id, src, edge.dst_chunk_id, edge.dst_symbol,
            edge.edge_type, edge.confidence, edge.detail,
        ))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM edges WHERE repository_id = %s", (repository_id,))
        if pending:
            cur.executemany(
                """
                INSERT INTO edges (
                    repository_id, src_chunk_id, dst_chunk_id, dst_symbol,
                    edge_type, confidence, detail
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                pending,
            )
        cur.execute("SELECT count(*) FROM chunks WHERE repository_id = %s", (repository_id,))
        chunk_count = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (f"call_edges:{repository_id}", f"{chunk_count}:{_EDGE_VERSION}"),
        )
    conn.commit()
    logger.info("call edges repository=%s edges=%s chunks=%s", repository_id, len(pending), chunk_count)
    return len(pending)


def prepare_call_edges(conn, repository_ids: list[int] | None = None) -> None:
    """Build edges for indexed repositories that do not have them yet."""
    sql = """
        SELECT r.id, r.name,
               (SELECT count(*) FROM chunks c WHERE c.repository_id = r.id)
        FROM repositories r
    """
    params: tuple = ()
    if repository_ids is not None:
        sql += " WHERE r.id = ANY(%s)"
        params = (repository_ids,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        repos = cur.fetchall()
        cur.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'call_edges:%%'"
        )
        ready = {row[0]: row[1] for row in cur.fetchall()}
    for repository_id, name, chunk_count in repos:
        if chunk_count == 0:
            continue
        if ready.get(f"call_edges:{repository_id}") == f"{chunk_count}:{_EDGE_VERSION}":
            continue
        logger.info("building call edges for %s", name)
        rebuild_edges(conn, repository_id)


@dataclass(frozen=True)
class _Method:
    chunk_id: int
    owner: str | None
    path: str


class CallIndex:
    """What a call symbol can mean in one repository's chunks.

    A qualified symbol names one chunk. A bare name is a call with no
    receiver, so Ruby can only find it in the caller's own class, a class it
    inherits from, a module, or a method defined outside any class. A method
    of an unrelated class is never the target.
    """

    def __init__(self, chunks: list[IndexedChunk]) -> None:
        self._by_symbol: dict[str, int] = {}
        self._kinds: dict[str, str] = {}
        self._owner_paths: dict[str, set[str]] = {}
        self._parents: dict[str, str] = {}
        self._methods: dict[str, dict[tuple[str, str], _Method]] = {}
        self._method_ids: dict[str, int] = {}
        self._texts: dict[str, list[str]] = {}
        declared: list[tuple[str, str]] = []
        for chunk in chunks:
            self._texts.setdefault(chunk.path, []).append(chunk.content)
            if not chunk.symbol:
                continue
            symbol = chunk.symbol.lower()
            self._by_symbol.setdefault(symbol, chunk.chunk_id)
            if chunk.kind in ("class", "module"):
                self._kinds.setdefault(symbol, chunk.kind)
                self._owner_paths.setdefault(symbol, set()).add(chunk.path)
                if chunk.kind == "class":
                    leaf = chunk.symbol.rsplit("::", 1)[-1]
                    for name, parent in _SUPERCLASS.findall(chunk.content):
                        if name.rsplit("::", 1)[-1] == leaf:
                            declared.append((chunk.symbol, parent))
                            break
            # An RSpec block named '#save' is not a method, and a method split
            # over two chunks is still one method.
            elif chunk.kind == "method" and " " not in chunk.symbol:
                parts = re.split(r"[#.]", chunk.symbol)
                owner = chunk.symbol[: -len(parts[-1]) - 1] if len(parts) > 1 else None
                self._method_ids.setdefault(symbol, chunk.chunk_id)
                self._methods.setdefault(parts[-1].lower(), {}).setdefault(
                    (symbol, chunk.path), _Method(chunk.chunk_id, owner, chunk.path),
                )
        for symbol, parent in declared:
            found = self._constant(parent, symbol)
            if found and found != symbol.lower():
                self._parents.setdefault(symbol.lower(), found)

    def targets(
        self, dst_symbol: str, owner: str | None = None, path: str = "",
    ) -> tuple[list[int], bool]:
        """Chunks the symbol can mean, and whether that reading is certain.

        One reachable method of a bare name is certain, a few are each
        possible, and more than `_AMBIGUOUS_LIMIT` are left unlinked.
        """
        if any(mark in dst_symbol for mark in ("#", " ", ".", ":")):
            found = self._by_symbol.get(dst_symbol.lower())
            return ([found] if found is not None else []), True
        name = dst_symbol.lower()
        family = self._family(owner)
        for relative in family:
            for separator in ("#", "."):
                own = self._method_ids.get(f"{relative}{separator}{name}")
                if own is not None:
                    return [own], True
        if name in _FRAMEWORK_CALLS:
            return [], True
        reachable = [
            method
            for method in self._methods.get(name, {}).values()
            if self._reachable(method, family, path)
        ]
        # A method written in the caller's own file is the one it calls.
        local = [method for method in reachable if method.path == path]
        candidates = [method.chunk_id for method in local or reachable]
        if len(candidates) == 1:
            return candidates, True
        if 1 < len(candidates) <= _AMBIGUOUS_LIMIT:
            return candidates, False
        return [], True

    def _reachable(self, method: _Method, family: list[str], path: str) -> bool:
        owner = (method.owner or "").lower()
        if self._kinds.get(owner) == "class":
            return False
        if method.path != path:
            # A method written in a spec file is local to that file, and
            # application code does not call test support code.
            if method.path.endswith("_spec.rb") or (_is_test(method.path) and not _is_test(path)):
                return False
        if method.owner and family and self._kinds.get(family[0]) == "class":
            # A class only has a module's methods when it includes the module.
            leaf = re.escape(method.owner.rsplit("::", 1)[-1])
            paths = {path}
            for relative in family:
                paths |= self._owner_paths.get(relative, set())
            mentioned = re.compile(rf"\b{leaf}\b")
            return any(
                mentioned.search(text)
                for file_path in paths
                for text in self._texts.get(file_path, [])
            )
        return True

    def _family(self, owner: str | None) -> list[str]:
        """The caller's class followed by the classes it inherits from."""
        if not owner or not _CONSTANT.fullmatch(owner):
            return []
        family = [owner.lower()]
        while len(family) < 8:
            parent = self._parents.get(family[-1])
            if parent is None or parent in family:
                break
            family.append(parent)
        return family

    def _constant(self, name: str, inside: str) -> str | None:
        """Resolve a class name the way Ruby does from inside a namespace."""
        name = name.removeprefix("::").lower()
        scope = inside.lower().split("::")[:-1]
        while scope:
            candidate = "::".join(scope + [name])
            if self._kinds.get(candidate) == "class":
                return candidate
            scope.pop()
        if self._kinds.get(name) == "class":
            return name
        ending = f"::{name}"
        matches = [
            symbol for symbol, kind in self._kinds.items()
            if kind == "class" and symbol.endswith(ending)
        ]
        return matches[0] if len(matches) == 1 else None


def _is_test(path: str) -> bool:
    return bool(_TEST_FOLDERS.intersection(path.replace("\\", "/").split("/")[:-1]))


def _declarations(chunks: list) -> tuple[dict, dict, set[str]]:
    sections: dict[tuple[str, str], str] = {}
    elements: dict[str, set[str]] = {}
    methods: set[str] = set()
    for chunk in chunks:
        symbol = chunk.symbol or ""
        matched = _SECTION_SYMBOL.match(symbol)
        if matched:
            sections[(matched.group("owner"), matched.group("name"))] = symbol
            elements[symbol] = set(_ELEMENT.findall(chunk.content))
        if "#" in symbol:
            methods.add(symbol)
    return sections, elements, methods


def _owner_name(symbol: str | None) -> str | None:
    if not symbol or symbol.startswith("factory ") or symbol.startswith("trait "):
        return None
    if " section :" in symbol:
        return symbol.split(" section :", 1)[0]
    if "#" in symbol:
        return symbol.split("#", 1)[0]
    if "." in symbol:
        return symbol.split(".", 1)[0]
    if re.fullmatch(r"[A-Z][\w:]*", symbol):
        return symbol
    return None
