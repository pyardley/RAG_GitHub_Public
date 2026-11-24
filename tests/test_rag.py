import json
import os
import subprocess
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
    AnswerError,
    _query_instructions,
    _with_matches,
    active_model,
    anthropic_payload,
    anthropic_root,
    anthropic_text,
    language_model_configured,
    matching_lines,
    ollama_payload,
    passage_limits,
    visible_answer,
)
from rag.follow import (
    calls_in,
    choose_steps,
    contains_step,
    definition_matches,
    feature_paths,
    page_symbol,
    step_body,
    step_needles,
    step_phrases,
)
from rag.gitrepo import branch_list, default_branch_name
from rag.journey import branch_answers, diff_chart, journey_scopes, mark_visits
from rag.flows import charts_for, parse_navigation, question_texts
from rag.features import behaviour_text, gherkin_lines, is_drafting_request, matched_step_texts, retrieve_features
from rag.chunker import find_blocks
from rag.history import HistoryError, file_history
from rag.testing import command_phrases, resolve_feature_paths, retrieve_testing
from rag.web import Handler, _progress_view, github_blob_url, public_status, run_features, run_flow, run_history, run_impact, run_query, run_testing, start_ingest
from unittest.mock import patch

from rag.plan import parse_queries, question_needs_plan, retrieval_model
from rag.usage import Meter, token_counts
from rag.intent import impact_target
from rag.search import (
    Hit,
    _feature_files,
    asks_for_features,
    _each_path_first,
    _hits_from_impact,
    _is_locale_or_messaging,
    _query_path_rank,
    _rerank_query_hits,
    code_literals,
    merge_hits,
    postgres_pattern,
    term_pattern,
    _drop_excluded,
    path_excluded,
    path_exclusions,
    path_in_scope,
    path_scopes,
    quoted_phrases,
    retrieve,
    search,
    topic_terms,
)
from rag.chunker import chunk_source, embed_text
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
        feature_step = "I submit the form with income '8000' and average 'average'"
        definition = (
            "When(/^I submit the form with income '([^\"']*)' and average '([^\"']*)'$/) do |num, period|\n"
            "  income_period_page.submit_income(num, period)\n"
            "end"
        )
        monthly = "When(/^I submit the form with income '([^\"']*)' and monthly$/) do |num|"
        self.assertEqual(
            step_needles(feature_step),
            ["I submit the form with income", "and average"],
        )
        self.assertTrue(definition_matches(definition, feature_step))
        self.assertFalse(definition_matches(monthly, feature_step))
        self.assertFalse(definition_matches(definition, "I submit the form with income '1000' and monthly"))
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

    def test_rspec_examples_keep_their_group(self):
        source = """RSpec.describe "Summary" do
  context "when the maximum threshold has been exceeded" do
    let(:application) { build(:online_application) }
    it { is_expected.to be true }

    it "returns the correct text" do
      expect(page).to have_content("more than")
    end
  end
end
"""
        by_symbol = {chunk.symbol: chunk for chunk in chunk_source("ruby", source) if chunk.symbol}
        group = "Summary / when the maximum threshold has been exceeded"
        example = group + " / returns the correct text"
        self.assertEqual(set(by_symbol), {"Summary", group, example})
        self.assertIn("let(:application)", by_symbol[group].content)
        self.assertIn("is_expected", by_symbol[group].content)
        self.assertIn("have_content", by_symbol[example].content)
        self.assertNotIn("let(:application)", by_symbol[example].content)

    def test_shared_examples_keep_their_name(self):
        source = """RSpec.shared_examples "a page" do
  it "shows the heading" do
    expect(page).to have_heading
  end
end
"""
        symbols = {chunk.symbol for chunk in chunk_source("ruby", source)}
        self.assertEqual(
            symbols,
            {"shared examples a page", "shared examples a page / shows the heading"},
        )

    def test_siteprism_sections_keep_their_elements(self):
        source = """class IncomePeriodPage < BasePage
  set_url '/income'

  section :content, '#content' do
    element :income_amount, '#income_period_amount'
  end

  def submit_income(num)
    content.income_amount.set(num)
  end
end
"""
        by_symbol = {chunk.symbol: chunk for chunk in chunk_source("ruby", source) if chunk.symbol}
        self.assertEqual(
            set(by_symbol),
            {
                "IncomePeriodPage",
                "IncomePeriodPage section :content",
                "IncomePeriodPage#submit_income",
            },
        )
        section = by_symbol["IncomePeriodPage section :content"]
        self.assertIn("element :income_amount", section.content)
        self.assertNotIn("def submit_income", section.content)
        self.assertIn("set_url", by_symbol["IncomePeriodPage"].content)
        self.assertNotIn("element :income_amount", by_symbol["IncomePeriodPage"].content)
        self.assertIn("content.income_amount.set", by_symbol["IncomePeriodPage#submit_income"].content)
        self.assertNotIn("element :income_amount", by_symbol["IncomePeriodPage#submit_income"].content)

    def test_factory_bot_traits_are_separate_symbols(self):
        source = """FactoryBot.define do
  factory :online_application do
    married { false }

    trait :savings_more_than_threshold do
      max_threshold_exceeded { true }
    end

    trait :refund do
      refund { true }
    end
  end
end
"""
        by_symbol = {chunk.symbol: chunk for chunk in chunk_source("ruby", source) if chunk.symbol}
        factory = "factory :online_application"
        threshold = "factory :online_application trait :savings_more_than_threshold"
        refund = "factory :online_application trait :refund"
        self.assertEqual(set(by_symbol), {factory, threshold, refund})
        self.assertIn("married", by_symbol[factory].content)
        self.assertNotIn("max_threshold_exceeded", by_symbol[factory].content)
        self.assertIn("max_threshold_exceeded", by_symbol[threshold].content)
        self.assertNotIn("refund { true }", by_symbol[threshold].content)
        self.assertIn("refund { true }", by_symbol[refund].content)

    def test_ruby_roles_are_embedded_with_the_chunk(self):
        page = embed_text(
            "features/support/page_objects/income_period_page.rb",
            "ruby",
            "IncomePeriodPage",
            "class IncomePeriodPage < BasePage\nend\n",
        )
        factory = embed_text(
            "spec/factories/online_applications.rb",
            "ruby",
            "factory :online_application",
            "factory :online_application do\nend\n",
        )
        spec = embed_text(
            "spec/services/fee_spec.rb",
            "ruby",
            "Fee",
            "RSpec.describe Fee do\nend\n",
        )
        self.assertIn("role: SitePrism page", page)
        self.assertIn("role: FactoryBot factory", factory)
        self.assertIn("role: RSpec", spec)

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

    def test_branch_list_puts_the_default_first(self):
        output = (
            "ref: refs/heads/master\tHEAD\n"
            "sha\tHEAD\n"
            "sha\trefs/heads/nightly-dev\n"
            "sha\trefs/heads/master\n"
            "sha\trefs/heads/../secret\n"
        )
        found = branch_list(output)
        self.assertEqual(found["default"], "master")
        self.assertEqual(found["branches"], ["master", "nightly-dev"])
        self.assertIsNone(branch_list("sha\tHEAD\n"))

    def test_blank_file(self):
        self.assertEqual(chunk_source("ruby", "\n\n"), [])


class CallEdgeTest(unittest.TestCase):
    def test_a_page_call_points_at_the_section_that_declares_the_element(self):
        from rag.edges import extract_edges

        source = """class IncomePeriodPage < BasePage
  set_url '/income'

  section :content, '#content' do
    element :income_amount, '#income_period_amount'
  end

  def submit_income(num)
    content.income_amount.set(num)
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("features/support/page_objects/income_period_page.rb", chunks)
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("IncomePeriodPage section :content", destinations)
        self.assertTrue(all(edge.confidence == "high" for _index, edge in edges))

    def test_a_factory_call_points_at_the_factory_and_trait(self):
        from rag.edges import extract_edges

        source = """RSpec.describe "Summary" do
  it "returns the correct text" do
    build(:online_application, :savings_more_than_threshold)
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("spec/views/summary_spec.rb", chunks)
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("factory :online_application", destinations)
        self.assertIn("factory :online_application trait :savings_more_than_threshold", destinations)

    def test_a_list_factory_call_points_at_the_factory(self):
        from rag.edges import extract_edges

        source = """RSpec.describe "Claims" do
  it "builds several" do
    create_list(:claim, 3, :ready)
    build_list(:claim, 2)
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("spec/models/claim_spec.rb", chunks)
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("factory :claim", destinations)
        self.assertIn("factory :claim trait :ready", destinations)

    def test_a_factory_bot_call_names_the_factory_and_ignores_a_list_count(self):
        from rag.edges import factory_symbols

        self.assertEqual(factory_symbols("FactoryBot.create(:claim)"), ["factory :claim"])
        self.assertEqual(factory_symbols("create(:claim)"), ["factory :claim"])
        self.assertEqual(
            factory_symbols("build(:claim, :ready)"),
            ["factory :claim", "factory :claim trait :ready"],
        )
        self.assertEqual(
            factory_symbols("create_list(:claim, 3, :ready)"),
            ["factory :claim", "factory :claim trait :ready"],
        )
        self.assertEqual(factory_symbols("online_application"), [])

    def test_impact_of_a_factory_bot_call_starts_at_the_factory(self):
        from rag.search import impact

        caller = Hit(
            2,
            "hmcts/et-full-system",
            "spec/models/claim_spec.rb",
            8,
            10,
            "builds a claim",
            "example",
            "build(:claim)",
            0.0,
            via="builds a claim → factory :claim",
        )

        def rows(_conn, params):
            self.assertEqual(params["exact"], "factory :claim")
            self.assertEqual(params["leaf"], "claim")
            return [(
                1,
                "hmcts/et-full-system",
                "spec/factories/claims.rb",
                1,
                4,
                "factory :claim",
                "factory",
                "factory :claim do\nend\n",
            )], False

        def callers(_conn, _repos, definitions, _seen):
            self.assertEqual([hit.symbol for hit in definitions], ["factory :claim"])
            return {caller.chunk_id: caller}, [], 0, False

        with (
            patch("rag.search._impact_rows", side_effect=rows),
            patch("rag.search._callers_and_gaps", side_effect=callers),
            patch("rag.search.search", return_value=([], False)),
        ):
            found = impact(None, [1], "FactoryBot.create(:claim)")
        self.assertEqual([hit.symbol for hit in found["definitions"]], ["factory :claim"])
        self.assertEqual(found["references"], [caller])
        self.assertEqual(found["callers_omitted"], 0)
        self.assertEqual(
            found["diagram"]["edges"],
            [{"from": "builds a claim", "to": "factory :claim"}],
        )

    def test_a_call_path_becomes_a_diagram(self):
        from rag.search import relationship_diagram

        definition = Hit(1, "repo", "app/models/fee.rb", 1, 4, "Fee#save", "method", "def save\nend\n", 0)
        step = Hit(
            2, "repo", "features/step_definitions/fee_steps.rb", 3, 5,
            "I save the fee", "step", "save", 0, via="I save the fee → Fee#save",
        )
        feature = Hit(
            3, "repo", "features/fee.feature", 8, 10,
            "Fee / saves", "scenario", "When I save the fee", 0,
            via="Fee / saves → I save the fee → Fee#save",
        )
        mention = Hit(4, "repo", "README.md", 1, 2, None, "file", "save", 0)
        diagram = relationship_diagram([definition], [step, feature, mention])
        self.assertEqual(
            diagram["edges"],
            [
                {"from": "I save the fee", "to": "Fee#save"},
                {"from": "Fee / saves", "to": "I save the fee"},
            ],
        )
        depths = {node["label"]: node["depth"] for node in diagram["nodes"]}
        kinds = {node["label"]: node["kind"] for node in diagram["nodes"]}
        self.assertEqual(depths["Fee#save"], 0)
        self.assertEqual(depths["I save the fee"], 1)
        self.assertEqual(depths["Fee / saves"], 2)
        self.assertEqual(kinds["Fee#save"], "definition")
        self.assertNotIn("README.md", depths)
        paths = {node["label"]: node.get("path") for node in diagram["nodes"]}
        self.assertEqual(paths["Fee / saves"], "features/fee.feature")
        self.assertEqual(paths["I save the fee"], "features/step_definitions/fee_steps.rb")
        self.assertEqual(paths["Fee#save"], "app/models/fee.rb")

    def test_a_factory_symbol_is_not_the_method_of_the_same_name(self):
        from rag.search import _defines_target

        method = Hit(
            1, "hmcts/hwf-publicapp", "app/controllers/application_controller.rb",
            44, 46, "ApplicationController#online_application", "method",
            "def online_application\nend\n", 0.0,
        )
        factory = Hit(
            2, "hmcts/hwf-publicapp", "spec/factories/online_applications.rb",
            2, 4, "factory :online_application", "factory",
            "factory :online_application do\nend\n", 0.0,
        )
        self.assertFalse(_defines_target(method, "factory :online_application"))
        self.assertTrue(_defines_target(factory, "factory :online_application"))
        self.assertTrue(_defines_target(method, "ApplicationController#online_application"))

    def test_a_bare_call_is_stored_when_the_method_is_elsewhere(self):
        from rag.edges import CallIndex, IndexedChunk, extract_edges

        source = """Given('a claimant is on the page') do
  et1_answer_case_heard_by_page
  create(:claim)
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("features/step_definitions/example_steps.rb", chunks)
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("et1_answer_case_heard_by_page", destinations)
        self.assertNotIn("end", destinations)
        self.assertNotIn("create", destinations)
        helper = IndexedChunk(
            9, "features/support/helpers/et1_helper.rb",
            "Et1Helper#et1_answer_case_heard_by_page", "method", "def et1_answer_case_heard_by_page\nend\n",
        )
        module = IndexedChunk(8, helper.path, "Et1Helper", "module", "module Et1Helper\n")
        calls = CallIndex([module, helper])
        self.assertEqual(
            calls.targets("et1_answer_case_heard_by_page", None, "features/step_definitions/example_steps.rb"),
            ([9], True),
        )

    def test_a_bare_call_inside_a_longer_line_is_found(self):
        from rag.edges import extract_edges, inline_calls

        source = """def total(fee, discount = 0)
  # remission_amount is described here, not called
  amount = fee_amount - discount
  label = "Pay #{formatted_total} now"
  note = 'ignore quoted_name here'
  rows.each { |row| puts row }
  expect(remission_amount).to eq(amount)
  store(key: fee, other_key: lookup_value)
  record.save if valid_fee?
  match(/pattern_word here/)
rescue StandardError => error
  log(error)
end
"""
        names = inline_calls(source)
        for expected in ("fee_amount", "formatted_total", "remission_amount", "lookup_value", "valid_fee?"):
            self.assertIn(expected, names)
        for unexpected in (
            "fee", "discount", "amount", "label", "note", "row", "error", "quoted_name",
            "key", "other_key", "save", "total", "pattern_word", "rescue", "if",
        ):
            self.assertNotIn(unexpected, names)
        self.assertNotIn("claim", inline_calls("expect(claim).to be_valid", {"claim"}))

        step = """Given('a fee is entered') do
  @total = fee_amount + 1
end
"""
        chunks = chunk_source("ruby", step)
        edges = extract_edges("features/step_definitions/fee_steps.rb", chunks)
        inline = {edge.dst_symbol for _index, edge in edges if edge.edge_type == "inline"}
        self.assertIn("fee_amount", inline)
        self.assertNotIn("fee", inline)

    def test_a_name_defined_in_several_places_is_a_possible_target(self):
        from rag.edges import CallIndex, IndexedChunk

        steps = "features/step_definitions/et_steps.rb"
        chunks = [
            IndexedChunk(1, "features/support/et1.rb", "Et1Helper", "module", "module Et1Helper\n"),
            IndexedChunk(2, "features/support/et3.rb", "Et3Helper", "module", "module Et3Helper\n"),
            IndexedChunk(4, "features/support/et1.rb", "Et1Helper#fill_in_all", "method", "def fill_in_all\nend\n"),
            IndexedChunk(5, "features/support/et3.rb", "Et3Helper#fill_in_all", "method", "def fill_in_all\nend\n"),
        ]
        chunks += [
            IndexedChunk(10 + number, f"features/support/h{number}.rb", f"H{number}#save", "method", "def save\nend\n")
            for number in range(8)
        ]
        calls = CallIndex(chunks)
        self.assertEqual(calls.targets("fill_in_all", None, steps), ([4, 5], False))
        self.assertEqual(calls.targets("fill_in_all", "Et3Helper", "features/support/et3.rb"), ([5], True))
        self.assertEqual(calls.targets("save", None, steps), ([], True))
        self.assertEqual(calls.targets("Fee.save", None, steps), ([], True))

    def test_a_bare_call_only_reaches_what_ruby_can_reach(self):
        from rag.edges import CallIndex, IndexedChunk

        chunks = [
            IndexedChunk(1, "app/models/base.rb", "Views::Base", "class", "module Views\n  class Base\n"),
            IndexedChunk(2, "app/models/base.rb", "Views::Base#to_flag", "method", "def to_flag\nend\n"),
            IndexedChunk(
                3, "app/models/detail.rb", "Views::Detail", "class",
                "module Views\n  class Detail < Base\n    include Money\n",
            ),
            IndexedChunk(4, "app/models/detail.rb", "Views::Detail#fee", "method", "def fee\n  to_flag\nend\n"),
            IndexedChunk(5, "app/models/other.rb", "Other", "class", "class Other\n"),
            IndexedChunk(6, "app/models/other.rb", "Other#to_flag", "method", "def to_flag\nend\n"),
            IndexedChunk(7, "app/lib/money.rb", "Money", "module", "module Money\n"),
            IndexedChunk(8, "app/lib/money.rb", "Money#pounds", "method", "def pounds\nend\n"),
            IndexedChunk(9, "spec/models/detail_spec.rb", "sample_fee", "method", "def sample_fee\nend\n"),
            IndexedChunk(10, "features/support/helpers.rb", "fee_page", "method", "def fee_page\nend\n"),
            IndexedChunk(11, "app/helpers/list_helper.rb", "ListHelper", "module", "module ListHelper\n"),
            IndexedChunk(12, "app/helpers/list_helper.rb", "ListHelper#page", "method", "def page\nend\n"),
            IndexedChunk(13, "app/models/fee.rb", "Fee", "class", "class Fee\n"),
        ]
        calls = CallIndex(chunks)
        # A subclass reaches the method it inherits, and nothing else does.
        self.assertEqual(calls.targets("to_flag", "Views::Detail", "app/models/detail.rb"), ([2], True))
        self.assertEqual(calls.targets("to_flag", "Other", "app/models/other.rb"), ([6], True))
        self.assertEqual(calls.targets("to_flag", None, "spec/models/detail_spec.rb"), ([], True))
        self.assertEqual(calls.targets("fee", None, "spec/models/detail_spec.rb"), ([], True))
        # A class has a module's method only when it includes the module.
        self.assertEqual(calls.targets("pounds", "Views::Detail", "app/models/detail.rb"), ([8], True))
        self.assertEqual(calls.targets("pounds", "Other", "app/models/other.rb"), ([], True))
        self.assertEqual(calls.targets("pounds", None, "spec/lib/money_spec.rb"), ([8], True))
        # A method written in a spec stays in that file, and application code
        # does not call test helpers.
        self.assertEqual(calls.targets("sample_fee", None, "spec/models/detail_spec.rb"), ([9], True))
        self.assertEqual(calls.targets("sample_fee", None, "spec/models/other_spec.rb"), ([], True))
        own = IndexedChunk(14, "spec/features/fee_spec.rb", "fee_page", "method", "def fee_page\nend\n")
        shared = IndexedChunk(15, "spec/support/steps.rb", "fee_page", "method", "def fee_page\nend\n")
        both = CallIndex([own, shared])
        self.assertEqual(both.targets("fee_page", None, "spec/features/fee_spec.rb"), ([14], True))
        self.assertEqual(both.targets("fee_page", None, "spec/features/other_spec.rb"), ([15], True))
        self.assertEqual(calls.targets("fee_page", None, "features/step_definitions/fee_steps.rb"), ([10], True))
        self.assertEqual(calls.targets("fee_page", "Other", "app/models/other.rb"), ([], True))
        # A lower-case name is a call, not the class of the same name, and
        # Capybara's page is not a helper that happens to be called page.
        self.assertEqual(calls.targets("fee", None, "features/step_definitions/fee_steps.rb"), ([], True))
        self.assertEqual(calls.targets("page", None, "spec/features/list_spec.rb"), ([], True))
        self.assertEqual(calls.targets("page", "ListHelper", "app/helpers/list_helper.rb"), ([12], True))

    def test_a_possible_hop_is_marked_in_the_path_and_the_diagram(self):
        from rag.edges import walk_callers
        from rag.search import relationship_diagram

        incoming = {1: [(2, "fill_in_all")], 2: [(3, "step")]}
        labels = {1: "Et1::Page#fill_in_all", 2: "features/step_definitions/et1_steps.rb", 3: "ET1 / fills in"}
        found, omitted = walk_callers({1}, incoming, labels, uncertain={(2, 1)})
        self.assertEqual(
            found,
            [
                (2, "features/step_definitions/et1_steps.rb ⇢ Et1::Page#fill_in_all"),
                (3, "ET1 / fills in → features/step_definitions/et1_steps.rb ⇢ Et1::Page#fill_in_all"),
            ],
        )
        self.assertEqual(omitted, 0)
        definition = Hit(1, "repo", "page.rb", 1, 4, "Et1::Page#fill_in_all", "method", "", 0)
        callers = [
            Hit(chunk_id, "repo", "x", 1, 2, labels[chunk_id], "method", "", 0, via=via)
            for chunk_id, via in found
        ]
        diagram = relationship_diagram([definition], callers)
        self.assertEqual(
            diagram["edges"],
            [
                {
                    "from": "features/step_definitions/et1_steps.rb",
                    "to": "Et1::Page#fill_in_all",
                    "possible": True,
                },
                {"from": "ET1 / fills in", "to": "features/step_definitions/et1_steps.rb"},
            ],
        )

    def test_a_dynamic_call_has_no_target(self):
        from rag.edges import extract_edges

        source = """class Fee
  def save
    send(:store)
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = [edge for _index, edge in extract_edges("app/models/fee.rb", chunks)]
        gaps = [edge for edge in edges if edge.confidence == "none"]
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0].dst_symbol)
        self.assertNotIn("store", {edge.dst_symbol for edge in edges})

    def test_an_alias_points_at_the_old_method(self):
        from rag.edges import extract_edges

        source = """class Fee
  alias_method :store, :save

  def save
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("app/models/fee.rb", chunks)
        self.assertIn("Fee#save", {edge.dst_symbol for _index, edge in edges})

    def test_const_new_links_an_unprefixed_class_in_its_module(self):
        from rag.edges import CallIndex, IndexedChunk, extract_edges

        source = """module C100App
  class ApplicationDecisionTree
    def proceed_to_payment
      PaymentsFlowControl.new(c100_application).payment_url
    end
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("app/services/c100_app/application_decision_tree.rb", chunks)
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("PaymentsFlowControl", destinations)
        self.assertIn("PaymentsFlowControl#payment_url", destinations)
        self.assertNotIn("Time", {
            edge.dst_symbol
            for _index, edge in extract_edges(
                "app/models/fee.rb",
                chunk_source("ruby", "class Fee\n  def at\n    Time.new\n  end\nend\n"),
            )
        })
        target = IndexedChunk(
            1, "app/services/c100_app/payments_flow_control.rb",
            "C100App::PaymentsFlowControl", "class", "module C100App\n  class PaymentsFlowControl\n",
        )
        other = IndexedChunk(
            2, "app/services/other/payments_flow_control.rb",
            "Other::PaymentsFlowControl", "class", "module Other\n  class PaymentsFlowControl\n",
        )
        caller = IndexedChunk(
            3, "app/services/c100_app/application_decision_tree.rb",
            "C100App::ApplicationDecisionTree#proceed_to_payment", "method",
            "def proceed_to_payment\n  PaymentsFlowControl.new(app).payment_url\nend\n",
        )
        calls = CallIndex([target, other, caller])
        self.assertEqual(
            calls.targets("PaymentsFlowControl", "C100App::ApplicationDecisionTree", caller.path),
            ([1], True),
        )
        self.assertEqual(
            calls.targets("PaymentsFlowControl", "Unrelated::Thing", "app/unrelated.rb"),
            ([], True),
        )

    def test_chained_new_links_a_keyword_named_method(self):
        from rag.edges import CallIndex, IndexedChunk, extract_edges

        source = """class PartPaymentsController
  def return_application
    if ResolverService.new(part_payment, current_user).return
      redirect_to root_path
    end
  end
end
"""
        chunks = chunk_source("ruby", source)
        edges = extract_edges("app/controllers/part_payments_controller.rb", chunks)
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("ResolverService#return", destinations)
        self.assertIn("ResolverService", destinations)
        bare = extract_edges(
            "app/models/fee.rb",
            chunk_source("ruby", "class Fee\n  def save\n    return value\n  end\nend\n"),
        )
        self.assertNotIn("return", {edge.dst_symbol for _index, edge in bare})
        method = IndexedChunk(
            5, "app/services/resolver_service.rb", "ResolverService#return", "method",
            "def return\nend\n",
        )
        klass = IndexedChunk(
            4, "app/services/resolver_service.rb", "ResolverService", "class", "class ResolverService\n",
        )
        calls = CallIndex([klass, method])
        self.assertEqual(calls.targets("ResolverService#return"), ([5], True))
        foo_edges = extract_edges(
            "app/models/foo.rb",
            chunk_source("ruby", "class Foo\n  def run\n    Bar.new.baz\n  end\nend\n"),
        )
        self.assertIn("Bar#baz", {edge.dst_symbol for _index, edge in foo_edges})

    def test_a_page_accessor_links_to_the_page_class(self):
        from rag.edges import CallIndex, IndexedChunk, extract_edges

        step = """Given('I open responses') do
  admin_pages.responses_page.load
end
"""
        edges = extract_edges(
            "features/step_definitions/et3_admin_responses_page.steps.rb",
            chunk_source("ruby", step),
        )
        destinations = {edge.dst_symbol for _index, edge in edges}
        self.assertIn("ResponsesPage", destinations)
        self.assertIn("ResponsesPage#load", destinations)

        existing = extract_edges(
            "features/step_definitions/claimant_steps.rb",
            chunk_source("ruby", "Given('x') do\n  claimant_page.fill_in_name\nend\n"),
        )
        self.assertIn("ClaimantPage#fill_in_name", {edge.dst_symbol for _index, edge in existing})

        support = extract_edges(
            "features/support/navigation_helper.rb",
            chunk_source(
                "ruby",
                "def to_income_kind\n  dependent_page.submit_child_bands\n  contact_page.valid_email\nend\n",
            ),
        )
        support_destinations = {edge.dst_symbol for _index, edge in support}
        self.assertIn("DependentPage#submit_child_bands", support_destinations)
        self.assertIn("ContactPage#valid_email", support_destinations)

        app_edges = extract_edges(
            "app/models/user.rb",
            chunk_source("ruby", "class User\n  def save\n    user.update_page\n  end\nend\n"),
        )
        self.assertNotIn("UpdatePage", {edge.dst_symbol for _index, edge in app_edges})

        page = IndexedChunk(
            7, "features/support/page_objects/admin/responses_page.rb",
            "EtFullSystem::Test::Admin::ResponsesPage", "class",
            "module EtFullSystem\n  module Test\n    module Admin\n      class ResponsesPage\n",
        )
        calls = CallIndex([page])
        self.assertEqual(
            calls.targets("ResponsesPage", None, "features/step_definitions/x.rb"),
            ([7], True),
        )

    def test_a_feature_step_points_at_its_step_definition(self):
        from rag.edges import IndexedChunk, link_steps

        feature = IndexedChunk(
            1,
            "features/income.feature",
            "Income / enters the amount",
            "scenario",
            "Feature: Income\n  Scenario: enters the amount\n    When I enter the income\n",
        )
        step = IndexedChunk(
            2,
            "features/step_definitions/income_steps.rb",
            "I enter the income",
            "method",
            "When(/^I enter the income$/) do\n  income_period_page.submit_income(1)\nend\n",
        )
        edges = link_steps([feature, step])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0][0], 1)
        self.assertEqual(edges[0][1].dst_chunk_id, 2)
        self.assertEqual(edges[0][1].confidence, "high")

    def test_the_walk_follows_callers_and_stops_at_a_cycle(self):
        from rag.edges import walk_callers

        incoming = {1: [(2, "call")], 2: [(3, "step")], 3: [(2, "back")]}
        labels = {1: "Page#submit", 2: "enter the income", 3: "Income / enters the amount"}
        found, omitted = walk_callers({1}, incoming, labels)
        self.assertEqual(
            found,
            [
                (2, "enter the income → Page#submit"),
                (3, "Income / enters the amount → enter the income → Page#submit"),
            ],
        )
        self.assertEqual(omitted, 0)

    def test_the_walk_reports_callers_past_the_limit(self):
        from rag.edges import walk_callers

        incoming = {1: [(2, "a"), (3, "b")], 2: [(4, "c")]}
        labels = {1: "factory :claim", 2: "one", 3: "two", 4: "three"}
        found, omitted = walk_callers({1}, incoming, labels, limit=1)
        self.assertEqual(found, [(2, "one → factory :claim")])
        self.assertEqual(omitted, 2)

    def test_user_facing_files_sort_ahead_of_specs(self):
        from rag.search import _surface_rank

        self.assertLess(
            _surface_rank("features/income.feature"),
            _surface_rank("features/support/page_objects/income_period_page.rb"),
        )
        self.assertLess(
            _surface_rank("app/controllers/applications_controller.rb"),
            _surface_rank("spec/models/fee_spec.rb"),
        )


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
        self.assertTrue(has_declaration("  factory :online_application do", "online_application"))
        self.assertTrue(
            has_declaration("    trait :savings_more_than_threshold do", "savings_more_than_threshold")
        )


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
        self.assertTrue(is_skipped(Path("spec/fixtures/vcr_cassettes/lookup.yml")))
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


class FeaturesTest(unittest.TestCase):
    STEP = "Then I should see this date can't be in the future error message"
    REFUND = (
        "Write a Cucumber feature for an applicant who already paid the fee and wants a refund. "
        "The payment date cannot be in the future or more than three months ago. "
        "Reuse existing step phrasing."
    )

    def test_a_pasted_step_keeps_its_keyword(self):
        self.assertEqual(gherkin_lines(self.STEP), [self.STEP])
        self.assertFalse(is_drafting_request(self.STEP))
        with patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"):
            self.assertTrue(question_needs_plan(self.STEP))

    def test_and_given_when_and_but_match_the_same_step_text(self):
        body = "I should see this date can't be in the future error message"
        for keyword in ("Then", "And", "Given", "When", "But"):
            self.assertEqual(matched_step_texts(f"{keyword} {body}"), [body])

    def test_a_feature_draft_drops_the_instruction_and_keeps_the_rule(self):
        self.assertTrue(is_drafting_request(self.REFUND))
        text = behaviour_text(self.REFUND)
        self.assertNotIn("Write a Cucumber feature", text)
        self.assertNotIn("Reuse existing", text)
        lowered = text.lower()
        self.assertIn("payment date", lowered)
        self.assertIn("future", lowered)
        self.assertIn("three months", lowered)
        with patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"):
            self.assertTrue(question_needs_plan(self.REFUND))

    def test_a_draft_answer_may_only_quote_steps_from_the_passages(self):
        instructions = _query_instructions(self.REFUND, [], draft=True)
        self.assertIn("Do not write a substitute step.", instructions)
        self.assertIn("name that check", instructions)

    def test_a_pasted_step_is_searched_as_that_line_under_features(self):
        feature = Hit(
            1, "hmcts/hwf-publicapp", "features/fee.feature", 30, 33,
            "Fee page / Date is in the future", "scenario", self.STEP, 0.0,
        )
        step = Hit(
            2, "hmcts/hwf-publicapp", "features/step_definitions/fee_steps.rb", 40, 44,
            None, "file", "Then(/^I should see this date can't be in the future error message$/) do", 0.0,
        )
        seen = {}

        def fake_literal(_conn, _repos, scopes, literals, _k, excluded=None):
            seen["scopes"] = scopes
            seen["literals"] = literals
            return [feature], 0.01, False

        def fake_follow(_conn, _repos, quoted, _hits):
            seen["quoted"] = quoted
            return [step]

        def fake_search(*_args, **_kwargs):
            raise AssertionError("a pasted step is not planned or embedded")

        with (
            patch("rag.features.literal_hits", side_effect=fake_literal),
            patch("rag.features.follow_matched_steps", side_effect=fake_follow),
            patch("rag.features.search", side_effect=fake_search),
            patch("rag.plan.search_queries", side_effect=AssertionError("planner")),
        ):
            hits, queries, truncated, drafting = retrieve_features(None, [1], self.STEP, 8, Meter())
        self.assertFalse(drafting)
        self.assertFalse(truncated)
        body = "I should see this date can't be in the future error message"
        self.assertEqual(seen["scopes"], ["features"])
        self.assertEqual(seen["literals"], [body])
        self.assertIn(body, seen["quoted"])
        self.assertNotIn("Then ", seen["literals"][0])
        self.assertEqual(hits, [feature, step])
        self.assertTrue(queries[0].startswith("step:"))

    def test_a_feature_draft_searches_the_behaviour_on_the_code_index(self):
        scenario = Hit(
            3, "hmcts/hwf-publicapp", "features/fee.feature", 25, 28,
            "Fee page / Date exceeding 3 months", "scenario", "Then the date is too old", 0.2,
        )
        seen = {}

        def fake_search(_conn, _repos, text, _k, _meter, **options):
            seen["text"] = text
            seen["index"] = options.get("index")
            seen["fusion"] = options.get("fusion")
            return [scenario], False

        with (
            patch("rag.features.search", side_effect=fake_search),
            patch("rag.features.literal_hits", side_effect=AssertionError("no step to quote")),
            patch("rag.plan.search_queries", side_effect=AssertionError("planner")),
        ):
            hits, queries, _truncated, drafting = retrieve_features(None, [1], self.REFUND, 8, Meter())
        self.assertTrue(drafting)
        self.assertEqual(hits, [scenario])
        self.assertIn("payment date", seen["text"])
        self.assertNotIn("Write a Cucumber feature", seen["text"])
        self.assertNotIn("Reuse existing", seen["text"])
        self.assertEqual(seen["index"], "code")
        self.assertEqual(seen["fusion"], "rrf")
        self.assertIn(seen["text"], queries)

    def test_feature_files_are_listed_before_other_directories(self):
        app = Hit(
            1, "hmcts/hwf-publicapp", "app/models/forms/fee.rb", 45, 53,
            "Forms::Fee#fee_dates_paid", "method", "def fee_dates_paid", 0.4,
        )
        feature = Hit(
            2, "hmcts/hwf-publicapp", "features/fee.feature", 25, 28,
            "Fee page / Date exceeding 3 months", "scenario", "Then the date is too old", 0.3,
        )
        spec = Hit(
            3, "hmcts/hwf-publicapp", "spec/models/forms/fee_spec.rb", 34, 38,
            None, "example", "it", 0.2,
        )
        feature_spec = Hit(
            5, "hmcts/hwf-publicapp", "spec/features/pages/fee_question_spec.rb", 82, 90,
            None, "example", "it", 0.25,
        )
        steps = Hit(
            4, "hmcts/hwf-publicapp", "features/step_definitions/fee_steps.rb", 1, 20,
            None, "file", "Then", 0.1,
        )

        def fake_search(_conn, _repos, _text, _k, _meter, **_options):
            return [app, feature, feature_spec, spec, steps], False

        with patch("rag.features.search", side_effect=fake_search):
            hits, _queries, truncated, drafting = retrieve_features(None, [1], self.REFUND, 4, Meter())
        self.assertTrue(drafting)
        self.assertTrue(truncated)
        self.assertEqual(
            [hit.path for hit in hits],
            [
                "features/fee.feature",
                "features/step_definitions/fee_steps.rb",
                "app/models/forms/fee.rb",
                "spec/features/pages/fee_question_spec.rb",
            ],
        )


class TestingTest(unittest.TestCase):
    QUESTION = (
        "How do I run the Cucumber suite, features/fee.feature, and one scenario, "
        "and how do I debug a scenario I am writing?"
    )

    def test_command_phrases_include_a_named_tag(self):
        phrases = command_phrases("how do I run @happy_path")
        self.assertIn("bundle exec cucumber", phrases)
        self.assertIn("SHOW_BROWSER", phrases)
        self.assertIn("@happy_path", phrases)

    def test_the_answer_quotes_only_commands_from_the_passages(self):
        instructions = _query_instructions(self.QUESTION, [], testing=True)
        self.assertIn("Do not say debugging is missing", instructions)
        self.assertIn("do not write cucumber a second time", instructions)
        self.assertIn("Feature path", instructions)
        self.assertIn("not @wip", instructions)
        self.assertIn("do not introduce byebug or pry", instructions)
        self.assertIn("Do not describe RSpec", instructions)
        self.assertIn("file:line", instructions)

    def test_commands_are_looked_up_without_the_planner(self):
        readme = Hit(
            1, "hmcts/hwf-publicapp", "README.md", 90, 96,
            None, "file", "CAPYBARA_SERVER_PORT=random bundle exec rake parallel:features", 0.0,
        )
        profile = Hit(
            2, "hmcts/hwf-publicapp", "config/cucumber.yml", 6, 10,
            None, "file", "wip: --tags @wip:3 --wip features", 0.0,
        )
        seen = {}

        def fake_literal(_conn, _repos, scopes, literals, _k, excluded=None):
            seen["scopes"] = scopes
            seen["literals"] = literals
            seen["excluded"] = excluded
            return [readme, profile], 0.01, False

        def fake_search(_conn, _repos, text, _k, _meter, **options):
            seen["text"] = text
            seen["index"] = options.get("index")
            seen["fusion"] = options.get("fusion")
            seen["search_excluded"] = options.get("excluded")
            return [], False

        with (
            patch("rag.testing.literal_hits", side_effect=fake_literal),
            patch("rag.testing.search", side_effect=fake_search),
            patch("rag.plan.search_queries", side_effect=AssertionError("planner")),
        ):
            hits, queries, truncated = retrieve_testing(None, [1], "How do I run the Cucumber suite?", 8, Meter())
        self.assertFalse(truncated)
        self.assertEqual(seen["scopes"], [])
        self.assertEqual(seen["excluded"], ["spec", "test", "app"])
        self.assertIn("bundle exec cucumber", seen["literals"])
        self.assertIn("parallel:features", seen["literals"])
        self.assertEqual(seen["index"], "code")
        self.assertEqual(seen["fusion"], "rrf")
        self.assertEqual(seen["search_excluded"], ["spec", "test", "app"])
        self.assertIn("debug a scenario", seen["text"])
        self.assertEqual([hit.path for hit in hits], ["README.md", "config/cucumber.yml"])
        self.assertTrue(any("bundle exec cucumber" in query for query in queries))

    def test_a_named_feature_is_kept_and_other_features_are_not(self):
        readme = Hit(
            1, "ministryofjustice/c100-application", "README.md", 52, 67,
            None, "file", "bundle exec cucumber features/miam.feature -t @happy_path", 0.0,
        )
        other = Hit(
            2, "ministryofjustice/c100-application", "features/miam.feature", 1, 8,
            None, "scenario", "Scenario: happy path", 0.2,
        )
        fee = Hit(
            3, "hmcts/hwf-publicapp", "features/fee.feature", 25, 30,
            "Fee page / Date exceeding 3 months", "scenario", "Scenario: Date exceeding 3 months", 0.0,
        )
        spec = Hit(
            4, "hmcts/hwf-publicapp", "spec/models/fee_spec.rb", 1, 8,
            None, "example", "bundle exec rspec", 0.4,
        )
        steps = Hit(
            5, "hmcts/hwf-publicapp", "features/step_definitions/fee_steps.rb", 1, 10,
            None, "file", "Then", 0.3,
        )
        calls = []

        def fake_literal(_conn, _repos, scopes, _literals, _k, excluded=None):
            calls.append(scopes)
            if scopes:
                return [fee], 0.01, False
            return [spec, other, readme, steps], 0.01, False

        def fake_search(*_args, **_kwargs):
            return [other, spec], False

        with (
            patch("rag.testing.literal_hits", side_effect=fake_literal),
            patch("rag.testing.search", side_effect=fake_search),
            patch("rag.plan.search_queries", side_effect=AssertionError("planner")),
        ):
            hits, queries, _truncated = retrieve_testing(None, [1], self.QUESTION, 8, Meter())
        self.assertEqual(calls[0], [])
        self.assertEqual(calls[1], ["features/fee.feature"])
        self.assertEqual(
            [hit.path for hit in hits],
            ["README.md", "features/fee.feature"],
        )
        self.assertTrue(any(query.startswith("feature:") for query in queries))

    def test_a_bare_feature_name_uses_the_indexed_directory(self):
        self.assertEqual(
            resolve_feature_paths(
                ["dob_married.feature"],
                ["features/fee.feature", "features/dob/dob_married.feature"],
            ),
            ["features/dob/dob_married.feature"],
        )
        self.assertEqual(
            resolve_feature_paths(
                ["features/dob_married.feature"],
                ["features/dob/dob_married.feature"],
            ),
            ["features/dob/dob_married.feature"],
        )
        self.assertEqual(
            resolve_feature_paths(
                ["features/fee.feature"],
                ["features/fee.feature", "features/dob/dob_married.feature"],
            ),
            ["features/fee.feature"],
        )
        feature = Hit(
            6, "hmcts/hwf-publicapp", "features/dob/dob_married.feature", 12, 18,
            "Married applicant", "scenario", "Scenario: Married applicant", 0.0,
        )
        readme = Hit(
            1, "hmcts/hwf-publicapp", "features/README.md", 17, 24,
            None, "file", "bundle exec cucumber cucumber features/summary.feature", 0.0,
        )
        calls = []

        def fake_literal(_conn, _repos, scopes, _literals, _k, excluded=None):
            calls.append(scopes)
            if scopes:
                return [feature], 0.01, False
            return [readme], 0.01, False

        with (
            patch("rag.testing.indexed_feature_paths", return_value=["features/dob/dob_married.feature"]),
            patch("rag.testing.literal_hits", side_effect=fake_literal),
            patch("rag.testing.search", return_value=([], False)),
            patch("rag.plan.search_queries", side_effect=AssertionError("planner")),
        ):
            hits, queries, _truncated = retrieve_testing(
                None, [1], "How do I run dob_married.feature? How do I debug it?", 8, Meter(),
            )
        self.assertEqual(calls[1], ["features/dob/dob_married.feature"])
        self.assertEqual([hit.path for hit in hits], ["features/README.md", "features/dob/dob_married.feature"])
        self.assertIn("feature: features/dob/dob_married.feature", queries)


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

    def test_ignored_folders_are_left_out_of_the_directory(self):
        question = (
            "look under /features for birth date. "
            "Ignore files in folders /features/support and /features/step_definitions"
        )
        excluded = ["features/support", "features/step_definitions"]
        self.assertEqual(path_scopes(question), ["features"])
        self.assertEqual(code_literals(question), [])
        self.assertEqual(path_exclusions(question), excluded)
        self.assertFalse(path_excluded("features/applicant/birth.feature", excluded))
        self.assertTrue(path_excluded("features/support/page_objects/dob_page.rb", excluded))
        self.assertTrue(path_excluded("features/step_definitions/birth_steps.rb", excluded))
        kept = Hit(1, "hmcts/hwf-publicapp", "features/applicant/birth.feature", 1, 4, None, "scenario", "birth date", 1)
        support = Hit(2, "hmcts/hwf-publicapp", "features/support/page_objects/dob_page.rb", 1, 4, None, "file", "birth date", 1)
        self.assertEqual(_drop_excluded([kept, support], excluded), [kept])
        self.assertEqual(path_exclusions("look under /features not under /features/support"), ["features/support"])

        seen = {}

        def fake_search(_conn, _repos, query, _k, _meter, scopes, skipped, **_options):
            seen["query"] = query
            seen["scopes"] = scopes
            seen["skipped"] = skipped
            return [kept], False

        meter = Meter()
        with (
            patch("rag.search.search", side_effect=fake_search),
            patch("rag.plan.search_queries", return_value=[question]),
        ):
            hits, queries, truncated = retrieve(None, [1], question, 8, meter)
        self.assertEqual(hits, [kept])
        self.assertEqual(queries, [question])
        self.assertFalse(truncated)
        self.assertEqual(seen["scopes"], ["features"])
        self.assertEqual(seen["skipped"], excluded)
        search_stage = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Search the index")
        self.assertIn("Left out features/support and features/step_definitions.", search_stage["note"])

    def test_a_directory_filter_is_not_searched_again(self):
        from rag.search import _without_scope_queries

        question = (
            "look under /features for birth date. "
            "Ignore files in folders /features/support and /features/step_definitions"
        )
        queries = _without_scope_queries([question, "/features"], ["features"])
        self.assertEqual(queries, [question])

    def test_results_are_sorted_by_relevance_across_repositories(self):
        public = Hit(
            1, "hmcts/hwf-publicapp", "features/dob/dob_single.feature",
            7, 12, None, "scenario", "Date of birth", 0.75,
        )
        staff_close = Hit(
            2, "hmcts/hwf-staffapp", "features/personal_details.feature",
            17, 19, None, "scenario", "Date of birth blank", 0.76,
        )
        staff_far = Hit(
            3, "hmcts/hwf-staffapp", "features/approve.feature",
            1, 1, None, "scenario", "Ask a manager page", 0.69,
        )

        def fake(_conn, repos, _question, _k, _embedding, _scopes, _excluded, **_options):
            if repos == [1]:
                return [public], False
            return [staff_far, staff_close], False

        class Embedder:
            def embed_query(self, _question):
                return [0.0, 0.0, 0.0]

        with (
            patch("rag.embedder.Embedder", return_value=Embedder()),
            patch("rag.search._search_with_embedding", side_effect=fake),
        ):
            hits, truncated = search(None, [1, 2], "birth date", 2, None, ["features"], [])
        self.assertEqual([hit.path for hit in hits], [staff_close.path, public.path])
        self.assertTrue(truncated)

    def test_a_question_without_a_feature_file_does_not_follow_the_top_hit(self):
        from rag.search import _feature_chain

        question = (
            "look under /features for birth date. "
            "Ignore files in folders /features/support and /features/step_definitions"
        )
        top = Hit(
            3, "hmcts/hwf-staffapp", "features/approve.feature",
            1, 1, "Ask a manager page", "scenario", "Feature: Ask a manager page", 0.69,
        )
        self.assertEqual(_feature_chain(None, [2], question, [top]), [])

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
            hits, queries, truncated = retrieve(None, [1], question, 16, meter)
        self.assertEqual(hits, [hit])
        self.assertEqual(queries, [question])
        self.assertFalse(truncated)
        embed = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Embed the searches")
        self.assertEqual(embed["note"], "The values were matched directly, so nothing was embedded.")
        self.assertIsNone(embed["total_tokens"])

    def test_a_literal_search_reports_when_more_matches_were_cut_off(self):
        question = "look under /features for text 4250"
        matches = [
            Hit(index, "hmcts/hwf-publicapp", f"features/fee_{index}.feature", 1, 2, None, "scenario", "4250", 1.0)
            for index in (1, 2, 3)
        ]

        def fake_literal(*_args, **_kwargs):
            return matches[:2], 0.01, True

        with patch("rag.search.literal_hits", side_effect=fake_literal):
            found, _queries, truncated = retrieve(None, [1], question, 2, Meter())
        self.assertTrue(truncated)
        self.assertEqual(len(found), 2)

    def test_a_lookup_logs_the_passages_it_kept(self):
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
        with (
            patch("rag.search.literal_hits", return_value=([hit], 0.01)),
            self.assertLogs("rag", level="INFO") as caught,
        ):
            retrieve(None, [1], question, 8, Meter())
        text = "\n".join(caught.output)
        self.assertIn("path=values", text)
        self.assertIn("scopes=app", text)
        self.assertIn(question, text)
        self.assertIn("savings_and_investment_extra.rb:23-27", text)
        self.assertIn("return if amount < 4250", text)

    def test_a_semantic_search_logs_a_dropped_directory_query(self):
        question = "look under /features for birth date"
        hit = Hit(
            1, "hmcts/hwf-publicapp", "features/dob/dob_single.feature",
            7, 9, "Date of birth", "scenario", "Date of birth", 0.75,
        )
        with (
            patch("rag.plan.search_queries", return_value=[question, "/features"]),
            patch("rag.search.search", return_value=([hit], False)),
            self.assertLogs("rag", level="INFO") as caught,
        ):
            retrieve(None, [1], question, 8, Meter())
        text = "\n".join(caught.output)
        self.assertIn("path=semantic", text)
        self.assertIn("dropped=/features", text)
        self.assertIn("dob_single.feature:7-9", text)
        self.assertNotIn("queries=/features|", text)

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

        def fake_literal(_conn, _repos, scopes, literals, _k, _excluded=None):
            seen["scopes"] = scopes
            seen["literals"] = literals
            return [feature_hit, step_hit], 0.01

        def fail_if_planned(*_args, **_kwargs):
            raise AssertionError("planner should not run")

        meter = Meter()
        with (
            patch("rag.search.literal_hits", side_effect=fake_literal),
            patch("rag.follow.follow_matched_steps", return_value=[]),
            patch("rag.plan.search_queries", side_effect=fail_if_planned),
            patch("rag.plan.retrieval_model", return_value="gpt-4o-mini"),
        ):
            self.assertFalse(question_needs_plan(question))
            hits, queries, truncated = retrieve(None, [1], question, 8, meter)
        self.assertFalse(truncated)
        self.assertEqual(seen["scopes"], ["features"])
        self.assertEqual(seen["literals"], [phrase])
        self.assertEqual(hits, [feature_hit, step_hit])
        self.assertEqual(queries, [question])
        plan = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Plan the question")
        self.assertEqual(plan["note"], "Looked for that text under the named directory.")

    def test_a_quoted_regex_is_the_postgres_pattern_and_does_not_fall_back(self):
        question = '''look under /features for "with income '[0-9]*' and average"'''
        phrase = "with income '[0-9]*' and average"
        pattern = postgres_pattern([phrase])
        self.assertEqual(pattern, "(?:with income '[0-9]*' and average)")
        feature = "When I submit the form with income '8000' and average 'average'"
        step = "When(/^I submit the form with income '([^\"']*)' and average '([^\"']*)'$/) do |num, income_period|"
        monthly = "When I submit the form with income '1000' and monthly"
        self.assertRegex(feature, "(?i)" + pattern)
        self.assertNotRegex(step, "(?i)" + pattern)
        self.assertNotRegex(monthly, "(?i)" + pattern)

        def empty(*_args, **_kwargs):
            return [], 0.02

        def fail_if_planned(*_args, **_kwargs):
            raise AssertionError("general search should not run")

        meter = Meter()
        with (
            patch("rag.search.literal_hits", side_effect=empty),
            patch("rag.plan.search_queries", side_effect=fail_if_planned),
        ):
            hits, queries, truncated = retrieve(None, [1], question, 8, meter)
        self.assertEqual(hits, [])
        self.assertEqual(queries, [question])
        self.assertFalse(truncated)
        plan = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Plan the question")
        self.assertEqual(plan["note"], "Looked for that regular expression under the named directory.")
        embed = next(stage for stage in meter.as_dict()["stages"] if stage["name"] == "Embed the searches")
        self.assertEqual(
            embed["note"],
            "The regular expression was matched directly, so nothing was embedded.",
        )

    def test_postgres_rejects_a_regular_expression_as_a_message(self):
        import psycopg

        from rag.search import literal_hits

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args, **_kwargs):
                raise psycopg.errors.InvalidRegularExpression("invalid")

        class Connection:
            def cursor(self):
                return Cursor()

            def rollback(self):
                self.rolled_back = True

        connection = Connection()
        with self.assertRaises(ValueError) as caught:
            literal_hits(connection, [1], ["features"], ["with income '(?P<n>[0-9]+)'"], 8)
        self.assertEqual(str(caught.exception), "That regular expression is not valid in the index.")
        self.assertTrue(connection.rolled_back)

    def test_a_quoted_search_keeps_the_step_that_runs_the_feature_line(self):
        question = '''look under /features for "with income '[0-9]*' and average"'''
        feature = Hit(
            1,
            "hmcts/hwf-publicapp",
            "features/e2e.feature",
            160,
            161,
            None,
            "scenario",
            "When I submit the form with income '8000' and average 'average'",
            1.0,
        )
        step = Hit(
            2,
            "hmcts/hwf-publicapp",
            "features/step_definitions/income_period_steps.rb",
            9,
            11,
            None,
            "file",
            "When(/^I submit the form with income '([^\"']*)' and average '([^\"']*)'$/)",
            1.0,
        )

        def fake_literal(*_args, **_kwargs):
            return [feature], 0.01

        def fake_follow(_conn, _repos, asked, hits):
            self.assertEqual(asked, question)
            self.assertEqual(hits, [feature])
            return [step]

        def fail_if_planned(*_args, **_kwargs):
            raise AssertionError("general search should not run")

        with (
            patch("rag.search.literal_hits", side_effect=fake_literal),
            patch("rag.follow.follow_matched_steps", side_effect=fake_follow),
            patch("rag.plan.search_queries", side_effect=fail_if_planned),
        ):
            hits, _queries, truncated = retrieve(None, [1], question, 8, Meter())
        self.assertFalse(truncated)
        self.assertEqual([hit.path for hit in hits], [feature.path, step.path])

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
        weaker_first = Hit(3, "hmcts/hwf-staffapp", "features/approve.feature", 1, 1, None, "scenario", "Ask a manager page", 0.2)
        stronger_second = Hit(4, "hmcts/hwf-publicapp", "features/dob/dob_single.feature", 7, 9, None, "scenario", "Date of birth", 0.9)
        ordered = merge_hits([[weaker_first], [stronger_second]], 2)
        self.assertEqual([hit.chunk_id for hit in ordered], [4, 3])

    def test_claude_plans_with_the_retrieval_model_when_the_key_is_set(self):
        with (
            patch("rag.plan.LLM_PROVIDER", "anthropic"),
            patch("rag.plan.ANTHROPIC_API_KEY", "sk-ant"),
            patch("rag.plan.ANTHROPIC_RETRIEVAL_MODEL", "claude-haiku-5-5"),
        ):
            self.assertEqual(retrieval_model(), "claude-haiku-5-5")

    def test_claude_does_not_plan_without_a_key(self):
        with (
            patch("rag.plan.LLM_PROVIDER", "anthropic"),
            patch("rag.plan.ANTHROPIC_API_KEY", ""),
        ):
            self.assertIsNone(retrieval_model())


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

    def test_claude_is_configured_when_the_key_is_set(self):
        with (
            patch("rag.answer.LLM_PROVIDER", "anthropic"),
            patch("rag.answer.ANTHROPIC_API_KEY", "sk-ant"),
            patch("rag.answer.ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        ):
            self.assertTrue(language_model_configured())
            self.assertEqual(active_model(), "claude-sonnet-5-5")
            self.assertEqual(active_model("claude-haiku-5-5"), "claude-haiku-5-5")

    def test_claude_is_not_configured_without_a_key(self):
        with (
            patch("rag.answer.LLM_PROVIDER", "anthropic"),
            patch("rag.answer.ANTHROPIC_API_KEY", ""),
        ):
            self.assertFalse(language_model_configured())

    def test_an_unknown_provider_is_rejected(self):
        with patch("rag.answer.LLM_PROVIDER", "bedrock"):
            with self.assertRaises(ValueError) as caught:
                language_model_configured()
        self.assertIn("anthropic", str(caught.exception))

    def test_anthropic_payload_puts_the_system_prompt_beside_the_user_message(self):
        payload = anthropic_payload(
            "claude-sonnet-5-5",
            [
                {"role": "system", "content": "Cite the passages."},
                {"role": "user", "content": "Where is it saved?"},
            ],
            max_tokens=8192,
        )
        self.assertEqual(payload["model"], "claude-sonnet-5-5")
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["system"], "Cite the passages.")
        self.assertEqual(
            payload["messages"],
            [{"role": "user", "content": "Where is it saved?"}],
        )
        self.assertNotIn("temperature", payload)

    def test_a_copied_v1_suffix_is_removed_from_the_claude_host(self):
        with patch("rag.answer.ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"):
            self.assertEqual(anthropic_root(), "https://api.anthropic.com")

    def test_anthropic_text_keeps_the_reply_and_drops_the_thinking(self):
        text = anthropic_text({
            "content": [
                {"type": "thinking", "thinking": "walk the callers"},
                {"type": "text", "text": "<think>again</think>\nDefined in application_controller.rb:44."},
            ],
        })
        self.assertEqual(text, "Defined in application_controller.rb:44.")

    def test_anthropic_text_rejects_a_thinking_trace_with_no_reply(self):
        with self.assertRaises(AnswerError) as caught:
            anthropic_text({"content": [{"type": "thinking", "thinking": "only this"}]})
        self.assertEqual(str(caught.exception), "Claude returned no answer text.")


class HistoryTest(unittest.TestCase):
    def test_versions_run_from_older_to_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            older = _commit_file(root, "app/helpers/application_helper.rb", b"def older\n", "Older helper")
            newer = _commit_file(root, "app/helpers/application_helper.rb", b"def newer\n", "Newer helper")
            found = file_history(
                "hmcts/hwf-publicapp",
                "master",
                "app/helpers/application_helper.rb",
                older,
                root=root,
            )
        self.assertEqual([item["sha"] for item in found["versions"]], [older, newer])
        self.assertEqual(found["versions"][0]["text"], "def older\n")
        self.assertEqual(found["versions"][0]["author"], "Ada")
        self.assertTrue(found["versions"][0]["date"])
        self.assertTrue(found["versions"][0]["indexed"])
        self.assertFalse(found["versions"][1]["indexed"])
        self.assertFalse(found["truncated"])
        self.assertEqual(found["path"], "app/helpers/application_helper.rb")
        self.assertIn(older, found["versions"][0]["url"])

    def test_a_deleted_version_is_marked_and_a_blob_url_is_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            _commit_file(root, "app/helpers/application_helper.rb", b"keep\n", "Add the helper")
            _delete_file(root, "app/helpers/application_helper.rb", "Remove the helper")
            found = file_history(
                "hmcts/hwf-publicapp",
                "master",
                "https://github.com/hmcts/hwf-publicapp/blob/master/app/helpers/application_helper.rb#L4",
                root=root,
            )
        self.assertEqual(found["path"], "app/helpers/application_helper.rb")
        self.assertEqual(found["versions"][0]["text"], "keep\n")
        self.assertEqual(found["versions"][1]["omitted"], "deleted")
        self.assertIsNone(found["versions"][1]["text"])

    def test_a_binary_or_huge_file_is_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            binary = _commit_file(root, "app/helper.rb", b"\0not text", "Binary")
            huge = _commit_file(root, "app/helper.rb", b"x" * 60_001, "Huge")
            found = file_history("hmcts/hwf-publicapp", "master", "app/helper.rb", root=root)
        by_sha = {item["sha"]: item["omitted"] for item in found["versions"]}
        self.assertEqual(by_sha[binary], "binary")
        self.assertEqual(by_sha[huge], "too large")

    def test_a_shallow_clone_fetches_older_commits_from_its_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work = base / "work"
            _git_init(work)
            for number in range(4):
                _commit_file(work, "README", f"{number}\n".encode(), f"readme {number}")
            _commit_file(work, "app/helper.rb", b"one\n", "Add helper")
            _commit_file(work, "app/helper.rb", b"two\n", "Edit helper")
            remote = base / "remote"
            shallow = base / "shallow"
            subprocess.run(["git", "clone", "--bare", str(work), str(remote)], check=True, capture_output=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", "master", str(remote), str(shallow)],
                check=True,
                capture_output=True,
            )
            found = file_history("hmcts/hwf-publicapp", "master", "app/helper.rb", root=shallow)
        self.assertEqual([item["text"] for item in found["versions"]], ["one\n", "two\n"])
        self.assertFalse(found["truncated"])

    def test_only_the_newest_window_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            for number in range(13):
                _commit_file(root, "app/fee.rb", f"{number}\n".encode(), f"change {number}")
            found = file_history("hmcts/hwf-publicapp", "master", "app/fee.rb", root=root)
        self.assertTrue(found["truncated"])
        self.assertEqual(len(found["versions"]), 12)
        self.assertEqual(found["versions"][0]["text"], "1\n")
        self.assertEqual(found["versions"][-1]["text"], "12\n")

    def test_a_block_keeps_only_the_commits_that_change_it(self):
        helper = "app/helpers/application_helper.rb"
        original = (
            "module ApplicationHelper\n"
            "  def ni_number_value(summary)\n"
            "    summary\n"
            "  end\n"
            "end\n"
        )
        added = (
            "module ApplicationHelper\n"
            "  def ni_number_value(summary)\n"
            "    summary\n"
            "  end\n"
            "\n"
            "  def address_lookup_access_token\n"
            "    Rails.cache.read('address_lookup')\n"
            "  end\n"
            "end\n"
        )
        other = added.replace("summary", "summary.to_s")
        edited = added.replace(
            "Rails.cache.read('address_lookup')",
            "Rails.cache.read('address_lookup_v2')",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            _commit_file(root, helper, original.encode(), "Add the helper")
            introduced = _commit_file(root, helper, added.encode(), "Add address lookup")
            _commit_file(root, helper, other.encode(), "Change the ni number helper")
            revised = _commit_file(root, helper, edited.encode(), "Rename the cache key")
            found = file_history(
                "hmcts/hwf-publicapp", "master", helper,
                block="address_lookup_access_token", root=root,
            )
        self.assertEqual([item["sha"] for item in found["versions"]], [introduced, revised])
        self.assertEqual(found["symbol"], "ApplicationHelper#address_lookup_access_token")
        self.assertEqual(found["block"], "address_lookup_access_token")
        self.assertIn("def address_lookup_access_token", found["versions"][0]["text"])
        self.assertIn("address_lookup_v2", found["versions"][1]["text"])
        self.assertNotIn("ni_number_value", found["versions"][0]["text"])
        self.assertNotIn("\nend\nend\n", found["versions"][0]["text"])
        self.assertEqual(found["versions"][0]["start_line"], 6)
        self.assertFalse(found["truncated"])

    def test_a_block_name_must_pick_one_declaration(self):
        source = "module A\n  def save\n  end\nend\nmodule B\n  def save\n  end\nend\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            _commit_file(root, "app/helper.rb", source.encode(), "Two saves")
            with self.assertRaises(ValueError) as caught:
                file_history(
                    "hmcts/hwf-publicapp", "master", "app/helper.rb",
                    block="save", root=root,
                )
        self.assertIn("A#save", str(caught.exception))
        self.assertIn("B#save", str(caught.exception))
        blocks = find_blocks("ruby", source, "A#save")
        self.assertEqual([item.symbol for item in blocks], ["A#save"])

    def test_an_unknown_block_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            _commit_file(root, "app/helper.rb", b"def other\nend\n", "Add other")
            with self.assertRaises(HistoryError) as caught:
                file_history(
                    "hmcts/hwf-publicapp", "master", "app/helper.rb",
                    block="missing_method", root=root,
                )
        self.assertIn("missing_method", str(caught.exception))

    def test_a_path_cannot_leave_the_repository(self):
        with self.assertRaises(ValueError):
            file_history("hmcts/hwf-publicapp", "master", "../secrets")
        with self.assertRaises(ValueError):
            file_history("not a repo", "master", "app/fee.rb")

    def test_a_missing_repository_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HistoryError) as caught:
                file_history("hmcts/hwf-publicapp", "master", "app/fee.rb", root=Path(tmp))
        self.assertIn("not available", str(caught.exception))

    def test_history_needs_one_repository(self):
        with self.assertRaises(ValueError):
            run_history({"path": "app/fee.rb"})

    @patch("rag.web.file_history", side_effect=HistoryError("The file history could not be read."))
    @patch("rag.web.resolve_repositories")
    @patch("rag.web.connect")
    def test_a_github_failure_is_reported(self, connect, resolve, _history):
        connect.return_value.close = lambda: None
        resolve.return_value = [{
            "name": "hmcts/hwf-publicapp",
            "branch": "master",
            "commit_sha": "b3063dc39ed1",
        }]
        status, payload = run_history({
            "repo": "hmcts/hwf-publicapp",
            "path": "app/helpers/application_helper.rb",
        })
        self.assertEqual(status, 502)
        self.assertIn("history", payload["error"])


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "init", "-b", "master", str(path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "checkout", "-b", "master"], check=True, capture_output=True)


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
    }


def _commit_file(root: Path, path: str, content: bytes, message: str) -> str:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    subprocess.run(
        ["git", "-C", str(root), "-c", "core.autocrlf=false", "add", "--", path],
        check=True,
        capture_output=True,
        env=_git_env(),
    )
    subprocess.run(
        ["git", "-C", str(root), "-c", "core.autocrlf=false", "commit", "-m", message],
        check=True,
        capture_output=True,
        env=_git_env(),
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _delete_file(root: Path, path: str, message: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "rm", "--", path],
        check=True,
        capture_output=True,
        env=_git_env(),
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message],
        check=True,
        capture_output=True,
        env=_git_env(),
    )


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
        with self.assertRaises(ValueError):
            run_features({"question": "  "})
        with self.assertRaises(ValueError):
            run_testing({"question": "  "})

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
            self.assertIn('role="tablist"', page)
            self.assertIn('role="tab"', page)
            self.assertIn('data-mode="flow"', page)
            self.assertIn(">Flow</button>", page)
            self.assertIn('data-mode="features"', page)
            self.assertIn(">Features</button>", page)
            self.assertIn('data-mode="testing"', page)
            self.assertIn(">Testing</button>", page)
            self.assertIn('data-mode="history"', page)
            self.assertIn(">History</button>", page)
            self.assertIn("function renderHistory", page)
            self.assertIn("app/helpers/application_helper.rb", page)
            self.assertIn("Step backward and Step forward move through the changes", page)
            self.assertIn('placeholder="address_lookup_access_token"', page)
            self.assertIn("only the versions that change it", page)
            self.assertIn('older.textContent = "Step backward"', page)
            self.assertIn('newer.textContent = "Step forward"', page)
            self.assertIn("quotes only steps already in the suite", page)
            self.assertIn("RSpec, Minitest, and the question planner are not used.", page)
            self.assertIn("Cucumber under features/ only.", page)
            self.assertIn("Existing steps only.", page)
            self.assertIn("function renderFlow", page)
            self.assertIn("function drawFlow", page)
            self.assertIn('toolButton("Zoom in"', page)
            self.assertIn('toolButton("Fit"', page)
            self.assertIn("Hold Ctrl and scroll to zoom", page)
            self.assertIn("Each box opens that step", page)
            self.assertIn('setAttribute("target", "_blank")', page)
            self.assertIn("Resources", page)
            self.assertIn("renderUsage", page)
            self.assertIn("renderUsage(body.usage)", page)
            self.assertIn("renderUsage(body.usage, result)", page)
            self.assertIn("panels[name].hidden = name !== next", page)
            self.assertIn("panel.replaceChildren()", page)
            self.assertNotIn("results.replaceChildren()", page)
            self.assertIn('id="process"', page)
            self.assertIn("This process started", page)
            self.assertIn("[0-9]* matches any amount", page)
            self.assertIn("with income '[0-9]*' and average", page)
            self.assertIn('heading.textContent = "Matches"', page)
            self.assertIn("More matches were found than the Passages limit of ", page)
            self.assertIn("more callers were left out of References.", page)
            self.assertIn("Relationships", page)
            self.assertIn("Call relationships", page)
            self.assertIn("Arrows point from a caller toward the definition.", page)
            self.assertIn("A dashed arrow is a call by a name that is defined in more than one place.", page)
            self.assertIn("Callers in the same file are shown as one box.", page)
            self.assertIn('id="stale-update"', page)
            self.assertIn('fetch("/api/freshness")', page)
            # The written answer is built from elements, not set as HTML.
            self.assertIn("answer.append(renderMarkdown(body.answer));", page)
            self.assertNotIn("innerHTML", page)
            self.assertIn('"Collapse all" : "Expand all"', page)
            self.assertIn("Possible caller.", page)
            self.assertIn('id="index-toggle"', page)
            self.assertIn('toggle.textContent = open ? "Close"', page)
            self.assertIn('id="index-branch"', page)
            self.assertIn("Enter a repository", page)
            self.assertIn("Reading branches", page)
            self.assertIn("Save as HTML", page)
            self.assertIn("text/html", page)
            self.assertIn('id="diagram-data"', page)
            self.assertIn("let output = document.querySelector('#results');", page)
            self.assertIn("Remove this index", page)
            self.assertIn('id="search-index"', page)
            self.assertIn('value="code" selected', page)
            self.assertIn('value="both"', page)
            self.assertIn('id="fusion"', page)
            self.assertIn('value="rrf" selected', page)
            self.assertIn('value="12"', page)
            self.assertIn("pairFusionWithIndex", page)
            self.assertIn('body.routed === "impact"', page)
            self.assertIn("Index chooses the text model, the code model, or both.", page)
            self.assertIn("This deletes that index and its checkout.", page)
            self.assertIn("remove-confirm", page)
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

    def test_an_invalid_pattern_is_a_message(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        message = "That regular expression is not valid in the index."

        def reject(*_args, **_kwargs):
            raise ValueError(message)

        class _Connection:
            def close(self):
                return None

        repo = {"id": 1, "name": "hmcts/hwf-publicapp", "branch": "master", "commit_sha": "b1d15f83c0a9"}
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/search",
                data=json.dumps({"question": "look for \"with income '(?P<n>[0-9]+)'\""}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with (
                patch("rag.web.connect", return_value=_Connection()),
                patch("rag.web.resolve_repositories", return_value=[repo]),
                patch("rag.web.retrieve", side_effect=reject),
            ):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 400)
            body = json.loads(caught.exception.read().decode("utf-8"))
            caught.exception.close()
            self.assertEqual(body["error"], message)
        finally:
            server.shutdown()
            server.server_close()

    def test_status_names_when_the_process_started(self):
        class Connection:
            def close(self):
                pass

        with (
            patch("rag.web.connect", return_value=Connection()),
            patch("rag.web._repository_rows", return_value=[]),
            patch("rag.web.ingest_lock_held", return_value=False),
        ):
            status, body = public_status()
        self.assertEqual(status, 200)
        self.assertRegex(body["started"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
        self.assertFalse(body["ingest"]["elsewhere"])

    def test_an_ingest_in_another_process_blocks_a_new_one(self):
        from rag import web

        class Connection:
            def close(self):
                pass

        with (
            patch("rag.web.connect", return_value=Connection()),
            patch("rag.web._repository_rows", return_value=[]),
            patch("rag.web.ingest_lock_held", return_value=True),
        ):
            _status, body = public_status()
            status, refused = web.start_ingest({"repo": "hmcts/hwf-publicapp"})
        self.assertTrue(body["ingest"]["elsewhere"])
        self.assertFalse(body["ingest"]["running"])
        self.assertEqual(status, 409)
        self.assertEqual(refused["error"], "Another process is building an index")
        self.assertFalse(web._ingest_state["running"])

    def test_the_schema_is_set_up_once_per_process(self):
        import rag.db as db

        calls = []
        with (
            patch.object(db, "_schema_ready", False),
            patch("rag.db.ensure_schema", side_effect=lambda conn: calls.append("schema")),
            patch("rag.db._prefer_iterative_scan", side_effect=lambda conn: calls.append("scan")),
        ):
            for _ in range(3):
                db._ensure_schema_once(object())
        self.assertEqual(calls, ["schema", "scan", "scan"])

    def test_an_index_behind_its_branch_is_reported_as_stale(self):
        import rag.web as web

        class _Connection:
            def close(self):
                pass

        old = "a" * 40
        new = "b" * 40
        rows = [
            {"name": "hmcts/hwf-publicapp", "branch": "master", "commit_sha": old},
            {"name": "hmcts/hwf-staffapp", "branch": "master", "commit_sha": new},
            {"name": "hmcts/et-full-system", "branch": "develop", "commit_sha": old},
        ]
        asked: list[tuple[str, str]] = []

        def remote(url, branch, timeout=None):
            asked.append((url, branch))
            return None if "et-full-system" in url else new

        web._freshness.clear()
        with (
            patch("rag.web.connect", return_value=_Connection()),
            patch("rag.web.list_repositories", return_value=rows),
            patch("rag.web.remote_commit", side_effect=remote),
        ):
            status, body = web.index_freshness()
            self.assertEqual(status, 200)
            found = {row["name"]: row for row in body["repositories"]}
            self.assertEqual(
                found["hmcts/hwf-publicapp"],
                {
                    "name": "hmcts/hwf-publicapp", "branch": "master",
                    "indexed": "a" * 12, "remote": "b" * 12, "stale": True,
                },
            )
            self.assertFalse(found["hmcts/hwf-staffapp"]["stale"])
            # A branch that could not be read is unknown, not stale.
            self.assertIsNone(found["hmcts/et-full-system"]["remote"])
            self.assertFalse(found["hmcts/et-full-system"]["stale"])
            self.assertEqual(len(asked), 3)
            # The answer is reused. Only the failed lookup is asked again.
            web.index_freshness()
            self.assertEqual(len(asked), 4)
            web.index_freshness(refresh=True)
            self.assertEqual(len(asked), 7)
        web._freshness.clear()

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

        def fake_retrieve(_conn, _repository_ids, question, _k, meter=None, **_options):
            if meter is not None:
                meter.add_search(0.0, 0.01)
                meter.finish_retrieval(1, embedded=False)
            return [hit], [question], False

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
            self.assertEqual(body["matches"], [])
            self.assertFalse(body["truncated"])
            from rag.config import DEFAULT_K
            self.assertEqual(body["passage_limit"], DEFAULT_K)
            self.assertTrue(body["usage"]["stages"])
            self.assertIn(
                "The values were matched directly, so nothing was embedded.",
                [stage["note"] for stage in body["usage"]["stages"]],
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_impact_response_includes_usage_stages(self):
        found = {
            "definitions": [],
            "references": [],
            "related": [],
            "gaps": [],
            "definition_count": 2,
            "reference_count": 3,
            "truncated": False,
            "callers_omitted": 0,
            "callers_capped": False,
            "possible_count": 0,
            "diagram": {"nodes": [], "edges": []},
        }
        repo = {
            "id": 1,
            "name": "hmcts/hwf-publicapp",
            "branch": "master",
            "commit_sha": "b1d15f83c0a9",
        }

        class _Connection:
            def close(self):
                return None

        with (
            patch("rag.web.connect", return_value=_Connection()),
            patch("rag.web.resolve_repositories", return_value=[repo]),
            patch("rag.web.impact", return_value=found),
            patch("rag.web.language_model_configured", return_value=False),
        ):
            status, body = run_impact({"target": "OnlineApplication", "answer": False})
        self.assertEqual(status, 200)
        names = [stage["name"] for stage in body["usage"]["stages"]]
        self.assertEqual(names, ["Plan the question", "Embed the searches", "Search the index"])
        embed = next(stage for stage in body["usage"]["stages"] if stage["name"] == "Embed the searches")
        self.assertIsNone(embed["total_tokens"])
        self.assertIn("looked up in the index", embed["note"])
        self.assertEqual(body["usage"]["total_tokens"], 0)
        search = next(stage for stage in body["usage"]["stages"] if stage["name"] == "Search the index")
        self.assertIn("5 passages kept", search["note"])


class IndexedRetrievalTest(unittest.TestCase):
    def test_a_search_for_limit_includes_a_threshold(self):
        """Looking for limit stays close to the savings threshold passages."""
        repo = Path(__file__).resolve().parents[1]
        script = """
import json
from rag.db import connect
from rag.search import search

question = "look for limit."
conn = connect()
try:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM repositories ORDER BY id")
        repositories = [row[0] for row in cur.fetchall()]
    if not repositories:
        raise SystemExit("no repositories")
    hits, _truncated = search(conn, repositories, question, 8)
finally:
    conn.close()
print("HITS")
print(json.dumps([
    {"path": hit.path, "symbol": hit.symbol or "", "content": hit.content}
    for hit in hits
]))
"""
        try:
            container = subprocess.run(
                ["docker", "compose", "ps", "-q", "rag"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            ).stdout.split()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.skipTest("The local index is not running")
        if not container:
            self.skipTest("The local index is not running")
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", "-w", "/app", "-e", "PYTHONPATH=/app", container[0], "python", "-"],
                input=script,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            self.fail("The search for limit did not finish")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        marker = "HITS\n"
        self.assertIn(marker, result.stdout, result.stdout)
        hits = json.loads(result.stdout.split(marker, 1)[1])
        text = "\n".join(f"{hit['symbol']}\n{hit['content']}" for hit in hits).lower()
        self.assertIn(
            "threshold",
            text,
            "A search for limit should keep a passage that mentions a threshold. Got: "
            + ", ".join(hit["path"] for hit in hits),
        )


class RemoveIndexTest(unittest.TestCase):
    def test_forgetting_a_repository_deletes_only_that_index(self):
        from rag.ingest import forget_repository

        calls = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params):
                calls.append((" ".join(sql.split()), params))

            def fetchone(self):
                return (4, 12)

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                self.committed = True

        connection = Connection()
        stored = {
            "id": 7,
            "name": "hmcts/hwf-publicapp",
            "url": "https://github.com/hmcts/hwf-publicapp.git",
            "branch": "master",
            "commit_sha": "b1d15f83c0a9",
            "ingested_at": None,
        }
        with patch("rag.ingest.get_repository", return_value=stored):
            removed = forget_repository(connection, "hmcts/hwf-publicapp")
        deleted = next(call for call in calls if call[0].startswith("DELETE"))
        self.assertEqual(deleted, ("DELETE FROM repositories WHERE id = %s", (7,)))
        self.assertEqual(removed["files"], 4)
        self.assertEqual(removed["chunks"], 12)
        self.assertTrue(connection.committed)

    def test_an_unknown_repository_is_not_deleted(self):
        from rag.ingest import forget_repository

        class Connection:
            def cursor(self):
                raise AssertionError("no delete")

        with patch("rag.ingest.get_repository", return_value=None):
            with self.assertRaises(ValueError) as caught:
                forget_repository(Connection(), "hmcts/missing")
        self.assertEqual(str(caught.exception), "hmcts/missing is not indexed")

    def test_removing_a_checkout_stays_inside_the_repos_directory(self):
        from rag.gitrepo import remove_checkout

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "hmcts" / "hwf-publicapp"
            checkout.mkdir(parents=True)
            (checkout / "README.md").write_text("indexed", encoding="utf-8")
            with patch("rag.gitrepo.REPOS_DIR", root):
                self.assertTrue(remove_checkout("hmcts/hwf-publicapp"))
            self.assertFalse(checkout.exists())
            self.assertFalse((root / "hmcts").exists())

    def test_remove_refuses_while_an_ingest_is_running(self):
        from rag import web

        web._ingest_state["running"] = True
        try:
            status, body = web.remove_index({"repo": "hmcts/hwf-publicapp"})
        finally:
            web._ingest_state["running"] = False
            web._ingest_state["repo"] = None
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "An ingest is already running")


class IntentAndRankingTest(unittest.TestCase):
    def test_impact_target_extracts_caller_questions(self):
        self.assertEqual(
            impact_target("What calls SavingsPassFailService, and what is affected if I change it?"),
            "SavingsPassFailService",
        )
        self.assertEqual(
            impact_target(
                "Which step definitions and helpers call the DependentPage method submit_child_bands?"
            ),
            "DependentPage#submit_child_bands",
        )
        self.assertEqual(
            impact_target("What calls et3_answer_saving_response, and what breaks if I change it?"),
            "et3_answer_saving_response",
        )
        self.assertIsNone(
            impact_target(
                "Where is HelpWithFeesReferenceValidator with HWF_REFERENCE_REGEX defined, "
                "and which form uses help_with_fees_reference: true on hwf_reference_number?"
            )
        )
        self.assertIsNone(impact_target("How are fees calculated for a single applicant?"))

    def test_locale_paths_are_demoted_and_logic_paths_preferred(self):
        self.assertTrue(_is_locale_or_messaging("config/locales/en.yml"))
        self.assertTrue(_is_locale_or_messaging("features/support/messaging/et1_en.yml"))
        self.assertFalse(_is_locale_or_messaging("config/settings.yml"))
        self.assertEqual(_query_path_rank("app/services/fee.rb")[1], 0)
        self.assertEqual(_query_path_rank("config/locales/en.yml")[0], 1)
        locale = _hit("config/locales/en.yml", None, "fee text", 0.9)
        service = _hit("app/services/fee.rb", "FeeService", "def fee\nend", 0.5)
        ranked = _rerank_query_hits([locale, service])
        self.assertEqual([hit.path for hit in ranked], ["app/services/fee.rb", "config/locales/en.yml"])
        same_file = [
            _hit("app/models/a.rb", "A", "one", 0.9),
            _hit("app/models/a.rb", "A#b", "two", 0.8),
            _hit("app/models/b.rb", "B", "three", 0.7),
        ]
        self.assertEqual(
            [hit.symbol for hit in _each_path_first(same_file)],
            ["A", "B", "A#b"],
        )

    def test_hits_from_impact_prefer_references(self):
        definition = Hit(1, "repo", "app/services/x.rb", 1, 2, "X", "class", "class X\nend", 0.0)
        caller = Hit(2, "repo", "app/controllers/y.rb", 3, 4, "Y#go", "method", "X.new", 0.0)
        related = Hit(3, "repo", "README.md", 1, 2, None, "file", "mentions X", 0.0)
        hits = _hits_from_impact(
            {"references": [caller], "definitions": [definition], "related": [related]},
            k=2,
        )
        self.assertEqual([hit.path for hit in hits], [caller.path, definition.path])

    def test_retrieve_routes_impact_questions(self):
        seen: dict = {}

        def fake_impact(_conn, _repos, target):
            seen["target"] = target
            return {
                "references": [Hit(10, "repo", "app/controllers/a.rb", 1, 2, "A#go", "method", "call", 0.0)],
                "definitions": [Hit(11, "repo", "app/services/b.rb", 1, 2, "B", "class", "class B", 0.0)],
                "related": [],
                "truncated": False,
                "callers_omitted": 0,
            }

        with (
            patch("rag.search.impact", side_effect=fake_impact),
            patch("rag.search.log_retrieval"),
        ):
            hits, queries, truncated = retrieve(
                None,
                [1],
                "What is affected if I change SavingsPassFailService?",
                8,
            )
        self.assertEqual(seen["target"], "SavingsPassFailService")
        self.assertEqual(queries, ["SavingsPassFailService"])
        self.assertEqual(hits[0].path, "app/controllers/a.rb")
        self.assertFalse(truncated)

    def test_run_query_returns_an_impact_payload_when_routed(self):
        found = {
            "definitions": [],
            "references": [],
            "related": [],
            "gaps": [],
            "definition_count": 0,
            "reference_count": 0,
            "truncated": False,
            "callers_omitted": 0,
            "callers_capped": False,
            "possible_count": 0,
            "diagram": {"nodes": [], "edges": []},
        }
        with patch("rag.web.run_impact", return_value=(200, {**found, "answer": None, "answer_error": None, "model": {}, "repositories": []})) as impact_call:
            status, body = run_query({
                "question": "What calls ResolverService#return?",
                "answer": False,
            })
        self.assertEqual(status, 200)
        self.assertEqual(body["routed"], "impact")
        self.assertEqual(body["target"], "ResolverService#return")
        self.assertEqual(impact_call.call_args.args[0]["target"], "ResolverService#return")

    def test_definition_users_passes_empty_scopes_to_literal_hits(self):
        from rag.search import _definition_users

        definition = Hit(
            1, "repo", "features/support/page_objects/et1/case_heard_by_page.rb",
            5, 6, "CaseHeardByPage", "class", "class CaseHeardByPage < BasePage\nend\n", 0.9,
        )
        seen: dict = {}

        def fake_literal(_conn, _repos, scopes, literals, k, excluded=None):
            seen["scopes"] = scopes
            seen["excluded"] = excluded
            seen["literals"] = literals
            return [], 0.0, False

        with (
            patch("rag.search._callers_and_gaps", return_value=({}, [], 0, False)),
            patch("rag.search.literal_hits", side_effect=fake_literal),
        ):
            extra = _definition_users(None, [1], [definition])
        self.assertEqual(extra, [])
        self.assertEqual(seen["scopes"], [])
        self.assertEqual(seen["excluded"], [])
        self.assertEqual(seen["literals"], ["CaseHeardByPage"])


class DualIndexTest(unittest.TestCase):
    def test_reciprocal_rank_fusion_uses_one_over_k_plus_rank(self):
        from rag.fusion import combine_rankings, reciprocal_rank_fusion

        single = reciprocal_rank_fusion([[10, 20, 30]], k=60)
        self.assertEqual(
            single,
            [(10, 1 / 61), (20, 1 / 62), (30, 1 / 63)],
        )
        # A repeated id keeps its first rank and does not push the next id down.
        self.assertEqual(
            reciprocal_rank_fusion([[5, 5, 6]], k=60),
            [(5, 1 / 61), (6, 1 / 62)],
        )
        both_lists = reciprocal_rank_fusion([[1, 2], [1, 3]], k=60)
        self.assertEqual(both_lists[0][0], 1)
        self.assertAlmostEqual(both_lists[0][1], 1 / 61 + 1 / 61)
        self.assertEqual([item[0] for item in both_lists[1:]], [2, 3])
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion([[1]], k=-1)

        summed = combine_rankings(
            index="text",
            fusion="score",
            text=[(1, 0.8), (2, 0.2)],
            code=None,
            lexical=[(2, 0.5), (3, 0.1)],
            rrf_k=60,
        )
        self.assertEqual(summed, [(1, 0.8), (2, 0.7), (3, 0.1)])

        keyword_in_the_same_fusion = combine_rankings(
            index="text",
            fusion="rrf",
            text=[(1, 0.9), (2, 0.1)],
            code=None,
            lexical=[(2, 0.4)],
            rrf_k=60,
        )
        self.assertEqual(keyword_in_the_same_fusion[0][0], 2)
        self.assertAlmostEqual(keyword_in_the_same_fusion[0][1], 1 / 61 + 1 / 62)

        merged = combine_rankings(
            index="both",
            fusion="rrf",
            text=[(1, 0.4), (2, 0.3)],
            code=[(2, 0.5), (3, 0.2)],
            lexical=[(3, 0.9)],
            rrf_k=60,
        )
        self.assertEqual([item[0] for item in merged], [2, 3, 1])
        self.assertAlmostEqual(merged[0][1], 1 / 62 + 1 / 61)

        raw_after_the_vector_merge = combine_rankings(
            index="both",
            fusion="score",
            text=[(1, 0.4), (2, 0.3)],
            code=[(2, 0.5), (3, 0.2)],
            lexical=[(3, 0.5)],
            rrf_k=60,
        )
        self.assertEqual(raw_after_the_vector_merge[0][0], 3)
        self.assertGreater(raw_after_the_vector_merge[0][1], 0.5)

    def test_the_default_index_is_code_and_only_that_model_is_loaded(self):
        from rag.config import (
            CODE_EMBEDDING_DIM,
            CODE_EMBEDDING_MODEL,
            DEFAULT_FUSION,
            DEFAULT_INDEX,
            resolve_fusion,
            resolve_index,
        )

        self.assertEqual(DEFAULT_INDEX, "code")
        self.assertEqual(DEFAULT_FUSION, "rrf")
        self.assertEqual(resolve_index(None), "code")
        self.assertEqual(resolve_index(" TEXT "), "text")
        self.assertEqual(resolve_fusion(None), "rrf")
        with self.assertRaises(ValueError):
            resolve_index("graph")
        with self.assertRaises(ValueError):
            resolve_fusion("blend")

        constructed: list[tuple] = []
        seen: dict = {}

        class FakeEmbedder:
            def __init__(self, model_name=None, dim=None):
                constructed.append((model_name, dim))

            def embed_query(self, _question):
                return [0.0, 0.0, 0.0]

        def fake_search(_conn, _repos, _question, _k, embeddings, _scopes, _excluded, **options):
            seen["keys"] = set(embeddings)
            seen["index"] = options["index"]
            seen["fusion"] = options["fusion"]
            return [], False

        with (
            patch("rag.embedder.Embedder", FakeEmbedder),
            patch("rag.search._search_with_embedding", side_effect=fake_search),
        ):
            search(None, [1], "where is it saved", 4)
            self.assertEqual(constructed, [(CODE_EMBEDDING_MODEL, CODE_EMBEDDING_DIM)])
            self.assertEqual(seen, {"keys": {"code"}, "index": "code", "fusion": "rrf"})
            constructed.clear()
            search(None, [1], "where is it saved", 4, index="both", fusion="score")
        self.assertEqual(seen["index"], "both")
        self.assertEqual(seen["fusion"], "score")
        self.assertEqual(seen["keys"], {"text", "code"})
        self.assertEqual(
            constructed,
            [(None, None), (CODE_EMBEDDING_MODEL, CODE_EMBEDDING_DIM)],
        )

    def test_query_flags_reach_retrieval(self):
        seen: dict = {}

        class _Connection:
            def close(self):
                return None

        def fake_retrieve(_conn, _ids, question, k, meter=None, **options):
            seen.update(question=question, k=k, **options)
            return [], [question], False

        with (
            patch("rag.cli.connect", return_value=_Connection()),
            patch("rag.cli.resolve_repositories", return_value=[
                {"id": 1, "name": "hmcts/hwf-publicapp", "commit_sha": "b1d15f83c0a9", "branch": "master"},
            ]),
            patch("rag.cli.retrieve", side_effect=fake_retrieve),
        ):
            status = main([
                "query", "where is the fee saved",
                "--index", "code", "--fusion", "score", "--rrf-k", "10",
                "--no-answer", "-k", "3",
            ])
        self.assertEqual(status, 0)
        self.assertEqual(seen["question"], "where is the fee saved")
        self.assertEqual(seen["k"], 3)
        self.assertEqual(seen["index"], "code")
        self.assertEqual(seen["fusion"], "score")
        self.assertEqual(seen["rrf_k"], 10)

    def test_the_web_api_rejects_an_unknown_index_before_opening_the_database(self):
        with patch("rag.web.connect", side_effect=AssertionError("database")):
            with self.assertRaises(ValueError):
                run_query({"question": "where", "index": "graph"})
        seen: dict = {}

        def fake_retrieve(_conn, _ids, _question, _k, meter=None, **options):
            seen.update(options)
            return [], ["where"], False

        class _Connection:
            def close(self):
                return None

        with (
            patch("rag.web.connect", return_value=_Connection()),
            patch("rag.web.resolve_repositories", return_value=[
                {"id": 1, "name": "hmcts/hwf-publicapp", "branch": "master", "commit_sha": "b1d15f83c0a9"},
            ]),
            patch("rag.web.retrieve", side_effect=fake_retrieve),
        ):
            status, body = run_query({
                "question": "where is it",
                "answer": False,
                "index": "both",
                "fusion": "score",
            })
        self.assertEqual(status, 200)
        self.assertEqual(body["index"], "both")
        self.assertEqual(body["fusion"], "score")
        self.assertEqual(seen["index"], "both")
        self.assertEqual(seen["fusion"], "score")

    def test_migration_from_the_single_column_is_idempotent(self):
        from rag.db import CODE_INDEX, LEGACY_INDEX, TEXT_INDEX, ChunkSchema, migration_statements, settings_plan
        from rag.config import CODE_EMBEDDING_MODEL, TEXT_EMBEDDING_DIM, TEXT_EMBEDDING_MODEL

        legacy = ChunkSchema(
            columns={"id": "bigint", "embedding": f"vector({TEXT_EMBEDDING_DIM})"},
            indexes=frozenset({LEGACY_INDEX, "chunks_tsv_idx"}),
        )
        first = migration_statements(legacy)
        self.assertIn(
            "ALTER TABLE chunks RENAME COLUMN embedding TO embedding_text",
            first,
        )
        self.assertIn(f"ALTER INDEX {LEGACY_INDEX} RENAME TO {TEXT_INDEX}", first)
        self.assertTrue(any("embedding_code vector(768)" in statement for statement in first))
        migrated = _schema_after(legacy, first)
        second = migration_statements(migrated)
        third = migration_statements(migrated)
        self.assertEqual(second, third)
        self.assertFalse(any("RENAME" in statement for statement in second))
        for statement in second:
            self.assertIn("IF NOT EXISTS", statement)
        self.assertIn(TEXT_INDEX, migrated.indexes)
        self.assertIn(CODE_INDEX, migrated.indexes)
        self.assertNotIn("embedding", migrated.columns)

        code_only = ChunkSchema(
            columns={"embedding": "vector(768)"},
            indexes=frozenset({LEGACY_INDEX}),
        )
        renamed = migration_statements(code_only)
        self.assertIn("ALTER TABLE chunks RENAME COLUMN embedding TO embedding_code", renamed)
        self.assertIn(f"ALTER INDEX {LEGACY_INDEX} RENAME TO {CODE_INDEX}", renamed)
        self.assertNotIn("RENAME COLUMN embedding TO embedding_text", " ".join(renamed))

        with self.assertRaises(SystemExit):
            migration_statements(ChunkSchema(columns={"embedding": "vector(1024)"}, indexes=frozenset()))
        with self.assertRaises(SystemExit):
            migration_statements(ChunkSchema(
                columns={"embedding_text": "vector(768)", "embedding_code": "vector(768)"},
                indexes=frozenset(),
            ))

        fresh = _schema_after(
            ChunkSchema(
                columns={"embedding_text": "vector(384)", "embedding_code": "vector(768)"},
                indexes=frozenset({TEXT_INDEX, CODE_INDEX}),
            ),
            [],
        )
        repeat = migration_statements(fresh)
        self.assertEqual(repeat, migration_statements(fresh))
        self.assertFalse(any("RENAME" in statement for statement in repeat))

        init_sql = (Path(__file__).resolve().parents[1] / "db" / "init.sql").read_text(encoding="utf-8")
        self.assertIn("embedding_text vector(384)", init_sql)
        self.assertIn("embedding_code vector(768)", init_sql)
        self.assertIn(TEXT_INDEX, init_sql)
        self.assertIn(CODE_INDEX, init_sql)
        self.assertNotIn("embedding vector(", init_sql)

        self.assertEqual(
            settings_plan({}),
            {
                "embedding_text_model": TEXT_EMBEDDING_MODEL,
                "embedding_text_dim": str(TEXT_EMBEDDING_DIM),
                "embedding_code_model": CODE_EMBEDDING_MODEL,
                "embedding_code_dim": "768",
            },
        )
        self.assertEqual(
            settings_plan({"embedding_model": TEXT_EMBEDDING_MODEL, "embedding_dim": "384"}),
            settings_plan({}),
        )
        self.assertEqual(
            settings_plan({"embedding_model": CODE_EMBEDDING_MODEL, "embedding_dim": "768"}),
            settings_plan({}),
        )
        with self.assertRaises(SystemExit):
            settings_plan({"embedding_model": "other/model", "embedding_dim": "32"})
        stored = settings_plan({})
        self.assertEqual(settings_plan(stored), stored)
        with self.assertRaises(SystemExit):
            settings_plan({**stored, "embedding_text_model": "other/model"})

    def test_backfill_updates_a_missing_code_embedding_in_place(self):
        from rag.ingest import backfill_missing

        calls: list[tuple] = []
        state = {"rows": [(
            9,
            "app/rag/search.py",
            "python",
            "search",
            "def search():\n    return hits\n",
            False,
            True,
        )]}

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                compact = " ".join(sql.split())
                calls.append((compact, params))
                self._rowcount = 0
                if compact.startswith("SELECT"):
                    self._rows = state["rows"]
                    state["rows"] = []
                elif compact.startswith("UPDATE"):
                    self._rowcount = 1
                    self.assert_update(compact)

            def fetchall(self):
                return self._rows

            @property
            def rowcount(self):
                return self._rowcount

            @staticmethod
            def assert_update(sql: str) -> None:
                if "INSERT" in sql or "DELETE" in sql:
                    raise AssertionError(sql)

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                self.committed = True

        class FakeModel:
            def __init__(self, dim):
                self.dim = dim

            def embed_documents(self, texts):
                self.texts = texts
                return [[0.25] * self.dim for _text in texts]

        model = FakeModel(768)
        with (
            patch("rag.ingest.text_embedder", side_effect=AssertionError("text model")),
            patch("rag.ingest.code_embedder", return_value=model),
        ):
            filled = backfill_missing(Connection(), 3)
            again = backfill_missing(Connection(), 3)
        self.assertEqual(filled, 1)
        self.assertEqual(again, 0)
        updates = [call for call in calls if call[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn("embedding_code IS NULL", updates[0][0])
        self.assertNotIn("INSERT INTO chunks", " ".join(call[0] for call in calls))
        self.assertNotIn("DELETE FROM chunks", " ".join(call[0] for call in calls))
        self.assertIn("def search", model.texts[0])
        self.assertEqual(updates[0][1][1], 9)

    def test_the_code_model_embeds_in_small_batches(self):
        from rag.embedder import Embedder, code_embedder, text_embedder

        class Vector:
            def tolist(self):
                return [0.0] * 768

        class FakeModel:
            def __init__(self):
                self.batch_sizes = []

            def embed(self, texts, batch_size):
                self.batch_sizes.append(batch_size)
                return [Vector() for _ in texts]

        code = code_embedder()
        model = FakeModel()
        with patch.dict(Embedder._shared, {code.model_name: model}):
            code.embed_documents(["def a; end"] * 10)
        self.assertEqual(model.batch_sizes, [4])
        self.assertEqual(text_embedder().batch_size, 32)

    def test_the_code_model_reads_at_most_2048_tokens(self):
        import types

        from rag.embedder import Embedder, code_embedder, text_embedder

        class Tokenizer:
            def __init__(self):
                self.limits = []

            def enable_truncation(self, max_length):
                self.limits.append(max_length)

        class FakeTextEmbedding:
            def __init__(self, **kwargs):
                self.model = types.SimpleNamespace(tokenizer=Tokenizer())

        fastembed = types.SimpleNamespace(TextEmbedding=FakeTextEmbedding)
        with patch.dict(sys.modules, {"fastembed": fastembed}), patch.dict(Embedder._shared, clear=True):
            code_model = code_embedder()._load()
            text_model = text_embedder()._load()
        self.assertEqual(code_model.model.tokenizer.limits, [2048])
        self.assertEqual(text_model.model.tokenizer.limits, [])


def _schema_after(schema, statements):
    from rag.db import CODE_INDEX, TEXT_INDEX, ChunkSchema

    columns = dict(schema.columns)
    indexes = set(schema.indexes)
    for statement in statements:
        parts = statement.split()
        if statement.startswith("ALTER TABLE chunks RENAME COLUMN"):
            columns[parts[7]] = columns.pop(parts[5])
        elif statement.startswith("ALTER INDEX") and "RENAME TO" in statement:
            indexes.discard(parts[2])
            indexes.add(parts[5])
        elif "ADD COLUMN IF NOT EXISTS embedding_text" in statement:
            columns.setdefault("embedding_text", "vector(384)")
        elif "ADD COLUMN IF NOT EXISTS embedding_code" in statement:
            columns.setdefault("embedding_code", "vector(768)")
        elif statement.startswith("CREATE INDEX") and TEXT_INDEX in statement:
            indexes.add(TEXT_INDEX)
        elif statement.startswith("CREATE INDEX") and CODE_INDEX in statement:
            indexes.add(CODE_INDEX)
    return ChunkSchema(columns, frozenset(indexes))


class FlowTest(unittest.TestCase):
    def test_navigation_list_stops_at_the_next_key(self):
        pages = parse_navigation(
            "session:\n  expires_in_minutes: 60\n"
            "navigation:\n  - :fee\n  - :form_name\n  # kept\n\n"
            "feature_switching:\n  - feature_key: band\n"
        )
        self.assertEqual(pages, ["fee", "form_name"])

    def test_question_text_is_the_heading(self):
        labels = question_texts(
            "en:\n  questions:\n    edit:\n      back_button: Back\n"
            "    fee:\n      breadcrumb: Step\n      text: Have you already paid the fee?\n"
            "    benefit:\n      text: 'Do you receive any of the following benefits?'\n"
            "  other:\n    text: Not a question\n"
        )
        self.assertEqual(labels["fee"], "Have you already paid the fee?")
        self.assertEqual(labels["benefit"], "Do you receive any of the following benefits?")
        self.assertNotIn("edit", labels)
        self.assertNotIn("other", labels)

    def test_public_journey_branches_and_links_to_the_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_public_checkout(root)
            chart = charts_for(root)[0]
        edges = {(edge["from"], edge["to"], edge["label"]) for edge in chart["edges"]}
        self.assertIn(("home", "checklist", "Start now"), edges)
        self.assertIn(("checklist", "fee", "Continue"), edges)
        self.assertIn(("applying_on_behalf", "legal_representative", "On behalf"), edges)
        self.assertIn(("applying_on_behalf", "national_insurance", "For themselves"), edges)
        self.assertNotIn(("applying_on_behalf", "legal_representative_detail", ""), edges)
        self.assertIn(("over_16", "national_insurance", "16 or over"), edges)
        self.assertIn(("over_16", "savings_and_investment", "Under 16"), edges)
        self.assertIn(("national_insurance", "home_office", "No NI number"), edges)
        self.assertIn(("national_insurance", "marital_status", "Has an NI number"), edges)
        self.assertIn(("marital_status", "partner_national_insurance", "Married"), edges)
        self.assertIn(("marital_status", "savings_and_investment", "Not married"), edges)
        self.assertIn(("savings_and_investment", "savings_and_investment_extra", "Further savings"), edges)
        self.assertIn(("savings_and_investment", "benefit", "No further savings"), edges)
        self.assertIn(("benefit", "probate", "On a listed benefit"), edges)
        self.assertIn(("benefit", "dependent", "Not on those benefits"), edges)
        self.assertIn(("income_kind", "probate", "No income"), edges)
        self.assertIn(("income_kind", "income_period", "Has income"), edges)
        self.assertIn(("applicant_address", "apply_type", "Legal representative"), edges)
        self.assertIn(("applicant_address", "contact", "Not a representative"), edges)
        self.assertIn(("contact", "summary", "Refund"), edges)
        self.assertIn(("contact", "apply_type", "Not a refund"), edges)
        self.assertIn(("apply_type", "summary", ""), edges)
        self.assertIn(("summary", "submit", "Sign and submit"), edges)
        self.assertIn(("submit", "confirmation", "Accepted"), edges)
        self.assertIn(("submit", "refund", "Refund accepted"), edges)
        self.assertIn(("submit", "summary", "Could not submit"), edges)
        benefit_lane = next(edge["lane"] for edge in chart["edges"] if edge["from"] == "benefit" and edge["to"] == "probate")
        income_lane = next(edge["lane"] for edge in chart["edges"] if edge["from"] == "income_kind" and edge["to"] == "probate")
        self.assertNotEqual(benefit_lane, income_lane)
        fee = next(node for node in chart["nodes"] if node["id"] == "fee")
        self.assertEqual(fee["path"], "app/models/forms/fee.rb")
        self.assertEqual(fee["label"], "Have you already paid the fee?")
        self.assertEqual(fee["start_line"], 2)
        form_name = next(node for node in chart["nodes"] if node["id"] == "form_name")
        self.assertEqual(form_name["path"], "config/settings.yml")
        benefit = next(node for node in chart["nodes"] if node["id"] == "benefit")
        self.assertEqual(benefit["kind"], "choice")
        confirmation = next(node for node in chart["nodes"] if node["id"] == "confirmation")
        refund = next(node for node in chart["nodes"] if node["id"] == "refund")
        self.assertEqual(confirmation["kind"], "end")
        self.assertEqual(refund["row"], confirmation["row"])
        self.assertEqual(refund["column"], 1)
        self.assertEqual(chart["title"], "Apply for help with fees")

    def test_staff_paper_journey_follows_the_redirects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_staff_checkout(root)
            charts = charts_for(root)
        titles = [chart["title"] for chart in charts]
        paper = next(chart for chart in charts if chart["title"].startswith("Process"))
        self.assertEqual(titles, ["Process a paper application", "Check evidence", "Check a part payment"])
        edges = {(edge["from"], edge["to"], edge["label"]) for edge in paper["edges"]}
        self.assertIn(("start", "fee_status", "Band calculation"), edges)
        self.assertIn(("start", "personal", "No band calculation"), edges)
        self.assertIn(("personal", "partner", "Partner details allowed"), edges)
        self.assertIn(("personal", "details", "No partner details"), edges)
        self.assertIn(("details", "approve", "Fee needs approval"), edges)
        self.assertIn(("details", "summary", "No discretion"), edges)
        self.assertIn(("savings", "declaration", "Savings over the limit"), edges)
        self.assertIn(("benefits", "dependents", "Not on benefits"), edges)
        self.assertIn(("summary", "confirmation", "Processed"), edges)
        evidence = next(chart for chart in charts if chart["title"] == "Check evidence")
        evidence_edges = {(edge["from"], edge["to"], edge["label"]) for edge in evidence["edges"]}
        self.assertIn(("accuracy", "income", "Details match"), evidence_edges)
        self.assertIn(("accuracy", "incorrect", "Details do not match"), evidence_edges)
        self.assertIn(("result", "summary", "Next"), evidence_edges)
        part = next(chart for chart in charts if chart["title"] == "Check a part payment")
        part_edges = {(edge["from"], edge["to"], edge["label"]) for edge in part["edges"]}
        self.assertIn(("accuracy", "summary", "Saved"), part_edges)
        self.assertNotIn("return_letter", {node["id"] for node in part["nodes"]})

    def test_page_objects_follow_the_buttons_that_are_clicked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_page_checkout(root)
            charts = charts_for(root)
        self.assertEqual([chart["title"] for chart in charts], ["Make a claim"])
        chart = charts[0]
        self.assertIn("Complete and submit the form", chart["note"])
        edges = {(edge["from"], edge["to"], edge["label"]) for edge in chart["edges"]}
        self.assertIn(("apply_page", "saving_page", "Start a claim"), edges)
        self.assertIn(("saving_page", "details_page", "Save and continue"), edges)
        self.assertIn(("details_page", "extra_page", "Yes"), edges)
        self.assertIn(("details_page", "done_page", "No"), edges)
        self.assertIn(("extra_page", "done_page", "Save and continue"), edges)
        self.assertIn(("apply_page", "return_page", "Return to a claim"), edges)
        done = next(node for node in chart["nodes"] if node["id"] == "done_page")
        returned = next(node for node in chart["nodes"] if node["id"] == "return_page")
        start = next(node for node in chart["nodes"] if node["id"] == "apply_page")
        self.assertEqual(done["kind"], "end")
        self.assertEqual(done["path"], "features/pages/done_page.rb")
        self.assertEqual(returned["row"], start["row"])
        self.assertEqual(returned["column"], 1)
        self.assertEqual(start["kind"], "choice")

    def test_a_decision_tree_starts_at_the_given_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_decision_checkout(root)
            blank = charts_for(root)
            self.assertEqual([chart["title"] for chart in blank], ["Start or continue an application"])
            charts = charts_for(
                root,
                "https://apply-to-court-about-child-arrangements.service.justice.gov.uk/steps/opening/start_or_continue",
            )
        self.assertEqual([chart["title"] for chart in charts], ["Start or continue an application"])
        chart = charts[0]
        self.assertEqual(chart["reader"], "decision")
        edges = {(edge["from"], edge["to"], edge["label"]) for edge in chart["edges"]}
        self.assertIn(("opening/start_or_continue", "opening/start", "Start a new application"), edges)
        self.assertIn(("opening/start_or_continue", "opening/continue_application", "Continue an application"), edges)
        self.assertIn(("opening/start_or_continue", "opening/sign_in_or_create_account", "Yes"), edges)
        self.assertIn(("opening/start_or_continue", "opening/no_court_found", "No court"), edges)
        self.assertIn(("opening/start", "opening/consent_order", "Continue"), edges)
        self.assertIn(("opening/consent_order", "opening/child_protection_cases", "No"), edges)
        self.assertIn(("opening/consent_order", "opening/consent_order_upload", "Yes"), edges)
        start = next(node for node in chart["nodes"] if node["id"] == "opening/start_or_continue")
        needed = next(node for node in chart["nodes"] if node["id"] == "opening/start")
        last = next(node for node in chart["nodes"] if node["id"] == "opening/child_protection_cases")
        nowhere = next(node for node in chart["nodes"] if node["id"] == "opening/no_court_found")
        self.assertEqual(start["label"], "Start or continue an application")
        self.assertEqual(start["kind"], "choice")
        self.assertEqual(start["path"], "app/controllers/steps/opening/start_or_continue_controller.rb")
        self.assertEqual(needed["label"], "What you will need")
        self.assertEqual(last["kind"], "end")
        self.assertEqual(nowhere["kind"], "end")
        self.assertGreater(last["row"], needed["row"])
        self.assertGreater(needed["row"], start["row"])

    def test_an_unmapped_checkout_has_no_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            self.assertEqual(charts_for(root), [])

    def test_flow_links_use_the_indexed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_public_checkout(root)
            repo = {
                "id": 1,
                "name": "hmcts/hwf-publicapp",
                "branch": "master",
                "commit_sha": "abc123def456",
            }

            class Conn:
                def close(self):
                    return None

            with (
                patch("rag.web.connect", return_value=Conn()),
                patch("rag.web.resolve_repositories", return_value=[repo]),
                patch("rag.web.checkout_dir", return_value=root),
            ):
                status, payload = run_flow({"repo": "hmcts/hwf-publicapp"})
        self.assertEqual(status, 200)
        fee = next(node for node in payload["charts"][0]["nodes"] if node["id"] == "fee")
        self.assertEqual(
            fee["url"],
            "https://github.com/hmcts/hwf-publicapp/blob/abc123def456/app/models/forms/fee.rb#L2",
        )
        self.assertEqual(payload["message"], "")
        names = [stage["name"] for stage in payload["usage"]["stages"]]
        self.assertEqual(names, ["Read the journeys", "Compare the branch"])
        self.assertIsNone(payload["usage"]["stages"][0]["total_tokens"])

    def test_a_missing_checkout_is_reported(self):
        repo = {"id": 1, "name": "hmcts/hwf-publicapp", "branch": "master", "commit_sha": "abc123def456"}

        class Conn:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone"
            with (
                patch("rag.web.connect", return_value=Conn()),
                patch("rag.web.resolve_repositories", return_value=[repo]),
                patch("rag.web.checkout_dir", return_value=missing),
            ):
                status, payload = run_flow({})
        self.assertEqual(status, 200)
        self.assertEqual(payload["charts"], [])
        self.assertIn("not on disk", payload["message"])
        self.assertIn("hmcts/hwf-publicapp", payload["message"])


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_public_checkout(root: Path) -> None:
    pages = [
        "fee", "form_name", "applying_on_behalf", "legal_representative",
        "legal_representative_detail", "over_16", "national_insurance", "home_office",
        "marital_status", "partner_national_insurance", "savings_and_investment",
        "savings_and_investment_extra", "benefit", "dependent", "income_kind",
        "income_period", "probate", "claim", "dob", "personal_detail",
        "applicant_address", "contact", "apply_type",
    ]
    listed = "\n".join(f"  - :{page}" for page in pages)
    _write(root, "config/settings.yml", f"navigation:\n{listed}\nfeature_switching:\n  - feature_key: band\n")
    _write(root, "config/locales/en.yml", (
        "en:\n  questions:\n    fee:\n      text: Have you already paid the fee?\n"
        "    benefit:\n      text: Do you receive any of the following benefits?\n"
    ))
    _write(root, "config/routes.rb", "resource :submission, only: :create\n")
    _write(root, "app/views/home/index.html.slim", "h1 Start\n= link_to checklist_path\n")
    _write(root, "app/views/home/checklist/_checklist.html.slim", "= link_to start_session_path\n")
    _write(root, "app/models/forms/fee.rb", "module Forms\n  class Fee\n  end\nend\n")
    _write(root, "app/services/navigation.rb", (
        "class Navigation\n  def next\n"
        "    if last_question? || (@current_question == :contact && skip_apply_type?)\n"
        "      summary_path\n    end\n  end\nend\n"
    ))
    _write(root, "app/services/navigation_helper.rb", _PUBLIC_HELPER)
    _write(root, "app/services/navigation_ni_helper.rb", _PUBLIC_NI)
    _write(root, "app/controllers/summaries_controller.rb", "class SummariesController\n  def show\n  end\nend\n")
    _write(root, "app/controllers/submissions_controller.rb", (
        "class SubmissionsController\n  def create\n  end\n"
        "  def confirmation_route\n    refund_confirmation_path\n    confirmation_path\n  end\n"
        "  def submit\n    redirect_to(summary_path)\n  end\nend\n"
    ))
    _write(root, "app/controllers/confirmations_controller.rb", (
        "class ConfirmationsController\n  def show\n  end\n  def refund\n  end\n"
        "  def create\n    redirect_to finish_session_path\n  end\nend\n"
    ))
    _write(root, "app/controllers/sessions_controller.rb", "class SessionsController\n  def finish\n  end\nend\n")


def _write_page_checkout(root: Path) -> None:
    _write(root, "config/locales/en.yml", (
        "en:\n  claim:\n    header: Make a claim\n    about: Complete and submit the form\n"
        "    start: Start a claim\n    return: Return to a claim\n    saving: Saving your claim\n"
        "    details: Your details\n    extra: Extra question\n    done: Claim submitted\n"
        "    back: Return to your claim\n    continue: Save and continue\n"
    ))
    _write(root, "features/pages/apply_page.rb", (
        "class ApplyPage\n"
        "  set_url config.url\n"
        "  element :header, :main_header, 'claim.header'\n"
        "  element :description, :element_with_text, 'claim.about'\n"
        "  gds_submit_button :start_button, :'claim.start'\n"
        "  gds_submit_button :return_button, :'claim.return'\n"
        "  def start\n    start_button.click\n  end\n"
        "  def return_to\n    return_button.click\n  end\n"
        "end\n"
    ))
    _write(root, "features/pages/saving_page.rb", (
        "class SavingPage\n"
        "  element :page_header, :page_title, 'claim.saving'\n"
        "  gds_submit_button :save_and_continue_button, :'claim.continue'\n"
        "  def register\n    save_and_continue_button.submit\n  end\n"
        "end\n"
    ))
    _write(root, "features/pages/details_page.rb", (
        "class DetailsPage\n"
        "  element :header, :content_header, 'claim.details'\n"
        "  gds_submit_button :continue_button, :'claim.continue'\n"
        "  def next\n    continue_button.click\n  end\n"
        "end\n"
    ))
    _write(root, "features/pages/extra_page.rb", (
        "class ExtraPage\n"
        "  element :header, :content_header, 'claim.extra'\n"
        "  gds_submit_button :continue_button, :'claim.continue'\n"
        "  def next\n    continue_button.click\n  end\n"
        "end\n"
    ))
    _write(root, "features/pages/done_page.rb", (
        "class DonePage\n  element :header, :main_header, 'claim.done'\nend\n"
    ))
    _write(root, "features/pages/return_page.rb", (
        "class ReturnPage\n  element :header, :main_header, 'claim.back'\nend\n"
    ))
    _write(root, "features/pages/only_start_page.rb", (
        "class OnlyStartPage\n  set_url config.other\n"
        "  element :header, :main_header, 'claim.header'\n"
        "  gds_submit_button :start_button, :'claim.start'\n"
        "  def start\n    start_button.click\n  end\n"
        "end\n"
    ))
    _write(root, "features/support/pages.rb", (
        "def apply_page\n  ApplyPage.new\nend\n"
        "def saving_page\n  SavingPage.new\nend\n"
        "def details_page\n  DetailsPage.new\nend\n"
        "def extra_page\n  ExtraPage.new\nend\n"
        "def done_page\n  DonePage.new\nend\n"
        "def return_page\n  ReturnPage.new\nend\n"
    ))
    _write(root, "features/support/journey.rb", (
        "def complete\n  begin_claim\n  saving_page.register\n  answer_details\n  done_page.open\nend\n"
        "def begin_claim\n  apply_page.load\n  apply_page.start\nend\n"
        "def answer_details\n"
        "  if choice\n    details_page.choice.set(:yes)\n    details_page.next\n    extra_page.next\n"
        "  else\n    details_page.next\n  end\n"
        "end\n"
    ))
    _write(root, "features/step_definitions/return_steps.rb", (
        "Given('the user returns') do\n  apply_page.return_to\nend\n"
        "Then('the return page is shown') do\n  return_page.wait\nend\n"
    ))


def _write_staff_checkout(root: Path) -> None:
    _write(root, "app/controllers/applications/process_controller.rb", (
        "class ProcessController\n  def create\n  end\n  def first_page\n"
        "    application_fee_status_path(@application)\n"
        "    application_personal_informations_path(@application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/fee_status_controller.rb", "class Fee\n  def index\n    redirect_to application_personal_informations_path(application)\n  end\nend\n")
    _write(root, "app/controllers/applications/process/personal_informations_controller.rb", (
        "class Personal\n  def index\n  end\n  def path_to_next_page\n"
        "    application_partner_informations_path(application)\n"
        "    application_details_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/partner_informations_controller.rb", (
        "class Partner\n  def index\n  end\n  def create\n    redirect_to application_details_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/details_controller.rb", (
        "class Details\n  def index\n    DetailsRouter.new(application).approval_or_continue\n  end\n"
        "  def approve\n  end\n  def approve_save\n    DetailsRouter.new(application).savings_or_summary\n  end\nend\n"
    ))
    _write(root, "app/services/details_router.rb", (
        "class DetailsRouter\n  def approval_or_continue\n    application_approve_path(application)\n"
        "    application_savings_investments_path(application)\n    application_summary_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/savings_investments_controller.rb", (
        "class Savings\n  def index\n  end\n  def saving_outcome_path\n"
        "    application_declaration_path(application)\n    application_benefits_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/benefits_controller.rb", (
        "class Benefits\n  def index\n  end\n"
        "  def benefit_check_and_redirect\n    application_dependents_path(application)\n"
        "    application_declaration_path(application)\n"
        "    application_benefit_override_paper_evidence_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/benefit_overrides_controller.rb", "class BenefitOverrides\n  def paper_evidence\n  end\nend\n")
    _write(root, "app/controllers/applications/process/dependents_controller.rb", (
        "class Dependents\n  def index\n  end\n  def path_to_next_page\n"
        "    application_income_kind_applicants_path(application)\n"
        "    application_summary_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/declaration_controller.rb", (
        "class Declaration\n  def index\n  end\n  def path_to_next_page\n"
        "    application_representative_path(application)\n"
        "    application_summary_path(application)\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/summary_controller.rb", (
        "class Summary\n  def index\n  end\n  def create\n    application_confirmation_path(application.id, 'paper')\n  end\nend\n"
    ))
    _write(root, "app/controllers/applications/process/confirmation_controller.rb", "class Confirmation\n  def index\n  end\nend\n")
    _write(root, "app/controllers/evidence_controller.rb", (
        "class EvidenceController\n  def accuracy\n  end\n  def income\n  end\n  def result\n  end\n"
        "  def summary\n  end\n  def confirmation\n  end\n"
        "  def redirect_after_accuracy_save\n    income_evidence_path\n"
        "    evidence_accuracy_incorrect_reason_path(evidence)\n  end\n"
        "  def income_save\n    result_evidence_path\n  end\n"
        "  def summary_save\n    confirmation_evidence_path\n  end\nend\n"
    ))
    _write(root, "app/views/evidence/result.html.slim", "= link_to 'Next', summary_evidence_path(@evidence)\n")
    _write(root, "app/views/evidence/accuracy_incorrect_reason/show.html.slim", "h1 Details\n")
    _write(root, "app/controllers/part_payments_controller.rb", (
        "class PartPaymentsController\n  def accuracy\n  end\n  def summary\n  end\n  def confirmation\n  end\n"
        "  def accuracy_save\n    redirect_to(summary_part_payment_path(part_payment))\n  end\n"
        "  def summary_save\n    redirect_to(confirmation_part_payment_path(part_payment))\n  end\n"
        "  def return_application\n    redirect_to return_letter_part_payment_path(part_payment)\n  end\nend\n"
    ))


_PUBLIC_HELPER = """
module NavigationHelper
  def next_question_id
  end

  def probate_or_claim
    :probate
  end

  def skip_income?
    @current_question == :benefit && @online_application.benefits?
  end

  def skip_income_range?
    return false if @online_application.calculation_scheme == Rails.configuration.ucd_schema.to_s
    @current_question == :income_kind && @online_application.income&.zero?
  end

  def skip_savings_and_investment_extra?
    @current_question == :savings_and_investment &&
      !@online_application.savings_and_investment_extra_required?
  end

  def skip_benefit?
    @online_application.ho_number.present? || @online_application.over_16 == false
  end

  def skip_apply_type?
    @online_application.refund?
  end

  def legal_representative?
    next_pages = {
      applying_on_behalf: @online_application.applying_on_behalf ? :legal_representative : :national_insurance,
      applicant_address: @online_application.legal_representative ? :apply_type : :contact
    }
  end

  def over_16?
    return false if @current_question != :over_16

    @next_page = if @online_application.over_16
      :national_insurance
    else
      :savings_and_investment
    end
  end
end
"""

_PUBLIC_NI = """
module NavigationNiHelper
  def no_ni_number_page
    @online_application.ni_number_present == false ? :home_office : :marital_status
  end

  def ni_next_question
    if @online_application.applying_on_behalf
      :legal_representative
    else
      :national_insurance
    end
  end

  def ni_related_question?
    next_pages = {
      applying_on_behalf: ni_next_question,
      national_insurance: no_ni_number_page
    }
  end

  def partner_ni_related_question?
    case @current_question
    when :marital_status
      @partner_ni_next_page = if @online_application.married && @online_application.ni_number_present?
        :partner_national_insurance
      else
        :savings_and_investment
      end
    end
  end
end
"""


def _write_decision_checkout(root: Path) -> None:
    _write(root, "config/routes.rb", """
Rails.application.routes.draw do
  namespace :steps do
    namespace :opening do
      edit_step :start_or_continue
      show_step :start
      edit_step :consent_order
      edit_step :consent_order_upload
      edit_step :child_protection_cases
      edit_step :continue_application
      edit_step :sign_in_or_create_account
      show_step :no_court_found
    end
  end
end
""")
    _write(root, "config/locales/en.yml", """
en:
  steps:
    opening:
      start_or_continue:
        edit:
          heading: Start or continue an application
      start:
        show:
          heading: What you will need
      consent_order:
        edit:
          heading: Is this a consent order?
      no_court_found:
        show:
          heading: Sorry, you cannot apply online
  helpers:
    label:
      steps_opening_start_or_continue_form:
        start_or_continue_options:
          new: Start a new application
          continue: Continue an application
""")
    _write(root, "app/controllers/steps/opening_step_controller.rb", """
class OpeningStepController < StepController
  def decision_tree_class
    C100App::OpeningDecisionTree
  end
end
""")
    _write(root, "app/services/c100_app/opening_decision_tree.rb", """
module C100App
  class OpeningDecisionTree < BaseDecisionTree
    def destination
      case step_name
      when :start_or_continue
        after_start_or_continue
      when :consent_order
        after_consent_order
      else
        raise InvalidStep
      end
    end

    def after_start_or_continue
      if question(:start_or_continue, c100_application, ApplicationIntent).new?
        after_start
      else
        edit(:continue_application)
      end
    end

    def after_start
      if question(:is_legal_representative).yes?
        edit(:sign_in_or_create_account)
      else
        check_court
      end
    end

    def check_court
      court = CourtPostcodeChecker.new.court_for(children_postcode)
      return show(:no_court_found) unless court
      show(:start)
    end

    def after_consent_order
      if question(:consent_order).yes?
        edit(:consent_order_upload)
      else
        edit(:child_protection_cases)
      end
    end
  end
end
""")
    for name in (
        "start_or_continue",
        "start",
        "consent_order",
        "consent_order_upload",
        "child_protection_cases",
        "continue_application",
        "sign_in_or_create_account",
        "no_court_found",
    ):
        class_name = "".join(part.capitalize() for part in name.split("_")) + "Controller"
        action = "show" if name in {"start", "no_court_found"} else "edit"
        _write(
            root,
            f"app/controllers/steps/opening/{name}_controller.rb",
            f"module Steps\n  module Opening\n    class {class_name} < OpeningStepController\n"
            f"      def {action}; end\n    end\n  end\nend\n",
        )
    _write(
        root,
        "app/views/steps/opening/start/show.html.erb",
        "<%= link_button 'Continue', edit_steps_opening_consent_order_path %>\n",
    )


class CliTest(unittest.TestCase):
    def test_remove_help_does_not_need_a_database(self):
        with self.assertRaises(SystemExit) as caught:
            main(["remove", "--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_help_does_not_need_a_database(self):
        from io import StringIO

        with self.assertRaises(SystemExit) as caught:
            with patch("sys.stdout", StringIO()) as output:
                main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("backfill", output.getvalue())
        query_help = StringIO()
        with self.assertRaises(SystemExit) as caught:
            with patch("sys.stdout", query_help):
                main(["query", "--help"])
        self.assertEqual(caught.exception.code, 0)
        text = query_help.getvalue()
        self.assertIn("--index", text)
        self.assertIn("--fusion", text)
        self.assertIn("score", text)
        self.assertIn("rrf", text)


class JourneyTest(unittest.TestCase):
    def test_yes_or_no_follows_the_arrow(self) -> None:
        nodes = [
            {
                "id": "miam/attended",
                "label": "MIAM attended",
                "path": "app/controllers/steps/miam/attended_controller.rb",
            },
            {
                "id": "miam/exemption_claim",
                "label": "MIAM exemption claim",
                "path": "app/controllers/steps/miam/exemption_claim_controller.rb",
                "url": "https://github.com/ministryofjustice/c100-application/blob/abc/exemption.rb#L3",
            },
            {
                "id": "miam/certification",
                "label": "MIAM certification",
                "path": "app/controllers/steps/miam/certification_controller.rb",
            },
        ]
        edges = [
            {"from": "miam/attended", "to": "miam/exemption_claim", "label": "No"},
            {"from": "miam/attended", "to": "miam/certification", "label": "Yes"},
            {"from": "miam/certification", "to": "miam/exemption_claim", "label": "No"},
        ]
        found = branch_answers(nodes, edges, "What happens if the applicant says no to a MIAM?")
        self.assertEqual(
            [(item["from"], item["label"], item["to"]) for item in found],
            [
                ("MIAM attended", "No", "MIAM exemption claim"),
                ("MIAM certification", "No", "MIAM exemption claim"),
            ],
        )
        self.assertTrue(found[0]["url"].startswith("https://github.com/"))

    def test_a_named_answer_uses_that_arrow(self) -> None:
        nodes = [
            {"id": "opening/start_or_continue", "label": "Start or continue an application"},
            {"id": "opening/start", "label": "What you will need"},
        ]
        edges = [{
            "from": "opening/start_or_continue",
            "to": "opening/start",
            "label": "Start a new application",
        }]
        found = branch_answers(nodes, edges, "Where does Start a new application go?")
        self.assertEqual(found[0]["to"], "What you will need")

    def test_a_question_without_an_answer_is_not_an_arrow(self) -> None:
        nodes = [{"id": "miam/attended", "label": "MIAM attended"}]
        edges = [{"from": "miam/attended", "to": "miam/exemption_claim", "label": "No"}]
        self.assertEqual(branch_answers(nodes, edges, "Tell me about the MIAM"), [])

    def test_scopes_include_the_view_and_leave_out_the_shared_locale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = root / "config" / "locales" / "steps" / "opening" / "en.yml"
            locale.parent.mkdir(parents=True)
            locale.write_text("en: {}\n", encoding="utf-8")
            (root / "config" / "locales" / "en.yml").write_text("en: {}\n", encoding="utf-8")
            scopes = journey_scopes(
                [{"path": "app/controllers/steps/opening/start_controller.rb"}],
                root,
            )
        self.assertEqual(scopes, [
            "app/controllers/steps/opening/start_controller.rb",
            "app/views/steps/opening/start",
            "config/locales/steps/opening/en.yml",
        ])

    def test_a_feature_marks_the_step_it_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feature = root / "features" / "opening.feature"
            feature.parent.mkdir()
            feature.write_text(
                "Feature: open\n  When I visit '/steps/opening/start_or_continue'\n"
                "  And I visit '/steps/opening/start'\n",
                encoding="utf-8",
            )
            steps = root / "features" / "step_definitions" / "apply_steps.rb"
            steps.parent.mkdir(parents=True)
            steps.write_text("apply_page.start_now\n", encoding="utf-8")
            chart = {
                "note": "The journey.",
                "nodes": [
                    {"id": "opening/start", "label": "Start", "path": "app/controllers/steps/opening/start_controller.rb"},
                    {"id": "opening/start_or_continue", "label": "Continue", "path": "app/controllers/steps/opening/start_or_continue_controller.rb"},
                    {"id": "opening/other", "label": "Other", "path": "app/controllers/steps/opening/other_controller.rb"},
                    {"id": "apply_page", "label": "Apply", "path": "features/support/page_objects/et1/apply_page.rb"},
                ],
            }
            mark_visits(root, chart)
        self.assertTrue(chart["nodes"][0]["visited"])
        self.assertEqual(chart["nodes"][0]["visits"], ["features/opening.feature"])
        self.assertTrue(chart["nodes"][1]["visited"])
        self.assertNotIn("visited", chart["nodes"][2])
        self.assertTrue(chart["nodes"][3]["visited"])
        self.assertIn("corner mark", chart["note"])

    def test_diff_lists_added_removed_and_rewired_steps(self) -> None:
        indexed = {
            "nodes": [
                {"id": "a", "label": "Start"},
                {"id": "b", "label": "Middle"},
                {"id": "c", "label": "Old end"},
            ],
            "edges": [
                {"from": "a", "to": "b", "label": "No"},
                {"from": "b", "to": "c", "label": "Yes"},
            ],
        }
        tip = {
            "nodes": [
                {"id": "a", "label": "Start"},
                {"id": "b", "label": "Middle"},
                {"id": "d", "label": "New end"},
            ],
            "edges": [
                {"from": "a", "to": "d", "label": "No"},
                {"from": "b", "to": "d", "label": "Yes"},
            ],
        }
        changes = diff_chart(indexed, tip, "abc123def4567890")
        self.assertEqual(changes["remote"], "abc123def456")
        self.assertEqual(changes["added"], ["New end"])
        self.assertEqual(changes["removed"], ["Old end"])
        self.assertEqual(changes["rewired"][0]["label"], "Start")
        self.assertIn("no → New end", changes["rewired"][0]["after"])
        self.assertFalse(changes["unchanged"])

    def test_an_unchanged_journey_is_marked(self) -> None:
        chart = {"nodes": [{"id": "a", "label": "Start"}], "edges": []}
        self.assertTrue(diff_chart(chart, {"nodes": list(chart["nodes"]), "edges": []}, "abc")["unchanged"])

    def test_a_question_about_the_chart_answers_from_the_arrow(self) -> None:
        with patch("rag.web.connect", side_effect=AssertionError("database")):
            status, payload = run_query({
                "question": "What happens if the answer is no to a MIAM?",
                "nodes": [
                    {"id": "miam/attended", "label": "MIAM attended", "path": "app/controllers/steps/miam/attended_controller.rb"},
                    {
                        "id": "miam/exemption_claim",
                        "label": "MIAM exemption claim",
                        "path": "app/controllers/steps/miam/exemption_claim_controller.rb",
                        "url": "https://github.com/ministryofjustice/c100-application/blob/abc/file.rb#L1",
                    },
                ],
                "edges": [{"from": "miam/attended", "to": "miam/exemption_claim", "label": "No"}],
            })
        self.assertEqual(status, 200)
        self.assertEqual(payload["branches"][0]["to"], "MIAM exemption claim")
        self.assertEqual(payload["hits"], [])

    def test_a_journey_question_without_an_arrow_stays_in_the_chart_files(self) -> None:
        seen: dict[str, list[str]] = {}

        def fake_retrieve(*_args, **kwargs):
            seen["scopes"] = kwargs.get("scopes") or []
            return [], ["where is the fee calculated"], False

        class Conn:
            def close(self) -> None:
                return None

        with patch("rag.web.connect", return_value=Conn()), \
             patch("rag.web.resolve_repositories", return_value=[{
                 "id": 1, "name": "acme/app", "branch": "main", "commit_sha": "abc123def456",
             }]), \
             patch("rag.web.retrieve", fake_retrieve), \
             patch("rag.web.checkout_dir", return_value=Path("missing-checkout")):
            status, payload = run_query({
                "question": "where is the fee calculated",
                "repo": "acme/app",
                "nodes": [{
                    "id": "opening/start",
                    "label": "Start",
                    "path": "app/controllers/steps/opening/start_controller.rb",
                }],
                "edges": [],
            })
        self.assertEqual(status, 200)
        self.assertTrue(payload["scoped"])
        self.assertIn("app/views/steps/opening/start", seen["scopes"])
        self.assertEqual(payload["hits"], [])

    def test_tip_checkout_fetches_the_branch_without_moving_the_indexed_commit(self) -> None:
        import os

        from rag import gitrepo

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

        def git(*args: str, cwd: Path) -> str:
            completed = subprocess.run(
                ["git", *args], cwd=cwd, check=True, env=env, capture_output=True, text=True,
            )
            return completed.stdout.strip()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin"
            origin.mkdir()
            git("init", "-b", "main", cwd=origin)
            (origin / "README").write_text("one\n", encoding="utf-8")
            git("add", "README", cwd=origin)
            git("commit", "-m", "one", cwd=origin)
            first = git("rev-parse", "HEAD", cwd=origin)
            clone = root / "repos" / "example" / "app"
            clone.parent.mkdir(parents=True)
            git("clone", "--depth", "1", "--branch", "main", str(origin), str(clone), cwd=root)
            (origin / "README").write_text("two\n", encoding="utf-8")
            git("add", "README", cwd=origin)
            git("commit", "-m", "two", cwd=origin)
            second = git("rev-parse", "HEAD", cwd=origin)
            with patch.object(gitrepo, "REPOS_DIR", root / "repos"):
                tip = gitrepo.tip_checkout("example/app", "main", second)
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual((tip / "README").read_text(encoding="utf-8"), "two\n")
            self.assertEqual(git("rev-parse", "HEAD", cwd=clone), first)


if __name__ == "__main__":
    unittest.main()
