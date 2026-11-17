import json
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
    _query_instructions,
    _with_matches,
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
from rag.web import Handler, _progress_view, github_blob_url, public_status, run_query, start_ingest
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
            self.assertIn('value="both"', page)
            self.assertIn('id="fusion"', page)
            self.assertIn('value="rrf"', page)
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
            self.assertEqual(body["passage_limit"], 8)
            self.assertTrue(body["usage"]["stages"])
            self.assertIn(
                "The values were matched directly, so nothing was embedded.",
                [stage["note"] for stage in body["usage"]["stages"]],
            )
        finally:
            server.shutdown()
            server.server_close()


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

    def test_the_default_index_is_text_and_only_that_model_is_loaded(self):
        from rag.config import (
            CODE_EMBEDDING_DIM,
            CODE_EMBEDDING_MODEL,
            DEFAULT_FUSION,
            DEFAULT_INDEX,
            resolve_fusion,
            resolve_index,
        )

        self.assertEqual(DEFAULT_INDEX, "text")
        self.assertEqual(DEFAULT_FUSION, "score")
        self.assertEqual(resolve_index(None), "text")
        self.assertEqual(resolve_index(" CODE "), "code")
        self.assertEqual(resolve_fusion(None), "score")
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
            self.assertEqual(constructed, [(None, None)])
            self.assertEqual(seen, {"keys": {"text"}, "index": "text", "fusion": "score"})
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


if __name__ == "__main__":
    unittest.main()
