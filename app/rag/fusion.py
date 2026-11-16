"""Combine ranked lists from the vector indexes and the keyword search.

Reciprocal rank fusion follows Cormack, Clarke and Buettcher (SIGIR 2009):
each list contributes ``1 / (k + rank)`` and the contributions are added.
Rank starts at 1. ``k`` is 60 unless it is configured otherwise. An id that
is missing from a list adds nothing from that list.
"""

from __future__ import annotations


def reciprocal_rank_fusion(
    rankings: list[list[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Merge rankings of chunk ids. The best score is first.

    A repeated id in one list keeps its first rank. Later copies are ignored,
    and they do not push the next distinct id down a rank.
    """
    if k < 0:
        raise ValueError("RRF k must be zero or greater")
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        rank = 0
        for chunk_id in ranking:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank += 1
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def combine_rankings(
    *,
    index: str,
    fusion: str,
    text: list[tuple[int, float]] | None,
    code: list[tuple[int, float]] | None,
    lexical: list[tuple[int, float]],
    rrf_k: int,
) -> list[tuple[int, float]]:
    """Return ``(chunk id, score)`` pairs, best first.

    ``text`` and ``code`` are cosine similarities in rank order. ``lexical``
    is ``ts_rank_cd`` in rank order.

    ``index=both`` merges the two vector rankings with reciprocal rank fusion.
    ``fusion=score`` then adds the keyword score (``ts_rank_cd``) to the
    vector score. A side that did not retrieve the chunk contributes 0, which
    is the previous single-index sum of cosine similarity and ``ts_rank_cd``.
    ``fusion=rrf`` folds the keyword ranking into the same fusion. With
    ``both``, that is one fusion over the text list, the code list, and the
    keyword list, so the keyword list is not given the weight of both models
    combined.
    """
    if index == "text":
        vectors = [text or []]
    elif index == "code":
        vectors = [code or []]
    elif index == "both":
        vectors = [text or [], code or []]
    else:
        raise ValueError("Index must be text, code, or both")
    if fusion not in {"rrf", "score"}:
        raise ValueError("Fusion must be rrf or score")
    if fusion == "score" and index != "both":
        return _add_scores(vectors[0], lexical)
    if fusion == "score":
        fused = reciprocal_rank_fusion([_ids(items) for items in vectors], k=rrf_k)
        return _add_scores(fused, lexical)
    rankings = [_ids(items) for items in vectors]
    rankings.append(_ids(lexical))
    return reciprocal_rank_fusion(rankings, k=rrf_k)


def _ids(pairs: list[tuple[int, float]]) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for chunk_id, _score in pairs:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        found.append(chunk_id)
    return found


def _add_scores(
    vector: list[tuple[int, float]],
    lexical: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Add the two raw scores. A chunk missing from one list scores 0 there."""
    scores: dict[int, float] = {}
    for chunk_id, score in vector:
        if chunk_id not in scores:
            scores[chunk_id] = score
    for chunk_id, score in lexical:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + score
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
