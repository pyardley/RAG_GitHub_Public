"""Hybrid keyword and vector search, plus impact lookup."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

import psycopg

from rag.edges import factory_symbols
from rag.log import log_retrieval, logger
from rag.text import (
    has_declaration,
    identifier_regex,
    last_identifier,
    looks_like_path,
    path_matches,
    symbol_matches,
)


_IMPACT_LIST_LIMIT = 80
_CALLER_WALK_LIMIT = 80
_EDGE_HOP_LIMIT = 400
_KNOWN_CALLER_LIMIT = 400


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
    via: str = ""


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
        raw = _clean_path(match.group(1))
        if not raw or "/" not in match.group(1) or raw in found:
            continue
        found.append(raw)
    return found


def path_exclusions(question: str) -> list[str]:
    """Folders named after ignore/except, such as 'ignore /features/support'."""
    found: list[str] = []
    for match in _NOT_UNDER.finditer(question):
        _add_path(found, match.group(1))
    for cue in _EXCLUDE_CUE.finditer(question):
        segment = question[cue.end():]
        stop = re.search(r"[.?!]", segment)
        if stop:
            segment = segment[:stop.start()]
        for match in _SLASH_PATH.finditer(segment):
            _add_path(found, match.group(0))
    return found


def path_excluded(path: str, excluded: list[str]) -> bool:
    if not excluded:
        return False
    lowered = path.replace("\\", "/").lower()
    for prefix in excluded:
        cleaned = prefix.lower().strip("/")
        if lowered == cleaned or lowered.startswith(cleaned + "/"):
            return True
    return False


def code_literals(question: str) -> list[str]:
    """Numbers and identifiers to match exactly, ignoring the directory name."""
    found: list[str] = []
    for match in _CODE_LITERAL.finditer(question):
        if (
            _inside_scope(match.start(), question)
            or _inside_quotes(match.start(), question)
            or _inside_excluded_path(match.start(), question)
        ):
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


def retrieve(conn, repository_ids: list[int], question: str, k: int, meter=None) -> tuple[list[Hit], list[str], bool]:
    """Search once, or once per query when a small model splits the question."""
    from rag.plan import search_queries
    from rag.usage import Stage

    excluded = path_exclusions(question)
    scopes = [
        scope for scope in path_scopes(question)
        if not path_excluded(scope, excluded)
    ]
    literals = code_literals(question)
    phrases = quoted_phrases(question)
    terms = literals + [phrase for phrase in phrases if phrase not in literals]
    if terms and (scopes or phrases):
        started = time.perf_counter()
        packed = literal_hits(conn, repository_ids, scopes, terms, k, excluded)
        hits = packed[0]
        truncated = bool(packed[2]) if len(packed) > 2 else False
        if hits and phrases:
            from rag.follow import follow_matched_steps

            hits = _with_follow(hits, follow_matched_steps(conn, repository_ids, question, hits), k)
        hits = _drop_excluded(hits, excluded)
        if hits or phrases:
            if meter is not None:
                meter.add(Stage("Plan the question", 0.0, note=_lookup_note(terms, phrases, scopes)))
                meter.add_search(0.0, time.perf_counter() - started)
                meter.finish_retrieval(
                    len(hits), embedded=False, direct=_direct_kind(terms, phrases), skipped=excluded,
                )
            return _logged(
                question, conn, repository_ids, k, _direct_kind(terms, phrases),
                [question], [], scopes, excluded, truncated, hits,
            )
    planned = search_queries(question, meter)
    queries = _without_scope_queries(planned, scopes)
    dropped = [query for query in planned if query not in queries]
    groups: list[list[Hit]] = []
    truncated = False
    for query in queries:
        found, extra = search(conn, repository_ids, query, k, meter, scopes, excluded)
        groups.append(found)
        truncated = truncated or extra
    hits = merge_hits(groups, k)
    shown = {hit.chunk_id for hit in hits}
    truncated = truncated or any(hit.chunk_id not in shown for group in groups for hit in group)
    if meter is not None:
        meter.finish_retrieval(len(hits), skipped=excluded)
    return _logged(
        question, conn, repository_ids, k, "semantic",
        queries, dropped, scopes, excluded, truncated, hits,
    )


def _logged(question, conn, repository_ids, k, path, queries, dropped, scopes, excluded, truncated, hits):
    log_retrieval(
        question=question,
        repos=_repo_names(conn, repository_ids, hits),
        k=k,
        path=path,
        queries=queries,
        scopes=scopes,
        excluded=excluded,
        dropped=dropped,
        truncated=truncated,
        hits=hits,
    )
    return hits, queries, truncated


def _repo_names(conn, repository_ids: list[int], hits: list[Hit]) -> list[str]:
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM repositories WHERE id = ANY(%s) ORDER BY name",
                    (repository_ids,),
                )
                names = [row[0] for row in cur.fetchall()]
        except psycopg.Error:
            names = []
        if names:
            return names
    return sorted({hit.repo for hit in hits})


def merge_hits(groups: list[list[Hit]], k: int) -> list[Hit]:
    """Keep the closest passage for each chunk, then the closest passages overall."""
    best: dict[int, Hit] = {}
    for group in groups:
        for hit in group:
            current = best.get(hit.chunk_id)
            if current is None or hit.score > current.score:
                best[hit.chunk_id] = hit
    ordered = sorted(best.values(), key=lambda hit: (-hit.score, hit.path, hit.start_line))
    return ordered[:k]


def _without_scope_queries(queries: list[str], scopes: list[str]) -> list[str]:
    """Drop a planned search that only repeats the directory already used as a filter."""
    scope_words = {part.lower() for scope in scopes for part in scope.split("/") if part}
    if not scope_words:
        return queries
    kept: list[str] = []
    for query in queries:
        words = {word for word in re.findall(r"[A-Za-z0-9]+", query.lower()) if len(word) > 2}
        if words and words <= scope_words:
            continue
        kept.append(query)
    return kept or queries[:1]


def search(
    conn,
    repository_ids: list[int],
    question: str,
    k: int,
    meter=None,
    scopes: list[str] | None = None,
    excluded: list[str] | None = None,
) -> tuple[list[Hit], bool]:
    """Return the passages and whether more matches were cut off by the limit."""
    from rag.embedder import Embedder

    if k < 1:
        raise ValueError("Result count must be at least 1")
    embed_started = time.perf_counter()
    embedding = _vector_literal(Embedder().embed_query(question))
    embed_seconds = time.perf_counter() - embed_started
    database_started = time.perf_counter()
    if len(repository_ids) > 1:
        groups: list[list[Hit]] = []
        truncated = False
        for repo_id in repository_ids:
            found, extra = _search_with_embedding(
                conn, [repo_id], question, k, embedding, scopes, excluded,
            )
            groups.append(found)
            truncated = truncated or extra
        hits = merge_hits(groups, k)
        shown = {hit.chunk_id for hit in hits}
        truncated = truncated or any(
            hit.chunk_id not in shown for group in groups for hit in group
        )
    else:
        hits, truncated = _search_with_embedding(
            conn, repository_ids, question, k, embedding, scopes, excluded,
        )
    if meter is not None:
        meter.add_search(embed_seconds, time.perf_counter() - database_started)
    return hits, truncated


def _search_with_embedding(
    conn,
    repository_ids: list[int],
    question: str,
    k: int,
    embedding: str,
    scopes: list[str] | None,
    excluded: list[str] | None,
) -> tuple[list[Hit], bool]:
    listing = asks_for_features(question)
    terms = topic_terms(question) if listing else []
    # Rank by exact cosine similarity. The HNSW index walks a cluster of short
    # feature titles and misses closer scenarios, so this scan does not use it.
    sql = f"""
        WITH semantic AS (
            SELECT id, 1 - ord AS similarity
            FROM (
                SELECT id, embedding <=> %(embedding)s::vector AS ord
                FROM chunks
                WHERE repository_id = ANY(%(repos)s)
                  AND (%(features_only)s = FALSE OR right(path, 8) = '.feature')
                  AND {_PATH_SCOPE_SQL}
                  AND {_PATH_EXCLUDE_SQL}
                ORDER BY ord
                LIMIT %(semantic_limit)s
            ) nearest
        ),
        lexical AS (
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
              AND {_PATH_EXCLUDE_SQL}
              AND %(lexical_mode)s <> 'none'
              AND CASE WHEN %(lexical_mode)s = 'or'
                       THEN tsv @@ to_tsquery('simple', %(lexical)s)
                       ELSE numnode(plainto_tsquery('simple', %(lexical)s)) > 0
                            AND tsv @@ plainto_tsquery('simple', %(lexical)s)
                  END
            ORDER BY rank DESC
            LIMIT 50
        )
        SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content,
               COALESCE(s.similarity, 0) + COALESCE(l.rank, 0) AS score
        FROM chunks c
        JOIN repositories r ON r.id = c.repository_id
        LEFT JOIN semantic s ON s.id = c.id
        LEFT JOIN lexical l ON l.id = c.id
        WHERE s.id IS NOT NULL OR l.id IS NOT NULL
        ORDER BY score DESC, c.path, c.start_line
        LIMIT %(k)s
    """
    pool = max(k, 40) if listing else k
    with conn.cursor() as cur:
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute(
            sql,
            {
                "embedding": embedding,
                "repos": repository_ids,
                "lexical": " | ".join(terms) if terms else question,
                "lexical_mode": "or" if terms else "and",
                "features_only": listing,
                "k": pool + 1,
                "semantic_limit": pool + 1,
                "scoped": bool(scopes),
                "scopes": [scope.lower().strip("/") for scope in (scopes or [])] or [""],
                "has_excluded": bool(excluded),
                "excluded": [item.lower().strip("/") for item in (excluded or [])] or [""],
            },
        )
        hits = [_hit_from_row(row) for row in cur.fetchall()]
    truncated = len(hits) > pool
    hits = hits[:pool]
    if listing:
        chosen = _feature_files(hits, terms, k)
        truncated = truncated or len(chosen) > k
        hits = chosen[:k]
        chain: list[Hit] = []
    else:
        chain = _feature_chain(conn, repository_ids, question, hits)
    conn.commit()
    return _drop_excluded(_with_chain(chain, hits, k), excluded or []), truncated


def impact(conn, repository_ids: list[int], target: str) -> dict[str, list[Hit]]:
    looked_up = factory_symbols(target) or [target]
    definitions: list[Hit] = []
    references: list[Hit] = []
    seen: set[int] = set()
    truncated = False
    for item in looked_up:
        rows, extra = _impact_rows(conn, _impact_params(repository_ids, item))
        truncated = truncated or extra
        for row in rows:
            hit = _hit_from_row((*row, 0.0))
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            if _defines_target(hit, item):
                definitions.append(hit)
            else:
                references.append(hit)
    callers, gaps, callers_omitted, callers_capped = _callers_and_gaps(
        conn, repository_ids, definitions, seen,
    )
    merged: list[Hit] = []
    for hit in references:
        walked = callers.pop(hit.chunk_id, None)
        merged.append(walked if walked is not None else hit)
    for chunk_id, hit in callers.items():
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        merged.append(hit)
    references = merged
    references.sort(key=_reference_order)
    shown_references = references[:_IMPACT_LIST_LIMIT]
    related = [
        hit
        for hit in search(conn, repository_ids, target, k=8)[0]
        if hit.chunk_id not in seen
    ]
    logger.info(
        "impact target=%r looked_up=%s definitions=%s references=%s callers=%s "
        "callers_omitted=%s callers_capped=%s gaps=%s related=%s truncated=%s",
        target,
        looked_up,
        len(definitions),
        len(shown_references),
        sum(1 for hit in shown_references if hit.via),
        callers_omitted,
        str(callers_capped).lower(),
        len(gaps),
        len(related),
        str(truncated).lower(),
    )
    return {
        "definitions": definitions[:_IMPACT_LIST_LIMIT],
        "references": shown_references,
        "related": related,
        "gaps": gaps[:12],
        "definition_count": len(definitions),
        "reference_count": len(references) + callers_omitted,
        "truncated": truncated,
        "callers_omitted": callers_omitted,
        "callers_capped": callers_capped,
    }


def _impact_params(repository_ids: list[int], target: str) -> dict:
    return {
        "repos": repository_ids,
        "exact": target.strip().lower(),
        "leaf": last_identifier(target).lower(),
        "path_term": target.strip().replace("\\", "/").lower(),
        "regex": identifier_regex(target),
        "factory": target.strip().lower().startswith("factory :"),
    }


def _defines_target(hit: Hit, target: str) -> bool:
    """A factory symbol matches that factory chunk, not a method of the same leaf name."""
    if target.strip().lower().startswith("factory :"):
        return (hit.symbol or "").lower() == target.strip().lower()
    return (
        symbol_matches(hit.symbol, target)
        or path_matches(hit.path, target)
        or has_declaration(hit.content, target)
    )


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
    if params.get("factory"):
        symbol_match = "lower(coalesce(c.symbol, '')) = %(exact)s"
    else:
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


def _feature_chain(conn, repository_ids: list[int], question: str, _hits: list[Hit]) -> list[Hit]:
    from rag.follow import feature_paths, follow_features

    paths = feature_paths(question)
    if not paths:
        return []
    return follow_features(conn, repository_ids, paths)


def _with_follow(hits: list[Hit], chain: list[Hit], k: int) -> list[Hit]:
    """Keep the matched passages, then the step definition and page objects."""
    if not chain:
        return hits
    merged: list[Hit] = []
    seen: set[int] = set()
    limit = min(16, max(k, len(hits)))
    for hit in hits + chain:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        merged.append(hit)
        if len(merged) >= limit:
            break
    return merged


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
    return chosen


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
    return (_surface_rank(hit.path), 0 if not hit.via else 1, hit.repo, hit.path, hit.start_line)


def _surface_rank(path: str) -> int:
    """Features, page objects, and controllers are the surfaces a change reaches."""
    lowered = path.replace("\\", "/").lower()
    if lowered.endswith(".feature"):
        return 0
    if "page_object" in lowered or lowered.endswith("_page.rb"):
        return 1
    if "/controllers/" in lowered or lowered.endswith("_controller.rb"):
        return 2
    if "step_definitions" in lowered:
        return 3
    if lowered.startswith("app/"):
        return 4
    return 5


def _callers_and_gaps(
    conn,
    repository_ids: list[int],
    definitions: list[Hit],
    seen: set[int],
) -> tuple[dict[int, Hit], list[Hit], int, bool]:
    """Walk stored call edges out from the definitions. Name search still stands.

    The third value is how many callers were left out of the returned list.
    The fourth is true when a hop stopped before every caller was loaded.
    """
    from rag.edges import prepare_call_edges, walk_callers

    if not definitions:
        return {}, [], 0, False
    prepare_call_edges(conn, repository_ids)
    start = {hit.chunk_id for hit in definitions}
    labels = {hit.chunk_id: hit.symbol or hit.path for hit in definitions}
    incoming: dict[int, list[tuple[int, str]]] = {}
    frontier = set(start)
    known = set(start)
    capped = False
    for _depth in range(4):
        if not frontier:
            break
        if len(known) > _KNOWN_CALLER_LIMIT:
            capped = True
            break
        added: set[int] = set()
        rows, hop_capped = _edge_rows(conn, repository_ids, frontier)
        capped = capped or hop_capped
        for src_id, dst_id, detail, symbol, path in rows:
            incoming.setdefault(dst_id, []).append((src_id, detail))
            labels.setdefault(src_id, symbol or path)
            if src_id not in known:
                added.add(src_id)
        known.update(added)
        frontier = added
    reached, omitted = walk_callers(start, incoming, labels, limit=_CALLER_WALK_LIMIT)
    missing = [chunk_id for chunk_id, _via in reached if chunk_id not in seen]
    loaded = {hit.chunk_id: hit for hit in _chunks_by_id(conn, missing)}
    callers = {
        chunk_id: replace(loaded[chunk_id], via=via)
        for chunk_id, via in reached
        if chunk_id in loaded
    }
    gap_ids = set(start) | set(callers)
    gaps = _gap_hits(conn, repository_ids, gap_ids)
    return callers, gaps, omitted, capped


def _edge_rows(conn, repository_ids: list[int], destinations: set[int]) -> tuple[list[tuple], bool]:
    if not destinations:
        return [], False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.src_chunk_id, e.dst_chunk_id, e.detail, c.symbol, c.path
            FROM edges e
            JOIN chunks c ON c.id = e.src_chunk_id
            WHERE e.repository_id = ANY(%s)
              AND e.confidence = 'high'
              AND e.dst_chunk_id = ANY(%s)
            ORDER BY c.path, c.start_line
            LIMIT %s
            """,
            (repository_ids, list(destinations), _EDGE_HOP_LIMIT),
        )
        rows = list(cur.fetchall())
    return rows, len(rows) == _EDGE_HOP_LIMIT


def _chunks_by_id(conn, chunk_ids: list[int]) -> list[Hit]:
    if not chunk_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content, 0
            FROM chunks c
            JOIN repositories r ON r.id = c.repository_id
            WHERE c.id = ANY(%s)
            ORDER BY c.path, c.start_line
            """,
            (chunk_ids,),
        )
        return [_hit_from_row(row) for row in cur.fetchall()]


def _gap_hits(conn, repository_ids: list[int], chunk_ids: set[int]) -> list[Hit]:
    if not chunk_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, e.detail
            FROM edges e
            JOIN chunks c ON c.id = e.src_chunk_id
            JOIN repositories r ON r.id = c.repository_id
            WHERE e.repository_id = ANY(%s)
              AND e.confidence = 'none'
              AND e.src_chunk_id = ANY(%s)
            ORDER BY c.path, c.start_line
            LIMIT 12
            """,
            (repository_ids, list(chunk_ids)),
        )
        rows = cur.fetchall()
    gaps: list[Hit] = []
    seen: set[tuple] = set()
    for row in rows:
        key = (row[0], row[6])
        if key in seen:
            continue
        seen.add(key)
        gaps.append(Hit(
            chunk_id=row[0],
            repo=row[1],
            path=row[2],
            start_line=row[3],
            end_line=row[4],
            symbol=row[5],
            kind="unresolved",
            content=row[6],
            score=0.0,
        ))
    return gaps


def literal_hits(
    conn,
    repository_ids: list[int],
    scopes: list[str],
    literals: list[str],
    k: int,
    excluded: list[str] | None = None,
) -> tuple[list[Hit], float, bool]:
    """Chunks under the named directory whose text contains one of the values.

    The third value is true when more chunks matched than the passage limit.
    """
    started = time.perf_counter()
    pattern = postgres_pattern(literals)
    limited = (_PATH_SCOPE_SQL + " AND " + _PATH_EXCLUDE_SQL).replace("path", "c.path")
    sql = f"""
        SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content, 0
        FROM chunks c
        JOIN repositories r ON r.id = c.repository_id
        WHERE c.repository_id = ANY(%(repos)s)
          AND {limited}
          AND c.content ~* %(pattern)s
        ORDER BY c.path, c.start_line
        LIMIT 200
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                {
                    "repos": repository_ids,
                    "scoped": bool(scopes),
                    "scopes": [scope.lower().strip("/") for scope in scopes] or [""],
                    "has_excluded": bool(excluded),
                    "excluded": [item.lower().strip("/") for item in (excluded or [])] or [""],
                    "pattern": pattern,
                },
            )
            hits = [_hit_from_row(row) for row in cur.fetchall()]
    except psycopg.errors.InvalidRegularExpression:
        conn.rollback()
        raise ValueError("That regular expression is not valid in the index.")
    conn.commit()
    hits.sort(key=lambda hit: (-_literal_count(hit, literals), hit.path, hit.start_line))
    if literals and all(" " in literal for literal in literals):
        hits = _each_path_first(hits)
    return hits[:k], time.perf_counter() - started, len(hits) > k


def _each_path_first(hits: list[Hit]) -> list[Hit]:
    """Show each file before a second chunk from a file already shown."""
    grouped: dict[str, list[Hit]] = {}
    for hit in hits:
        grouped.setdefault(hit.path, []).append(hit)
    first = [group[0] for group in grouped.values()]
    rest = [hit for group in grouped.values() for hit in group[1:]]
    return first + rest


def is_regex_term(term: str) -> bool:
    """A quoted phrase that contains regex syntax, such as [0-9]*."""
    if not _REGEX_METACHAR.search(term):
        return False
    try:
        re.compile(term)
    except re.error:
        return False
    return True


def term_pattern(term: str) -> str:
    """Postgres pattern for one search term.

    A regular expression is used as written. Any other term is matched as a
    whole word or phrase.
    """
    if is_regex_term(term):
        return term
    return identifier_regex(term)


def postgres_pattern(terms: list[str]) -> str:
    """Pattern passed to Postgres ``~*`` for a direct text search."""
    return "(?:" + "|".join(term_pattern(term) for term in terms) + ")"


def _clean_path(raw: str) -> str:
    return raw.replace("\\", "/").strip().strip("/").rstrip(".")


def _add_path(found: list[str], raw: str) -> None:
    cleaned = _clean_path(raw)
    if cleaned and cleaned not in found:
        found.append(cleaned)


def _drop_excluded(hits: list[Hit], excluded: list[str]) -> list[Hit]:
    if not excluded:
        return hits
    return [hit for hit in hits if not path_excluded(hit.path, excluded)]


def _direct_kind(terms: list[str], phrases: list[str]) -> str:
    if phrases and any(is_regex_term(term) for term in terms):
        return "regular expression"
    if phrases:
        return "text"
    return "values"


def _lookup_note(terms: list[str], phrases: list[str], scopes: list[str]) -> str:
    if not phrases:
        return "Looked for those values under the named directory."
    kind = "that regular expression" if _direct_kind(terms, phrases) == "regular expression" else "that text"
    if scopes:
        return f"Looked for {kind} under the named directory."
    return f"Looked for {kind}."


def _literal_count(hit: Hit, literals: list[str]) -> int:
    text = hit.content.lower()
    return sum(literal.lower() in text for literal in literals)


def _inside_scope(start: int, question: str) -> bool:
    return any(match.start(1) <= start < match.end(1) for match in _PATH_SCOPE.finditer(question))


def _inside_quotes(start: int, question: str) -> bool:
    return any(match.start(1) <= start < match.end(1) for match in _QUOTED_PHRASE.finditer(question))


def _inside_excluded_path(start: int, question: str) -> bool:
    for cue in _EXCLUDE_CUE.finditer(question):
        rest = question[cue.end():]
        stop = re.search(r"[.?!]", rest)
        segment_end = cue.end() + (stop.start() if stop else len(rest))
        window = question[cue.end():segment_end]
        for match in _SLASH_PATH.finditer(window):
            if cue.end() + match.start() <= start < cue.end() + match.end():
                return True
    return any(match.start(1) <= start < match.end(1) for match in _NOT_UNDER.finditer(question))


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
_REGEX_METACHAR = re.compile(r"[.\\+*?^${}()\[\]|]")
_PATH_SCOPE_SQL = """(
    %(scoped)s = FALSE
    OR EXISTS (
        SELECT 1
        FROM unnest(%(scopes)s::text[]) AS scope(prefix)
        WHERE lower(path) = prefix
           OR left(lower(path), char_length(prefix) + 1) = prefix || '/'
    )
)"""
_PATH_EXCLUDE_SQL = """(
    %(has_excluded)s = FALSE
    OR NOT EXISTS (
        SELECT 1
        FROM unnest(%(excluded)s::text[]) AS skipped(prefix)
        WHERE lower(path) = prefix
           OR left(lower(path), char_length(prefix) + 1) = prefix || '/'
    )
)"""
_EXCLUDE_CUE = re.compile(
    r"\b(?:ignore|ignoring|exclude|excluding|except|without|skip|skipping)\b",
    re.IGNORECASE,
)
_NOT_UNDER = re.compile(
    r"\bnot\s+(?:under|in|inside|within|from)\s+"
    r"(/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+)",
    re.IGNORECASE,
)
_SLASH_PATH = re.compile(r"/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+")
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
