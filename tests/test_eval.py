"""Recall and reciprocal rank for the example question file. No database."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "eval"))

import run_eval  # noqa: E402

from rag.search import Hit  # noqa: E402


def _hit(path: str, chunk_id: int) -> Hit:
    return Hit(chunk_id, "example/repo", path, 1, 4, None, "method", "body", 0.5)


class EvalMetricsTest(unittest.TestCase):
    def test_recall_and_reciprocal_rank(self):
        question = {"paths": ["app/rag/search.py", "app/rag/db.py"], "chunk_ids": [9]}
        hits = [
            _hit("app/rag/embedder.py", 1),
            _hit("app/rag/search.py", 2),
            _hit("app/rag/db.py", 9),
        ]
        self.assertEqual(run_eval.recall_at_k(hits, question, 1), 0.0)
        self.assertAlmostEqual(run_eval.recall_at_k(hits, question, 3), 1.0)
        self.assertAlmostEqual(run_eval.reciprocal_rank(hits, question), 0.5)
        self.assertEqual(run_eval.reciprocal_rank([_hit("README.md", 4)], question), 0.0)

    def test_the_shipped_questions_are_examples(self):
        questions = run_eval.load_questions(ROOT / "eval" / "questions.jsonl")
        self.assertGreaterEqual(len(questions), 4)
        for question in questions:
            self.assertTrue(question["example"])
            self.assertTrue(question["paths"])
            self.assertTrue(question["question"])

    def test_each_repository_file_covers_every_category(self):
        for name in ("hwf-publicapp", "hwf-staffapp", "et-full-system", "c100-application"):
            with self.subTest(name=name):
                questions = run_eval.load_questions(ROOT / "eval" / f"{name}.jsonl")
                self.assertGreaterEqual(len(questions), 20)
                categories = {question["category"] for question in questions}
                self.assertEqual(categories, {"concept", "identifier", "impact", "step"})
                ids = [question["id"] for question in questions]
                self.assertEqual(len(ids), len(set(ids)))
                for question in questions:
                    self.assertFalse(question["example"])
                    self.assertTrue(all("\\" not in path for path in question["paths"]))


if __name__ == "__main__":
    unittest.main()
