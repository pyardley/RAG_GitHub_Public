#!/usr/bin/env python3
"""Recall of expected paths for impact questions via the call graph.

Reads the same JSONL files as run_eval.py. Each impact question needs a
``target`` field naming the symbol Impact should look up (for example
``C100App::PaymentsFlowControl`` or ``ResolverService#return``). Questions
without a target, or with another category, are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from rag.db import connect, resolve_repositories  # noqa: E402
from rag.edges import prepare_call_edges  # noqa: E402
from rag.search import impact  # noqa: E402


def load_impact_questions(path: Path) -> list[dict]:
    questions: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if str(item.get("category") or "").strip() != "impact":
            continue
        target = str(item.get("target") or "").strip()
        paths = [str(value) for value in item.get("paths") or []]
        if not target:
            raise SystemExit(f"{path}:{line_number} impact question needs a target")
        if not paths:
            raise SystemExit(f"{path}:{line_number} impact question needs paths")
        questions.append({
            "id": str(item.get("id") or line_number),
            "target": target,
            "paths": paths,
            "question": str(item.get("question") or "").strip(),
        })
    return questions


def recall(paths_found: set[str], expected: list[str]) -> float:
    if not expected:
        return 0.0
    return sum(1 for path in expected if path in paths_found) / len(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Impact caller recall on eval questions.")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--repo", help="Limit to owner/name")
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args(argv)

    questions = load_impact_questions(args.questions)
    if not questions:
        raise SystemExit(f"{args.questions} has no impact questions with a target")

    with connect() as conn:
        repositories = resolve_repositories(conn, args.repo)
        repository_ids = [item["id"] for item in repositories]
        prepare_call_edges(conn, repository_ids)
        scores: list[float] = []
        for item in questions:
            result = impact(conn, repository_ids, item["target"])
            # Callers and text mentions are merged into references.
            found_paths = {hit.path for hit in result["references"]}
            score = recall(found_paths, item["paths"])
            scores.append(score)
            if args.details:
                missing = [path for path in item["paths"] if path not in found_paths]
                print(f"{item['id']}\t{score:.2f}\t{item['target']}"
                      + (f"\tmissing={missing}" if missing else ""))

    mean = sum(scores) / len(scores)
    print(f"questions={len(scores)} mean_recall={mean:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
