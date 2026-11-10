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
_EDGE_VERSION = "3"
_ALIAS_METHOD = re.compile(
    r"alias_method\s+:([A-Za-z_]\w*[?!]?)\s*,\s*:([A-Za-z_]\w*[?!]?)"
)
_ALIAS = re.compile(
    r"(?m)^[ \t]*alias\s+([A-Za-z_]\w*[?!]?)\s+([A-Za-z_]\w*[?!]?)\s*$"
)
_CONST_CALL = re.compile(r"\b([A-Z][\w:]*)\.([a-z_][\w?!]*)")
_RECEIVER_CALL = re.compile(r"\b([a-z_][\w]*)\.([a-z_][\w?!]*)")
_BARE_CALL = re.compile(r"(?m)^[ \t]*([a-z_][\w?!]*)\s*(?:\(|$)")
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
        for method in _BARE_CALL.findall(chunk.content):
            if method in _SKIP_CALLS or method in _RUBY_KEYWORDS:
                continue
            # A method declared in this file keeps its Class#method name.
            # Any other bare call is stored by name and linked later when
            # only one indexed method has that name.
            if owner:
                symbol = f"{owner}#{method}"
                if symbol in methods:
                    add(index, CallEdge(symbol, None, "call", "high", method))
                    continue
            add(index, CallEdge(method, None, "call", "high", method))
    return found


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
) -> tuple[list[tuple[int, str]], int]:
    """Callers of the start chunks, then callers of those, out to `max_depth`.

    Each result is the caller chunk and the path from that caller back to the
    start, using the chunk symbols. A cycle is not followed again. The second
    value is how many further callers were left out of the list.
    """
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
                via = f"{label} → {path}" if path else label
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
    by_symbol: dict[str, int] = {}
    by_leaf: dict[str, int | None] = {}
    labels: dict[int, str] = {}
    for row in rows:
        chunk = IndexedChunk(row[0], row[1], row[2], row[3], row[4])
        grouped.setdefault(chunk.path, []).append(chunk)
        labels[chunk.chunk_id] = chunk.symbol or chunk.path
        if chunk.symbol and chunk.symbol.lower() not in by_symbol:
            by_symbol[chunk.symbol.lower()] = chunk.chunk_id
        if chunk.symbol and "#" in chunk.symbol:
            leaf = chunk.symbol.rsplit("#", 1)[1].lower()
            if leaf not in by_leaf:
                by_leaf[leaf] = chunk.chunk_id
            elif by_leaf[leaf] != chunk.chunk_id:
                by_leaf[leaf] = None
    pending: list[tuple] = []
    for path, chunks in grouped.items():
        for index, edge in extract_edges(path, chunks):
            dst = edge.dst_chunk_id
            if dst is None and edge.dst_symbol:
                dst = resolve_call_target(edge.dst_symbol, by_symbol, by_leaf)
            pending.append((
                repository_id, chunks[index].chunk_id, dst, edge.dst_symbol,
                edge.edge_type, edge.confidence, edge.detail,
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


def resolve_call_target(
    dst_symbol: str,
    by_symbol: dict[str, int],
    by_leaf: dict[str, int | None],
) -> int | None:
    """Chunk for a call symbol. A bare name matches only a unique method."""
    found = by_symbol.get(dst_symbol.lower())
    if found is not None:
        return found
    if "#" in dst_symbol or " " in dst_symbol:
        return None
    return by_leaf.get(dst_symbol.lower())


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
