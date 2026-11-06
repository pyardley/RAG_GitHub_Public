"""Hybrid keyword and vector search, plus impact lookup."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

import psycopg

from rag.text import (
    has_declaration,
    identifier_regex,
    last_identifier,
    looks_like_path,
    path_matches,
    symbol_matches,
)


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    repo: str
    path: str
    start_line: int
    end_line: int
    symbol: str | None
    kind: str
    content: str
    score: float


def asks_for_features(question: str) -> bool:
    """A request for the feature files themselves, rather than how a step runs."""
    from rag.follow import feature_paths

    if feature_paths(question) or _HOW_IT_RUNS.search(question):
        return False
    return _FEATURE_LIST.search(question) is not None


def path_scopes(question: str) -> list[str]:
    """Directories named after under/in/from, such as 'under /app'."""
    found: list[str] = []
    for match in _PATH_SCOPE.finditer(question):
        raw = match.group(1).replace("\\", "/").strip().strip("/").rstrip(".")
        if not raw or "/" not in match.group(1) or raw in found:
            continue
        found.append(raw)
    return found


def code_literals(question: str) -> list[str]:
    """Numbers and identifiers to match exactly, ignoring the directory name."""
    found: list[str] = []
    for match in _CODE_LITERAL.finditer(question):
        if _inside_scope(match.start(), question) or _inside_quotes(match.start(), question):
            continue
        word = match.group(0)
        if word not in found:
            found.append(word)
    return found


def quoted_phrases(question: str) -> list[str]:
    """Multi-word text in quotes, such as a Cucumber step."""
    found: list[str] = []
    for match in _QUOTED_PHRASE.finditer(question):
        phrase = " ".join(match.group(1).split())
        if " " not in phrase or phrase in found:
            continue
        found.append(phrase)
    return found


def path_in_scope(path: str, scopes: list[str]) -> bool:
    if not scopes:
        return True
    lowered = path.replace("\\", "/").lower()
    for scope in scopes:
        prefix = scope.lower().strip("/")
        if lowered == prefix or lowered.startswith(prefix + "/"):
            return True
    return False


def topic_terms(question: str) -> list[str]:
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", question.lower()):
        if len(word) < 3 or word in _LIST_WORDS or word in terms:
            continue
        terms.append(word)
    return terms


def retrieve(conn, repository_ids: list[int], question: str, k: int, meter=None) -> tuple[list[Hit], list[str]]:
    """Search once, or once per query when a small model splits the question."""
    from rag.plan import search_queries
    from rag.usage import Stage

    scopes = path_scopes(question)
    literals = code_literals(question)
    phrases = quoted_phrases(question)
    terms = literals + [phrase for phrase in phrases if phrase not in literals]
    if terms and (scopes or phrases):
        hits, seconds = literal_hits(conn, repository_ids, scopes, terms, k)
        if hits:
            if meter is not None:
                if phrases and scopes:
                    note = "Looked for that text under the named directory."
                elif phrases:
                    note = "Looked for that text."
                else:
                    note = "Looked for those values under the named directory."
                meter.add(Stage("Plan the question", 0.0, note=note))
                meter.add_search(0.0, seconds)
                meter.finish_retrieval(len(hits), embedded=False)
            return hits, [question]
    queries = search_queries(question, meter)
    if len(queries) == 1:
        hits = search(conn, repository_ids, queries[0], k, meter, scopes)
    else:
        groups = [search(conn, repository_ids, query, k, meter, scopes) for query in queries]
        hits = merge_hits(groups, k)
    if meter is not None:
        meter.finish_retrieval(len(hits))
    return hits, queries


def merge_hits(groups: list[list[Hit]], k: int) -> list[Hit]:
    """Take turns from each search so one broad query cannot fill the list."""
    best: dict[int, Hit] = {}
    order: list[int] = []
    cursors = [0] * len(groups)
    while len(order) < k:
        progressed = False
        for index, hits in enumerate(groups):
            if cursors[index] >= len(hits):
                continue
            hit = hits[cursors[index]]
            cursors[index] += 1
            progressed = True
            current = best.get(hit.chunk_id)
            if current is None:
                best[hit.chunk_id] = hit
                order.append(hit.chunk_id)
                if len(order) >= k:
                    break
            elif hit.score > current.score:
                best[hit.chunk_id] = hit
        if not progressed:
            break
    return [best[chunk_id] for chunk_id in order]


def search(
    conn,
    repository_ids: list[int],
    question: str,
    k: int,
    meter=None,
    scopes: list[str] | None = None,
) -> list[Hit]:
    from rag.embedder import Embedder

    if k < 1:
        raise ValueError("Result count must be at least 1")
    listing = asks_for_features(question)
    terms = topic_terms(question) if listing else []
    embed_started = time.perf_counter()
    embedding = _vector_literal(Embedder().embed_query(question))
    embed_seconds = time.perf_counter() - embed_started
    database_started = time.perf_counter()
    sql = f"""
        WITH semantic AS (
            SELECT id, row_number() OVER (ORDER BY ord) AS rnk
            FROM (
                SELECT id, embedding <=> %(embedding)s::vector AS ord
                FROM chunks
                WHERE repository_id = ANY(%(repos)s)
                  AND (%(features_only)s = FALSE OR right(path, 8) = '.feature')
                  AND {_PATH_SCOPE_SQL}
                ORDER BY ord
                LIMIT 50
            ) nearest
        ),
        lexical AS (
            SELECT id, row_number() OVER (ORDER BY rank DESC) AS rnk
            FROM (
                SELECT id,
                       ts_rank_cd(
                           tsv,
                           CASE WHEN %(lexical_mode)s = 'or'
                                THEN to_tsquery('simple', %(lexical)s)
                                ELSE plainto_tsquery('simple', %(lexical)s)
                           END
                       ) AS rank
                FROM chunks
                WHERE repository_id = ANY(%(repos)s)
                  AND (%(features_only)s = FALSE OR right(path, 8) = '.feature')
                  AND {_PATH_SCOPE_SQL}
                  AND %(lexical_mode)s <> 'none'
                  AND CASE WHEN %(lexical_mode)s = 'or'
                           THEN tsv @@ to_tsquery('simple', %(lexical)s)
                           ELSE numnode(plainto_tsquery('simple', %(lexical)s)) > 0
                                AND tsv @@ plainto_tsquery('simple', %(lexical)s)
                      END
                ORDER BY rank DESC
                LIMIT 50
            ) matched
        )
        SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content,
               COALESCE(1.0 / (60 + s.rnk), 0) + COALESCE(1.0 / (60 + l.rnk), 0) AS score
        FROM chunks c
        JOIN repositories r ON r.id = c.repository_id
        LEFT JOIN semantic s ON s.id = c.id
        LEFT JOIN lexical l ON l.id = c.id
        WHERE s.id IS NOT NULL OR l.id IS NOT NULL
        ORDER BY score DESC, c.path, c.start_line
        LIMIT %(k)s
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "embedding": embedding,
                "repos": repository_ids,
                "lexical": " | ".join(terms) if terms else question,
                "lexical_mode": "or" if terms else "and",
                "features_only": listing,
                "k": max(k, 40) if listing else k,
                "scoped": bool(scopes),
                "scopes": [scope.lower().strip("/") for scope in (scopes or [])],
            },
        )
        hits = [_hit_from_row(row) for row in cur.fetchall()]
    if listing:
        hits = _feature_files(hits, terms, k)
        chain: list[Hit] = []
    else:
        chain = _feature_chain(conn, repository_ids, question, hits)
    conn.commit()
    hits = _with_chain(chain, hits, k)
    if meter is not None:
        meter.add_search(embed_seconds, time.perf_counter() - database_started)
    return hits


def impact(conn, repository_ids: list[int], target: str) -> dict[str, list[Hit]]:
    leaf = last_identifier(target).lower()
    params = {
        "repos": repository_ids,
        "exact": target.strip().lower(),
        "leaf": leaf,
        "path_term": target.strip().replace("\\", "/").lower(),
        "regex": identifier_regex(target),
    }
    rows, truncated = _impact_rows(conn, params)
    definitions: list[Hit] = []
    references: list[Hit] = []
    seen: set[int] = set()
    for row in rows:
        hit = _hit_from_row((*row, 0.0))
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        if (
            symbol_matches(hit.symbol, target)
            or path_matches(hit.path, target)
            or has_declaration(hit.content, target)
        ):
            definitions.append(hit)
        else:
            references.append(hit)
    references.sort(key=_reference_order)
    related = [
        hit
        for hit in search(conn, repository_ids, target, k=8)
        if hit.chunk_id not in seen
    ]
    return {
        "definitions": definitions[:40],
        "references": references[:40],
        "related": related,
        "definition_count": len(definitions),
        "reference_count": len(references),
        "truncated": truncated,
    }


def _impact_rows(conn, params: dict) -> tuple[list[tuple], bool]:
    selected = """
        SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content
        FROM chunks c
        JOIN repositories r ON r.id = c.repository_id
    """
    file_clause = """
        strpos(lower(c.path), '/' || %(leaf)s || '.') > 0
        OR left(lower(c.path), char_length(%(leaf)s) + 1) = %(leaf)s || '.'
    """
    exact = params["exact"]
    qualified = "#" in exact or "::" in exact or ("." in exact and not looks_like_path(exact))
    symbol_match = """
        lower(coalesce(c.symbol, '')) = %(exact)s
        OR right(lower(coalesce(c.symbol, '')), char_length(%(leaf)s) + 1) = '#' || %(leaf)s
        OR right(lower(coalesce(c.symbol, '')), char_length(%(leaf)s) + 1) = '.' || %(leaf)s
        OR right(lower(coalesce(c.symbol, '')), char_length(%(leaf)s) + 2) = '::' || %(leaf)s
    """
    if not qualified:
        symbol_match += f" OR {file_clause}"
    if looks_like_path(exact):
        located = f"""
            {selected}
            WHERE c.repository_id = ANY(%(repos)s)
              AND (strpos(lower(c.path), %(path_term)s) > 0 OR {file_clause})
        """
    else:
        located = f"""
            {selected}
            WHERE c.repository_id = ANY(%(repos)s)
              AND ({symbol_match})
        """
    located += " ORDER BY c.path, c.start_line LIMIT 80"
    mentioned = f"""
        {selected}
        WHERE c.repository_id = ANY(%(repos)s)
          AND c.content ~* %(regex)s
        ORDER BY c.path, c.start_line
        LIMIT 200
    """
    with conn.cursor() as cur:
        cur.execute(located, params)
        rows = list(cur.fetchall())
    truncated = len(rows) == 80
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(mentioned, params)
                content_rows = list(cur.fetchall())
    except psycopg.Error:
        content_rows = []
    else:
        truncated = truncated or len(content_rows) == 200
        rows.extend(content_rows)
    conn.commit()
    return rows, truncated


def _feature_chain(conn, repository_ids: list[int], question: str, hits: list[Hit]) -> list[Hit]:
    from rag.follow import feature_paths, follow_features

    paths = feature_paths(question)
    if not paths:
        paths = [hit.path for hit in hits if hit.path.endswith(".feature")][:1]
    if not paths:
        return []
    return follow_features(conn, repository_ids, paths)


def _with_chain(chain: list[Hit], hits: list[Hit], k: int) -> list[Hit]:
    if not chain:
        return hits
    merged: list[Hit] = []
    seen: set[int] = set()
    limit = min(16, max(k, len(chain)))
    for hit in chain + hits:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        merged.append(hit)
        if len(merged) >= limit:
            break
    return merged


def _boost_topic_paths(hits: list[Hit], terms: list[str]) -> list[Hit]:
    if not terms:
        return hits
    ranked: list[Hit] = []
    for hit in hits:
        path = hit.path.lower().replace("_", " ").replace("-", " ")
        extra = 0.25 * sum(term in path for term in terms)
        ranked.append(replace(hit, score=hit.score + extra))
    ranked.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
    return ranked


def _feature_files(hits: list[Hit], terms: list[str], k: int) -> list[Hit]:
    boosted = _boost_topic_paths(hits, terms)
    named = [hit for hit in boosted if _names_topic(hit, terms)]
    pool = named or boosted
    groups: dict[str, list[Hit]] = {}
    for hit in pool:
        groups.setdefault(hit.path, []).append(hit)
    chosen = [_threshold_scenario(group) for group in groups.values()]
    chosen.sort(key=lambda hit: (-max(item.score for item in groups[hit.path]), hit.path))
    return chosen[:k]


def _names_topic(hit: Hit, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = f"{hit.path} {hit.symbol or ''}".lower().replace("_", " ").replace("-", " ")
    return any(term in haystack for term in terms)


def _threshold_scenario(hits: list[Hit]) -> Hit:
    def rank(hit: Hit) -> tuple[bool, bool, float]:
        symbol = (hit.symbol or "").lower()
        text = f"{symbol}\n{hit.content}".lower()
        specific = "£" in text or "threshold" in text or "range" in text
        incidental = any(word in symbol for word in ("timeout", "navigating", "help with"))
        return (specific and not incidental, specific, hit.score)

    return max(hits, key=rank)


def _reference_order(hit: Hit) -> tuple:
    test = hit.path.startswith(("spec/", "features/", "test/", "tests/", "performance-tests/"))
    return (test, hit.repo, hit.path, hit.start_line)


def literal_hits(
    conn,
    repository_ids: list[int],
    scopes: list[str],
    literals: list[str],
    k: int,
) -> tuple[list[Hit], float]:
    """Chunks under the named directory whose text contains one of the values."""
    started = time.perf_counter()
    pattern = "(?:" + "|".join(identifier_regex(literal) for literal in literals) + ")"
    sql = f"""
        SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content, 0
        FROM chunks c
        JOIN repositories r ON r.id = c.repository_id
        WHERE c.repository_id = ANY(%(repos)s)
          AND {_PATH_SCOPE_SQL.replace("path", "c.path")}
          AND c.content ~* %(pattern)s
        ORDER BY c.path, c.start_line
        LIMIT 200
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "repos": repository_ids,
                "scoped": bool(scopes),
                "scopes": [scope.lower().strip("/") for scope in scopes] or [""],
                "pattern": pattern,
            },
        )
        hits = [_hit_from_row(row) for row in cur.fetchall()]
    conn.commit()
    hits.sort(key=lambda hit: (-_literal_count(hit, literals), hit.path, hit.start_line))
    if literals and all(" " in literal for literal in literals):
        hits = _each_path_first(hits)
    return hits[:k], time.perf_counter() - started


def _each_path_first(hits: list[Hit]) -> list[Hit]:
    """Show each file before a second chunk from a file already shown."""
    grouped: dict[str, list[Hit]] = {}
    for hit in hits:
        grouped.setdefault(hit.path, []).append(hit)
    first = [group[0] for group in grouped.values()]
    rest = [hit for group in grouped.values() for hit in group[1:]]
    return first + rest


def _literal_count(hit: Hit, literals: list[str]) -> int:
    text = hit.content.lower()
    return sum(literal.lower() in text for literal in literals)


def _inside_scope(start: int, question: str) -> bool:
    return any(match.start(1) <= start < match.end(1) for match in _PATH_SCOPE.finditer(question))


def _inside_quotes(start: int, question: str) -> bool:
    return any(match.start(1) <= start < match.end(1) for match in _QUOTED_PHRASE.finditer(question))


def _hit_from_row(row: tuple) -> Hit:
    return Hit(
        chunk_id=row[0],
        repo=row[1],
        path=row[2],
        start_line=row[3],
        end_line=row[4],
        symbol=row[5],
        kind=row[6],
        content=row[7],
        score=float(row[8]),
    )


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in values) + "]"


_PATH_SCOPE = re.compile(
    r"\b(?:under|in|inside|within|from)\s+"
    r"(/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]*)*)",
    re.IGNORECASE,
)
_CODE_LITERAL = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|\d{3,}|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"
)
_QUOTED_PHRASE = re.compile(r'[“"]([^"“”\n]+)[”"]')
_PATH_SCOPE_SQL = """(
    %(scoped)s = FALSE
    OR EXISTS (
        SELECT 1
        FROM unnest(%(scopes)s::text[]) AS scope(prefix)
        WHERE lower(path) = prefix
           OR left(lower(path), char_length(prefix) + 1) = prefix || '/'
    )
)"""
_FEATURE_LIST = re.compile(
    r"\b(?:show|list|find|which|what)\b.{0,80}\bfeatures?\b"
    r"|\bfeatures?\b.{0,40}\b(?:that|which|test|cover|exercise)\b"
    r"|\bfeature files?\b",
    re.IGNORECASE | re.DOTALL,
)
_HOW_IT_RUNS = re.compile(
    r"\b(?:how|implement(?:ed|ation|s)?|turned into|button|step definition|page object)\b",
    re.IGNORECASE,
)
_LIST_WORDS = {
    "show", "list", "find", "which", "what", "feature", "features", "file", "files",
    "that", "test", "tests", "testing", "the", "and", "for", "with", "from", "into",
    "about", "does", "are", "you", "all", "any", "please",
}
