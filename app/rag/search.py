"""Hybrid keyword and vector search, plus impact lookup."""

from __future__ import annotations

from dataclasses import dataclass

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


def search(conn, repository_ids: list[int], question: str, k: int) -> list[Hit]:
    from rag.embedder import Embedder

    if k < 1:
        raise ValueError("Result count must be at least 1")
    embedding = _vector_literal(Embedder().embed_query(question))
    sql = """
        WITH semantic AS (
            SELECT id, row_number() OVER (ORDER BY ord) AS rnk
            FROM (
                SELECT id, embedding <=> %(embedding)s::vector AS ord
                FROM chunks
                WHERE repository_id = ANY(%(repos)s)
                ORDER BY ord
                LIMIT 50
            ) nearest
        ),
        lexical AS (
            SELECT id, row_number() OVER (ORDER BY rank DESC) AS rnk
            FROM (
                SELECT id, ts_rank_cd(tsv, plainto_tsquery('simple', %(query)s)) AS rank
                FROM chunks
                WHERE repository_id = ANY(%(repos)s)
                  AND numnode(plainto_tsquery('simple', %(query)s)) > 0
                  AND tsv @@ plainto_tsquery('simple', %(query)s)
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
            {"embedding": embedding, "repos": repository_ids, "query": question, "k": k},
        )
        hits = [_hit_from_row(row) for row in cur.fetchall()]
    conn.commit()
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
