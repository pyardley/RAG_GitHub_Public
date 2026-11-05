"""Hybrid keyword and vector search, plus impact lookup."""

from __future__ import annotations

import re
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


def topic_terms(question: str) -> list[str]:
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", question.lower()):
        if len(word) < 3 or word in _LIST_WORDS or word in terms:
            continue
        terms.append(word)
    return terms


def search(conn, repository_ids: list[int], question: str, k: int) -> list[Hit]:
    from rag.embedder import Embedder

    if k < 1:
        raise ValueError("Result count must be at least 1")
    listing = asks_for_features(question)
    terms = topic_terms(question) if listing else []
    embedding = _vector_literal(Embedder().embed_query(question))
    sql = """
        WITH semantic AS (
            SELECT id, row_number() OVER (ORDER BY ord) AS rnk
            FROM (
                SELECT id, embedding <=> %(embedding)s::vector AS ord
                FROM chunks
                WHERE repository_id = ANY(%(repos)s)
                  AND (%(features_only)s = FALSE OR right(path, 8) = '.feature')
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
            },
        )
        hits = [_hit_from_row(row) for row in cur.fetchall()]
    if listing:
        hits = _feature_files(hits, terms, k)
        chain: list[Hit] = []
    else:
        chain = _feature_chain(conn, repository_ids, question, hits)
    conn.commit()
    return _with_chain(chain, hits, k)


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
