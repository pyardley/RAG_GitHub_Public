import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from rag.answer import ollama_payload, passage_limits, visible_answer
from rag.chunker import chunk_source
from rag.web import Handler, _progress_view, github_blob_url, run_query, start_ingest
from rag.search import Hit
from rag.cli import main
from rag.files import is_skipped, iter_source_files, language_for, parse_repo
from rag.text import (
    has_declaration,
    identifier_regex,
    path_matches,
    search_text,
    symbol_matches,
)


class ChunkerTest(unittest.TestCase):
    def test_ruby_methods_are_separate_symbols(self):
        source = """# frozen_string_literal: true

class Foo < ApplicationRecord
  validates :name, presence: true

  def self.build
    new
  end

  def save!
    super
  end
end
"""
        chunks = chunk_source("ruby", source)
        by_symbol = {chunk.symbol: chunk for chunk in chunks if chunk.symbol}
        self.assertEqual(set(by_symbol), {"Foo", "Foo.build", "Foo#save!"})
        self.assertIn("validates", by_symbol["Foo"].content)
        self.assertNotIn("def self.build", by_symbol["Foo"].content)
        self.assertIn("def self.build", by_symbol["Foo.build"].content)
        self.assertNotIn("def save!", by_symbol["Foo.build"].content)
        self.assertEqual(by_symbol["Foo.build"].start_line, 6)
        self.assertEqual(by_symbol["Foo.build"].end_line, 8)
        self.assertTrue(any("frozen_string_literal" in chunk.content for chunk in chunks))

    def test_nested_ruby_constant(self):
        source = """module A
  class B
    def c
    end
  end
end
"""
        symbols = {chunk.symbol for chunk in chunk_source("ruby", source)}
        self.assertEqual(symbols, {"A", "A::B", "A::B#c"})

    def test_rspec_example_groups(self):
        source = """RSpec.describe "Foo" do
  describe "#bar" do
  end
end
"""
        symbols = {chunk.symbol for chunk in chunk_source("ruby", source)}
        self.assertEqual(symbols, {"Foo", "Foo / #bar"})

    def test_java_methods(self):
        source = """public class FeeService {
    public FeeService(Clock clock) {
        this.clock = clock;
    }

    public int calculate(int fee) {
        return fee;
    }
}
"""
        by_symbol = {chunk.symbol: chunk for chunk in chunk_source("java", source)}
        self.assertEqual(set(by_symbol), {"FeeService", "FeeService.FeeService", "FeeService.calculate"})
        self.assertIn("return fee", by_symbol["FeeService.calculate"].content)
        self.assertNotIn("this.clock", by_symbol["FeeService.calculate"].content)

    def test_javascript_methods(self):
        source = """class Fee {
  calculate(x) {
    return x;
  }
}
function format(value) {
  return value;
}
"""
        symbols = {chunk.symbol for chunk in chunk_source("javascript", source)}
        self.assertEqual(symbols, {"Fee", "Fee.calculate", "format"})

    def test_gherkin_scenarios_keep_the_feature_name(self):
        source = """Feature: Apply for help
  Scenario: Pays the fee
    Given a page
"""
        symbols = {chunk.symbol for chunk in chunk_source("gherkin", source)}
        self.assertEqual(symbols, {"Apply for help", "Apply for help / Pays the fee"})

    def test_long_files_overlap(self):
        text = "\n".join(f"line {number}" for number in range(1, 251))
        chunks = chunk_source("yaml", text, max_lines=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0].start_line, 1)
        self.assertEqual(chunks[-1].end_line, 250)
        self.assertLess(chunks[1].start_line, chunks[0].end_line)

    def test_blank_file(self):
        self.assertEqual(chunk_source("ruby", "\n\n"), [])


class TextTest(unittest.TestCase):
    def test_search_text_splits_identifiers(self):
        text = search_text("app/services/FeeCalculator.rb", "fee_calculator")
        self.assertIn("Fee Calculator", text)
        self.assertIn("fee calculator", text)

    def test_symbol_and_path_matching(self):
        self.assertTrue(symbol_matches("Foo#save", "save"))
        self.assertTrue(symbol_matches("Foo.build", "Foo#build"))
        self.assertFalse(symbol_matches("Foo#save", "Bar#save"))
        self.assertFalse(symbol_matches("ApplicationController", "Application"))
        self.assertTrue(path_matches("app/models/fee_calculator.rb", "fee_calculator"))
        self.assertTrue(path_matches("app/models/fee_calculator.rb", "app/models/fee_calculator.rb"))
        self.assertFalse(path_matches("app/models/fee_calculator.rb", "fee"))

    def test_identifier_regex_keeps_ruby_punctuation(self):
        self.assertEqual(identifier_regex("Foo#bar"), r"\mFoo[\s#.:]+bar\M")
        self.assertEqual(identifier_regex("save!"), r"\msave!(?!\w)")

    def test_declarations_are_not_calls(self):
        self.assertFalse(has_declaration("result = calculate(1)", "calculate"))
        self.assertTrue(has_declaration("  def calculate(x)\n  end", "calculate"))
        self.assertTrue(has_declaration("    public int calculate(int fee) {", "calculate"))


class FilesTest(unittest.TestCase):
    def test_parse_repo(self):
        self.assertEqual(
            parse_repo("hmcts/hwf-publicapp"),
            ("hmcts/hwf-publicapp", "https://github.com/hmcts/hwf-publicapp.git"),
        )
        self.assertEqual(
            parse_repo("https://github.com/hmcts/hwf-publicapp"),
            ("hmcts/hwf-publicapp", "https://github.com/hmcts/hwf-publicapp"),
        )
        self.assertEqual(
            parse_repo("git@github.com:hmcts/hwf-publicapp.git")[0],
            "hmcts/hwf-publicapp",
        )
        with self.assertRaises(ValueError):
            parse_repo("../secrets")
        with self.assertRaises(ValueError):
            parse_repo("https://github.com/hmcts/hwf-publicapp/tree/master")

    def test_skip_rules(self):
        self.assertTrue(is_skipped(Path("vendor/bundle/foo.rb")))
        self.assertTrue(is_skipped(Path(".env")))
        self.assertTrue(is_skipped(Path(".env.test")))
        self.assertTrue(is_skipped(Path("Gemfile.lock")))
        self.assertTrue(is_skipped(Path("public/assets/application.js")))
        self.assertFalse(is_skipped(Path("app/models/fee.rb")))
        self.assertFalse(is_skipped(Path(".env.example")))
        self.assertEqual(language_for(Path("app/models/fee.rb")), "ruby")
        self.assertEqual(language_for(Path("Dockerfile")), "dockerfile")
        self.assertEqual(language_for(Path(".env.development.example")), "config")

    def test_walk_skips_vendor_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "app" / "fee.rb").write_text("class Fee\nend\n", encoding="utf-8")
            (root / "vendor").mkdir()
            (root / "vendor" / "hidden.rb").write_text("class Hidden\nend\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
            found = [relative for _, relative, _ in iter_source_files(root)]
        self.assertIn("app/fee.rb", found)
        self.assertIn("Gemfile", found)
        self.assertNotIn("vendor/hidden.rb", found)
        self.assertFalse(any(path.startswith(".env") for path in found))


class AnswerTest(unittest.TestCase):
    def test_visible_answer_drops_a_think_block(self):
        text = "<think>walk the callers</think>\nDefined in application_controller.rb:44."
        self.assertEqual(visible_answer(text), "Defined in application_controller.rb:44.")

    def test_passage_budget_shrinks_with_the_context_window(self):
        hits, per_passage, total = passage_limits(8192)
        self.assertEqual((hits, per_passage), (8, 2500))
        self.assertGreater(total, per_passage)

    def test_ollama_payload_sets_context_and_keeps_the_passages_inside_it(self):
        hits = [
            Hit(i, "hmcts/hwf-publicapp", f"app/file_{i}.rb", 1, 4, "Foo#bar", "method", "x" * 4000, 0.0)
            for i in range(12)
        ]
        payload = ollama_payload("qwen2.5-coder:7b", "where is it saved", hits, impact=False, num_ctx=8192)
        self.assertEqual(payload["model"], "qwen2.5-coder:7b")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        passages = payload["messages"][1]["content"]
        self.assertLessEqual(passages.count("\n\n["), 8)
        self.assertIn("app/file_0.rb:1-4", passages)
        self.assertNotIn("app/file_11.rb", passages)


class WebTest(unittest.TestCase):
    def test_github_link_points_at_the_indexed_lines(self):
        url = github_blob_url(
            "hmcts/hwf-publicapp",
            "b1d15f83c0a9",
            "app/controllers/application_controller.rb",
            44,
            46,
        )
        self.assertEqual(
            url,
            "https://github.com/hmcts/hwf-publicapp/blob/"
            "b1d15f83c0a9/app/controllers/application_controller.rb#L44-L46",
        )
        self.assertIsNone(github_blob_url("hmcts/hwf-publicapp", "pending", "app/fee.rb", 1, 3))
        self.assertIsNone(github_blob_url("hmcts/hwf-publicapp", "b1d15f83c0a9", "../fee.rb", 1, 1))

    def test_progress_percent_is_unknown_while_cloning(self):
        cloning = _progress_view({"phase": "clone", "done": 0, "total": 0, "chunks": 0, "label": "Cloning"})
        self.assertIsNone(cloning["percent"])
        indexing = _progress_view({"phase": "index", "done": 25, "total": 100, "chunks": 80, "label": "25 of 100"})
        self.assertEqual(indexing["percent"], 25)

    def test_empty_question_is_rejected_before_the_database(self):
        with self.assertRaises(ValueError):
            run_query({"question": "  "})

    def test_ingest_rejects_a_repository_path_before_starting(self):
        with self.assertRaises(ValueError):
            start_ingest({"repo": "../secrets"})

    def test_home_page_lists_the_three_tasks(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Impact", page)
            self.assertIn("Search", page)
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/query",
                data=json.dumps({"question": ""}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()


class CliTest(unittest.TestCase):
    def test_help_does_not_need_a_database(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
