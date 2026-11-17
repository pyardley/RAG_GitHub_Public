#!/usr/bin/env python3
"""Recall@k and mean reciprocal rank for the text, code, and both indexes.

The questions in eval/questions.jsonl are examples for an ingest of this
repository. Replace them with paths or chunk ids from your own ingest before
you treat the table as a result. The script calls search directly, so it
measures the indexes and the keyword fusion, not the question-planning model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from rag.config import resolve_rrf_k  # noqa: E402
from rag.db import connect, resolve_repositories  # noqa: E402
from rag.search import Hit, search  # noqa: E402

INDEXES = ("text", "code", "both")
FUSIONS = ("rrf", "score")


def load_questions(path: Path) -> list[dict]:
    questions: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            raise SystemExit(f"{path}:{line_number} needs a question")
        paths = [str(value) for value in item.get("paths") or []]
        chunk_ids = [int(value) for value in item.get("chunk_ids") or []]
        if not paths and not chunk_ids:
            raise SystemExit(f"{path}:{line_number} needs paths or chunk_ids")
        questions.append({
            "id": str(item.get("id") or line_number),
            "question": str(item["question"]).strip(),
            "paths": paths,
            "chunk_ids": chunk_ids,
            "example": bool(item.get("example")),
            "category": str(item.get("category") or "").strip(),
        })
    if not questions:
        raise SystemExit(f"{path} has no questions")
    return questions


def recall_at_k(hits: list[Hit], question: dict, k: int) -> float:
    """Share of the expected paths and chunk ids present in the top k."""
    expected = len(question["paths"]) + len(question["chunk_ids"])
    if expected == 0:
        return 0.0
    found_paths = {hit.path for hit in hits[:k]}
    found_ids = {hit.chunk_id for hit in hits[:k]}
    matched = sum(1 for path in question["paths"] if path in found_paths)
    matched += sum(1 for chunk_id in question["chunk_ids"] if chunk_id in found_ids)
    return matched / expected


def reciprocal_rank(hits: list[Hit], question: dict) -> float:
    """1 / rank of the first hit that matches any expected path or chunk id."""
    paths = set(question["paths"])
    chunk_ids = set(question["chunk_ids"])
    for rank, hit in enumerate(hits, start=1):
        if hit.path in paths or hit.chunk_id in chunk_ids:
            return 1.0 / rank
    return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the vector indexes and keyword fusion modes.")
    parser.add_argument(
        "--questions",
        type=Path,
        default=ROOT / "eval" / "questions.jsonl",
        help="JSONL file of questions. Defaults to eval/questions.jsonl",
    )
    parser.add_argument("--k", type=int, default=8, help="Passages retrieved per question")
    parser.add_argument("--repo", help="Limit the search to owner/name")
    parser.add_argument("--rrf-k", type=int, default=None, help="Reciprocal rank fusion constant. Defaults to 60")
    parser.add_argument("--details", action="store_true", help="Print each question's first matching rank")
    args = parser.parse_args(argv)
    if args.k < 1:
        raise SystemExit("k must be at least 1")
    questions = load_questions(args.questions)
    if any(item["example"] for item in questions):
        print("These questions are examples. Replace eval/questions.jsonl before you trust the numbers.")
        print()
    rrf_k = resolve_rrf_k(args.rrf_k)
    conn = connect()
    try:
        repositories = resolve_repositories(conn, args.repo)
        repository_ids = [item["id"] for item in repositories]
        names = ", ".join(item["name"] for item in repositories)
        print(f"Repositories: {names}")
        print(f"Questions: {len(questions)}    k: {args.k}    RRF k: {rrf_k}")
        print()
        rows: list[tuple[str, str, float, float]] = []
        by_category: dict[str, list[tuple[str, str, float, float]]] = {}
        details: list[str] = []
        for index in INDEXES:
            for fusion in FUSIONS:
                recalls: list[float] = []
                reciprocal: list[float] = []
                category_scores: dict[str, tuple[list[float], list[float]]] = {}
                for question in questions:
                    hits, _truncated = search(
                        conn,
                        repository_ids,
                        question["question"],
                        args.k,
                        index=index,
                        fusion=fusion,
                        rrf_k=rrf_k,
                    )
                    recalls.append(recall_at_k(hits, question, args.k))
                    reciprocal.append(reciprocal_rank(hits, question))
                    if question["category"]:
                        scores = category_scores.setdefault(question["category"], ([], []))
                        scores[0].append(recalls[-1])
                        scores[1].append(reciprocal[-1])
                    if args.details:
                        rank = _first_rank(hits, question)
                        top = hits[0].path if hits else "-"
                        details.append(
                            f"{index:6} {fusion:6} {question['id']:20} "
                            f"recall {recalls[-1]:.3f}  first {rank:4}  top {top}"
                        )
                rows.append((index, fusion, _mean(recalls), _mean(reciprocal)))
                for category, (category_recalls, category_reciprocal) in category_scores.items():
                    by_category.setdefault(category, []).append(
                        (index, fusion, _mean(category_recalls), _mean(category_reciprocal))
                    )
    finally:
        conn.close()
    _print_table(rows, args.k)
    if len(by_category) > 1:
        for category, category_rows in by_category.items():
            count = sum(1 for question in questions if question["category"] == category)
            print()
            print(f"{category} ({count} questions)")
            _print_table(category_rows, args.k)
    if details:
        print()
        print("\n".join(details))
    return 0


def _print_table(rows: list[tuple[str, str, float, float]], k: int) -> None:
    header = f"{'index':<8} {'fusion':<8} {'recall@' + str(k):<12} {'MRR':<8}"
    print(header)
    print("-" * len(header))
    for index, fusion, recall, mrr in rows:
        print(f"{index:<8} {fusion:<8} {recall:<12.3f} {mrr:<8.3f}")


def _first_rank(hits: list[Hit], question: dict) -> str:
    score = reciprocal_rank(hits, question)
    if score == 0:
        return "-"
    return str(round(1 / score))


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


if __name__ == "__main__":
    raise SystemExit(main())
