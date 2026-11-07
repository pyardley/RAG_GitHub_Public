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

from rag.answer import (
    _query_instructions,
    _with_matches,
    matching_lines,
    ollama_payload,
    passage_limits,
    visible_answer,
)
from rag.follow import calls_in, choose_steps, contains_step, feature_paths, page_symbol, step_body, step_phrases
from rag.gitrepo import default_branch_name
from rag.web import Handler, _progress_view, github_blob_url, run_query, start_ingest
from unittest.mock import patch

from rag.plan import parse_queries, question_needs_plan
from rag.usage import Meter, token_counts
from rag.search import (
    Hit,
    _feature_files,
    asks_for_features,
    _each_path_first,
    code_literals,
    merge_hits,
    term_pattern,
    path_in_scope,
    path_scopes,
    quoted_phrases,
    retrieve,
    topic_terms,
)
from rag.chunker import chunk_source
from rag.cli import main
from rag.files import is_skipped, iter_source_files, language_for, parse_repo
from rag.text import (
    has_declaration,
    identifier_regex,
    path_matches,
    search_text,
    symbol_matches,
)


def _hit(path: str, symbol: str, content: str, score: float) -> Hit:
    return Hit(1, "hmcts/hwf-publicapp", path, 1, 2, symbol, "scenario", content, score)


class ChunkerTest(unittest.TestCase):
    def test_feature_chain_names_the_page_object_call(self):
        question = (
            "Explain how features like /features/address/address_married.feature "
            "are turned into button presses"
        )
        self.assertEqual(feature_paths(question), ["features/address/address_married.feature"])
        feature = """Feature: Address page
  Scenario: Entering address with postcode
    When I enter my address with postcode
    Then I should be taken to contact page
"""
        self.assertEqual(
            choose_steps(step_phrases(feature)),
            ["I enter my address with postcode"],
        )
        self.assertTrue(contains_step(feature, "I enter my address with postcode"))
        self.assertFalse(contains_step(feature, "I enter my address"))
        step = "When(/^I enter my address with postcode$/) do\n  address_page.submit_full_address\nend\n"
        self.assertEqual(calls_in(step), [("address_page", "submit_full_address")])
        body = step_body(
            "When(/^I click something$/) { other_page.go }\n" + step + "When(/^I enter my postcode$/) { address_page.post_code }",
            "I enter my address with postcode",
        )
        self.assertEqual(calls_in(body), [("address_page", "submit_full_address")])
        self.assertEqual(page_symbol("address_page", "submit_full_address"), "AddressPage#submit_full_address")
        self.assertTrue(asks_for_features("Show features that test savings thresholds"))
        self.assertEqual(topic_terms("Show features that test savings thresholds"), ["savings", "thresholds"])
        self.assertFalse(
            asks_for_features(
                "Explain how features like /features/address/address_married.feature "
                "are turned into button presses"
            )
        )
        help_page = _hit("features/savings_investment/savings_investment_single.feature", "Help with savings", "Then I should see the help text", 0.9)
        band = _hit(
            "features/savings_investment/savings_investment_single.feature",
            "Savings and investment page refund / Selecting £4,250 to £15,999",
            "When I submit the form with between £4,250 and £15,999 checked",
            0.4,
        )
        timeout = _hit(
            "features/savings_investment/savings_investment_single.feature",
            "Savings and investments page timeout (£16,000 or more option)",
            "When I slowly submit the form with £16,000 or more checked",
            0.95,
        )
        onward = _hit("features/marital_status.feature", "Selecting single", "Then I should be taken to savings and investments page", 0.8)
        listed = _feature_files([help_page, band, timeout, onward], ["savings", "thresholds"], 8)
        self.assertEqual(
            [(hit.path, hit.symbol) for hit in listed],
            [("features/savings_investment/savings_investment_single.feature", band.symbol)],
        )
        page = "def submit_full_address\n  address_page.street('102')\n  continue\nend\n"
        self.assertEqual(
            calls_in(page),
            [("address_page", "street"), (None, "continue")],
        )

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

    def test_default_branch_name_reads_the_symbolic_ref(self):
        output = "ref: refs/heads/master\tHEAD\nd6867d299ece441f1f26c14ab1ddd8cd1e571eb0\tHEAD\n"
        self.assertEqual(default_branch_name(output), "master")
        self.assertIsNone(default_branch_name("d6867d299ece441f1f26c14ab1ddd8cd1e571eb0\tHEAD\n"))

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


class PlanTest(unittest.TestCase):
    def test_parse_queries_keeps_words_from_the_question_and_drops_invented_names(self):
        question = "Where does an applicant have too much in savings to get help with fees?"
        text = '{"queries": ["savings fees", "checkSavingsLimit", "applicant savings", "help with fees"]}'
        self.assertEqual(
            parse_queries(text, question),
            [question, "savings fees", "applicant savings"],
        )

    def test_parse_queries_falls_back_when_the_model_does_not_return_json(self):
        self.assertEqual(parse_queries("I am not sure", "where is the fee"), ["where is the fee"])

    def test_a_named_feature_is_not_rewritten(self):
        question = (
            "Explain how features like /features/address/address_married.feature "
            "are turned into button presses"
        )
        with patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"):
            self.assertFalse(question_needs_plan(question))
            self.assertFalse(question_needs_plan("Show features that test savings thresholds"))
            self.assertTrue(question_needs_plan("Where are savings thresholds decided?"))

    def test_a_directory_lookup_keeps_the_named_values(self):
        question = "look under /app for value 4250 or min_threshold_base or max_threshold_base"
        self.assertEqual(path_scopes(question), ["app"])
        self.assertEqual(
            code_literals(question),
            ["4250", "min_threshold_base", "max_threshold_base"],
        )
        self.assertTrue(path_in_scope("app/models/forms/savings_and_investment_extra.rb", ["app"]))
        self.assertFalse(path_in_scope("spec/models/savings_spec.rb", ["app"]))
        self.assertFalse(path_in_scope("config/settings.yml", ["app"]))
        self.assertEqual(path_scopes("Where are savings thresholds decided?"), [])
        self.assertEqual(code_literals("Where are savings thresholds decided?"), [])
        with patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"):
            self.assertFalse(question_needs_plan(question))

    def test_the_word_in_front_of_the_number_does_not_matter(self):
        question = "look under /app for text 4250"
        self.assertEqual(path_scopes(question), ["app"])
        self.assertEqual(code_literals(question), ["4250"])
        self.assertEqual(path_scopes("inside app/models for 4250"), ["app/models"])
        self.assertEqual(code_literals("inside app/models for 4250"), ["4250"])
        self.assertEqual(path_scopes("under app for 4250"), [])
        with patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"):
            self.assertFalse(question_needs_plan(question))
            self.assertFalse(question_needs_plan("inside app/models for 4250"))

    def test_a_literal_inside_the_directory_is_not_a_search_term(self):
        question = "under app/models/forms/savings_and_investment_extra.rb for 4250"
        self.assertEqual(
            path_scopes(question),
            ["app/models/forms/savings_and_investment_extra.rb"],
        )
        self.assertEqual(code_literals(question), ["4250"])
        self.assertTrue(path_in_scope(
            "app/models/forms/savings_and_investment_extra.rb",
            ["app/models"],
        ))
        self.assertFalse(path_in_scope("app/models_extra/fee.rb", ["app/models"]))

    def test_retrieve_stops_at_the_literal_search(self):
        question = "look under /app for text 4250"
        hit = Hit(
            9,
            "hmcts/hwf-publicapp",
            "app/models/forms/savings_and_investment_extra.rb",
            23,
            27,
            "Forms::SavingsAndInvestmentExtra#amount",
            "method",
            "return if amount < 4250",
            1.0,
        )
        meter = Meter()

        def fail_if_planned(*_args, **_kwargs):
            raise AssertionError("planner should not run")

        with (
            patch("rag.search.literal_hits", return_value=([hit], 0.01)),
            patch("rag.plan.search_queries", side_effect=fail_if_planned),
        ):
            hits, queries = retrieve(None, [1], question, 16, meter)
        self.assertEqual(hits, [hit])
        self.assertEqual(queries, [question])
        embed = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Embed the searches")
        self.assertEqual(embed["note"], "The values were matched directly, so nothing was embedded.")
        self.assertIsNone(embed["total_tokens"])

    def test_a_quoted_step_is_searched_under_the_directory(self):
        question = 'look for "I submit the form with income" under /features'
        phrase = "I submit the form with income"
        self.assertEqual(path_scopes(question), ["features"])
        self.assertEqual(quoted_phrases(question), [phrase])
        self.assertEqual(code_literals(question), [])
        pattern = "(?i)" + identifier_regex(phrase).replace(r"\m", r"\b").replace(r"\M", r"\b")
        step = "When(/^" + phrase + " '([^\"']*)' and monthly$/) do |num|"
        feature = "When " + phrase + " '1000' and monthly"
        unrelated = "When I submit the form with wages and working tax credit checked"
        self.assertRegex(step, pattern)
        self.assertRegex(feature, pattern)
        self.assertNotRegex(unrelated, pattern)
        feature_hit = Hit(
            3,
            "hmcts/hwf-publicapp",
            "features/income_kind/income_kind_single.feature",
            27,
            31,
            "Scenario",
            "scenario",
            feature,
            1.0,
        )
        step_hit = Hit(
            4,
            "hmcts/hwf-publicapp",
            "features/step_definitions/income_period_steps.rb",
            5,
            7,
            None,
            "file",
            step,
            1.0,
        )
        seen = {}

        def fake_literal(_conn, _repos, scopes, literals, _k):
            seen["scopes"] = scopes
            seen["literals"] = literals
            return [feature_hit, step_hit], 0.01

        def fail_if_planned(*_args, **_kwargs):
            raise AssertionError("planner should not run")

        meter = Meter()
        with (
            patch("rag.search.literal_hits", side_effect=fake_literal),
            patch("rag.plan.search_queries", side_effect=fail_if_planned),
            patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"),
        ):
            self.assertFalse(question_needs_plan(question))
            hits, queries = retrieve(None, [1], question, 8, meter)
        self.assertEqual(seen["scopes"], ["features"])
        self.assertEqual(seen["literals"], [phrase])
        self.assertEqual(hits, [feature_hit, step_hit])
        self.assertEqual(queries, [question])
        plan = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Plan the question")
        self.assertEqual(plan["note"], "Looked for that text under the named directory.")

    def test_a_long_feature_does_not_crowd_out_the_step_file(self):
        feature = "features/e2e.feature"
        step = "features/step_definitions/income_period_steps.rb"
        hits = [
            Hit(index, "hmcts/hwf-publicapp", feature, index, index, None, "scenario", "when", 1.0)
            for index in range(1, 9)
        ]
        hits.append(Hit(9, "hmcts/hwf-publicapp", step, 5, 7, None, "file", "when", 1.0))
        spread = _each_path_first(hits)
        self.assertEqual([hit.path for hit in spread[:2]], [feature, step])

    def test_usage_sums_language_model_tokens_and_keeps_retrieval_untokenised(self):
        self.assertEqual(token_counts({"usage": {"prompt_tokens": 20, "completion_tokens": 5}}), (20, 5))
        self.assertEqual(token_counts({"prompt_eval_count": 11, "eval_count": 3}), (11, 3))
        meter = Meter()
        meter.add_search(0.2, 0.05)
        meter.add_search(0.1, 0.02)
        meter.finish_retrieval(8)
        usage = meter.as_dict()
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(usage["stages"][0]["name"], "Embed the searches")
        self.assertEqual(usage["stages"][0]["total_tokens"], None)
        self.assertAlmostEqual(usage["stages"][0]["seconds"], 0.3, places=3)
        self.assertEqual(usage["stages"][1]["note"], "2 searches, 8 passages kept.")

    def test_merge_hits_keeps_the_higher_score_for_a_repeated_chunk(self):
        low = Hit(1, "hmcts/hwf-publicapp", "app/a.rb", 1, 2, "A", "method", "a", 0.1)
        high = Hit(1, "hmcts/hwf-publicapp", "app/a.rb", 1, 2, "A", "method", "a", 0.8)
        other = Hit(2, "hmcts/hwf-publicapp", "app/b.rb", 3, 4, "B", "method", "b", 0.4)
        merged = merge_hits([[low, other], [high]], 2)
        self.assertEqual([hit.chunk_id for hit in merged], [1, 2])
        self.assertEqual(merged[0].score, 0.8)


class AnswerTest(unittest.TestCase):
    def test_a_quoted_step_pattern_counts_as_a_match(self):
        question = 'look for "I submit the form with income" under /features'
        hits = [
            Hit(
                3,
                "hmcts/hwf-publicapp",
                "features/income_kind/income_kind_single.feature",
                27,
                31,
                "Scenario",
                "scenario",
                "When I submit the form with income '1000' and monthly",
                1.0,
            ),
            Hit(
                4,
                "hmcts/hwf-publicapp",
                "features/step_definitions/income_period_steps.rb",
                1,
                11,
                None,
                "file",
                "When(/^I submit the form with income '([^\"']*)' and monthly$/) do |num|",
                1.0,
            ),
        ]
        instructions = _query_instructions(question, hits)
        self.assertIn("Cucumber step pattern", instructions)
        self.assertIn("no exact match", instructions)
        self.assertNotIn("If a stage is not in the passages, say so", instructions)

    def test_the_written_answer_keeps_the_step_definition_line(self):
        question = 'look for "and average" under /features'
        feature = "\n".join([
            "When I submit the form with income '8000' and average 'average'",
            "| Total income £8,000 Average for the last three months Change income |",
        ])
        step = "\n".join([
            "When(/^I submit the form with income '([^\"']*)' and monthly$/) do |num|",
            "When(/^I submit the form with income '([^\"']*)' and average '([^\"']*)'$/) do |num, income_period|",
        ])
        hits = [
            Hit(1, "hmcts/hwf-publicapp", "features/e2e.feature", 160, 192, None, "scenario", feature, 1.0),
            Hit(2, "hmcts/hwf-publicapp", "features/step_definitions/income_period_steps.rb", 5, 11, None, "file", step, 1.0),
        ]
        lines = matching_lines(question, hits)
        self.assertEqual(
            [(path, line) for path, line, _text in lines],
            [
                ("features/e2e.feature", 160),
                ("features/step_definitions/income_period_steps.rb", 6),
            ],
        )
        written = _with_matches(question, hits, "The feature summary mentions Average at line 192.")
        self.assertIn("features/step_definitions/income_period_steps.rb:6:", written)
        self.assertTrue(written.startswith("Matches\n"))

    def test_a_quoted_regex_matches_any_amount(self):
        question = '''look for "with income '[0-9]*' and average" under /features'''
        phrase = "with income '[0-9]*' and average"
        self.assertEqual(quoted_phrases(question), [phrase])
        self.assertIn("[0-9]*", term_pattern(phrase))
        self.assertNotIn(r"\[0-9\]", term_pattern(phrase))
        feature = "When I submit the form with income '8000' and average 'average'"
        step = "When(/^I submit the form with income '([^\"']*)' and average '([^\"']*)'$/) do |num, income_period|"
        summary = "| Total income £8,000 Average for the last three months Change income |"
        hits = [
            Hit(1, "hmcts/hwf-publicapp", "features/e2e.feature", 218, 244, None, "scenario", "\n".join([feature, summary]), 1.0),
            Hit(2, "hmcts/hwf-publicapp", "features/step_definitions/income_period_steps.rb", 9, 11, None, "file", step, 1.0),
        ]
        self.assertEqual(
            [(path, line) for path, line, _text in matching_lines(question, hits)],
            [("features/e2e.feature", 218)],
        )

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
            self.assertIn("Resources", page)
            self.assertIn("renderUsage", page)
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

    def test_query_response_includes_usage_stages(self):
        hit = Hit(
            9,
            "hmcts/hwf-publicapp",
            "app/models/forms/savings_and_investment_extra.rb",
            23,
            27,
            "Forms::SavingsAndInvestmentExtra#amount",
            "method",
            "return if amount < 4250",
            1.0,
        )
        repo = {
            "id": 1,
            "name": "hmcts/hwf-publicapp",
            "branch": "master",
            "commit_sha": "b1d15f83c0a9",
        }

        def fake_retrieve(_conn, _repository_ids, question, _k, meter=None):
            if meter is not None:
                meter.add_search(0.0, 0.01)
                meter.finish_retrieval(1, embedded=False)
            return [hit], [question]

        class _Connection:
            def close(self):
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/query",
                data=json.dumps({
                    "question": "look under /app for text 4250",
                    "repo": "hmcts/hwf-publicapp",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with (
                patch("rag.web.connect", return_value=_Connection()),
                patch("rag.web.resolve_repositories", return_value=[repo]),
                patch("rag.web.retrieve", side_effect=fake_retrieve),
                patch("rag.web.language_model_configured", return_value=True),
                patch("rag.web.answer", return_value="The threshold is 4250."),
            ):
                with urllib.request.urlopen(request) as response:
                    body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(body["answer"], "The threshold is 4250.")
            self.assertTrue(body["usage"]["stages"])
            self.assertIn(
                "The values were matched directly, so nothing was embedded.",
                [stage["note"] for stage in body["usage"]["stages"]],
            )
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
