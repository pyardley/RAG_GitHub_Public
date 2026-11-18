"""End-to-end flow charts for an indexed checkout.

Two readers share one chart. An application that lists its questions in
``config/settings.yml``, or sends the next page with ``redirect_to``, is read
from those files. A test suite that describes screens as page objects is read
from the page objects and the journey that clicks through them. Neither reader
names a repository. Both use the checkout of the indexed commit, so the chart
stays with that commit. A box links to the file that implements the step.
"""

from __future__ import annotations

import re
from pathlib import Path

_NAV_ITEM = re.compile(r"^[ \t]+-[ \t]+:([A-Za-z0-9_]+)\s*(?:#.*)?$")
_QUESTION_KEY = re.compile(r"^    ([a-z0-9_]+):\s*(?:#.*)?$")
_QUESTION_TEXT = re.compile(r"^      text:\s+(\S.*?)\s*$")
_IF_ELSE = re.compile(
    r"if\b[^\n]*\n\s*:([A-Za-z0-9_]+)\s*\n\s*else\s*\n\s*:([A-Za-z0-9_]+)\b",
    re.M,
)
_TERNARY = re.compile(
    r"(\w+):\s*@online_application\.\w+\s*\?\s*:(\w+)\s*:\s*:(\w+)"
)
_SYMBOL = re.compile(r":([A-Za-z0-9_]+)")

# Suffix ids that a question symbol must not collide with.
_RESERVED = {"home", "checklist", "summary", "submit", "confirmation", "refund", "finish"}

_UNMAPPED = "No end-to-end journey was found in this repository."


def charts_for(root: Path) -> list[dict]:
    """Flow charts found in a checkout. An empty list means nothing was found."""
    if not root.is_dir():
        return []
    charts: list[dict] = []
    public = public_chart(root)
    if public:
        charts.append(public)
    charts.extend(staff_charts(root))
    # Imported here so this module can finish loading before page flows import it back.
    from rag.pageflows import page_object_charts

    charts.extend(page_object_charts(root))
    return charts


def parse_navigation(source: str) -> list[str]:
    """Question symbols under the top-level ``navigation:`` key, in page order."""
    items: list[str] = []
    in_list = False
    for line in source.splitlines():
        if not in_list:
            if re.match(r"^navigation:\s*(?:#.*)?$", line):
                in_list = True
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break
        match = _NAV_ITEM.match(line)
        if match:
            items.append(match.group(1))
    return items


def question_texts(source: str) -> dict[str, str]:
    """Map a question id to its h1, from ``questions.<id>.text`` in a locale file."""
    labels: dict[str, str] = {}
    in_questions = False
    current: str | None = None
    for line in source.splitlines():
        if not in_questions:
            if re.match(r"^  questions:\s*(?:#.*)?$", line):
                in_questions = True
            continue
        if re.match(r"^  [A-Za-z0-9_]+:\s*", line):
            break
        key = _QUESTION_KEY.match(line)
        if key:
            current = key.group(1)
            continue
        if current is None or current in labels:
            continue
        found = _QUESTION_TEXT.match(line)
        if not found:
            continue
        value = _yaml_scalar(found.group(1))
        if value:
            labels[current] = value
    return labels


def public_chart(root: Path) -> dict | None:
    """Citizen journey, or None when this checkout is not that application."""
    helper = _read(root / "app/services/navigation_helper.rb")
    settings = _read(root / "config/settings.yml")
    pages = parse_navigation(settings)
    if not helper or len(pages) < 2:
        return None
    ni = _read(root / "app/services/navigation_ni_helper.rb")
    navigation = _read(root / "app/services/navigation.rb")
    labels = _locale_labels(root)
    nodes: dict[str, dict] = {}
    for symbol in pages:
        nodes[_page_id(symbol)] = _question_node(root, symbol, settings, labels)

    _prefix_nodes(root, nodes)
    _suffix_nodes(root, nodes)
    edges = _public_edges(pages, nodes, helper, ni, navigation)
    if len(nodes) < 2 or not edges:
        return None
    _mark_choices(nodes, edges)
    _place(list(nodes.values()), _public_order(pages), shared={"refund": ("confirmation", 1)})
    _route(list(nodes.values()), edges)
    return {
        "title": "Apply for help with fees",
        "note": (
            "Each box opens the file for that step. "
            "A label on an arrow is the answer that chooses it. "
            "An arrow with no label follows the question list in config/settings.yml."
        ),
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def staff_charts(root: Path) -> list[dict]:
    """Paper processing, evidence check, and part payment, when those controllers exist."""
    charts = []
    for builder in (paper_chart, evidence_chart, part_payment_chart):
        chart = builder(root)
        if chart and chart["edges"]:
            charts.append(chart)
    return charts


def paper_chart(root: Path) -> dict | None:
    process = _read(root / "app/controllers/applications/process_controller.rb")
    if not process:
        return None
    nodes: dict[str, dict] = {}
    texts: dict[str, str] = {"start": process}
    _add(nodes, root, "start", "Start a paper application", [
        "app/controllers/applications/process_controller.rb",
    ], ["def create"])
    steps = (
        ("fee_status", "Fee status", "app/controllers/applications/process/fee_status_controller.rb"),
        ("personal", "Personal details", "app/controllers/applications/process/personal_informations_controller.rb"),
        ("partner", "Partner details", "app/controllers/applications/process/partner_informations_controller.rb"),
        ("details", "Application details", "app/controllers/applications/process/details_controller.rb"),
        ("savings", "Savings and investments", "app/controllers/applications/process/savings_investments_controller.rb"),
        ("benefits", "Benefits", "app/controllers/applications/process/benefits_controller.rb"),
        ("evidence", "Benefit evidence", "app/controllers/benefit_overrides_controller.rb"),
        ("dependents", "Children", "app/controllers/applications/process/dependents_controller.rb"),
        ("income_kind", "Applicant income types", "app/controllers/applications/process/income_kind_applicants_controller.rb"),
        ("partner_income", "Partner income types", "app/controllers/applications/process/income_kind_partners_controller.rb"),
        ("incomes", "Income", "app/controllers/applications/process/incomes_controller.rb"),
        ("declaration", "Declaration", "app/controllers/applications/process/declaration_controller.rb"),
        ("representative", "Representative", "app/controllers/applications/process/representative_controller.rb"),
        ("summary", "Summary", "app/controllers/applications/process/summary_controller.rb"),
        ("confirmation", "Confirmation", "app/controllers/applications/process/confirmation_controller.rb"),
    )
    for step_id, label, path in steps:
        texts[step_id] = _add(nodes, root, step_id, label, [path], ["def index", "def show", "def paper_evidence"])
    # Fee approval is an action on the details controller, not its own file.
    if "def approve" in texts.get("details", ""):
        nodes["approve"] = _node(
            "approve", "Fee approval", nodes["details"]["path"], texts["details"], ["def approve"]
        )
    router = _read(root / "app/services/details_router.rb")
    edges: list[dict] = []
    _when(edges, nodes, process, "application_fee_status_path", "start", "fee_status", "Band calculation")
    _when(edges, nodes, process, "application_personal_informations_path", "start", "personal", "No band calculation")
    _when(edges, nodes, texts.get("fee_status", ""), "application_personal_informations_path", "fee_status", "personal", "Continue")
    _when(edges, nodes, texts.get("personal", ""), "application_partner_informations_path", "personal", "partner", "Partner details allowed")
    _when(edges, nodes, texts.get("personal", ""), "application_details_path", "personal", "details", "No partner details")
    _when(edges, nodes, texts.get("partner", ""), "application_details_path", "partner", "details", "Continue")
    if "DetailsRouter" in texts.get("details", ""):
        _when(edges, nodes, router, "application_approve_path", "details", "approve", "Fee needs approval")
        _when(edges, nodes, router, "application_savings_investments_path", "details", "savings", "Discretion applied")
        _when(edges, nodes, router, "application_summary_path", "details", "summary", "No discretion")
    if "savings_or_summary" in texts.get("details", ""):
        _when(edges, nodes, router, "application_savings_investments_path", "approve", "savings", "Discretion applied")
        _when(edges, nodes, router, "application_summary_path", "approve", "summary", "No discretion")
    _when(edges, nodes, texts.get("savings", ""), "application_benefits_path", "savings", "benefits", "Savings within the limit")
    _when(edges, nodes, texts.get("savings", ""), "application_declaration_path", "savings", "declaration", "Savings over the limit")
    _when(edges, nodes, texts.get("benefits", ""), "application_dependents_path", "benefits", "dependents", "Not on benefits")
    _when(edges, nodes, texts.get("benefits", ""), "application_declaration_path", "benefits", "declaration", "Benefit confirmed")
    _when(edges, nodes, texts.get("benefits", ""), "application_benefit_override_paper_evidence_path", "benefits", "evidence", "Needs evidence")
    _when(edges, nodes, texts.get("dependents", ""), "application_income_kind_applicants_path", "dependents", "income_kind", "UCD scheme")
    _when(edges, nodes, texts.get("dependents", ""), "application_summary_path", "dependents", "summary", "Older scheme")
    _when(edges, nodes, texts.get("income_kind", ""), "application_income_kind_partners_path", "income_kind", "partner_income", "Married")
    _when(edges, nodes, texts.get("income_kind", ""), "application_incomes_path", "income_kind", "incomes", "Not married")
    _when(edges, nodes, texts.get("partner_income", ""), "application_incomes_path", "partner_income", "incomes", "Continue")
    _when(edges, nodes, texts.get("incomes", ""), "application_declaration_path", "incomes", "declaration", "UCD scheme")
    _when(edges, nodes, texts.get("incomes", ""), "application_summary_path", "incomes", "summary", "Older scheme")
    _when(edges, nodes, texts.get("declaration", ""), "application_representative_path", "declaration", "representative", "Has a representative")
    _when(edges, nodes, texts.get("declaration", ""), "application_summary_path", "declaration", "summary", "No representative")
    _when(edges, nodes, texts.get("representative", ""), "application_summary_path", "representative", "summary", "Continue")
    _when(edges, nodes, texts.get("summary", ""), "application_confirmation_path", "summary", "confirmation", "Processed")
    if "confirmation" in nodes:
        nodes["confirmation"]["kind"] = "end"
    _mark_choices(nodes, edges)
    order = [
        "start", "fee_status", "personal", "partner", "details", "approve", "savings",
        "benefits", "evidence", "dependents", "income_kind", "partner_income", "incomes",
        "declaration", "representative", "summary", "confirmation",
    ]
    _place(list(nodes.values()), order)
    _route(list(nodes.values()), edges)
    return {
        "title": "Process a paper application",
        "note": "Staff enter a paper application. An arrow is a redirect in that step's controller.",
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def evidence_chart(root: Path) -> dict | None:
    path = "app/controllers/evidence_controller.rb"
    source = _read(root / path)
    if not source:
        return None
    nodes: dict[str, dict] = {}
    for step_id, label, needle in (
        ("accuracy", "Check the details", "def accuracy"),
        ("income", "Check income", "def income"),
        ("result", "Income result", "def result"),
        ("summary", "Evidence summary", "def summary"),
        ("confirmation", "Evidence decision", "def confirmation"),
    ):
        if needle in source:
            nodes[step_id] = _node(step_id, label, path, source, [needle])
    incorrect_path, incorrect = _first(root, [
        "app/controllers/evidence/accuracy_incorrect_reason_controller.rb",
        "app/views/evidence/accuracy_incorrect_reason/show.html.slim",
    ])
    if incorrect:
        nodes["incorrect"] = _node(
            "incorrect", "Details do not match", incorrect_path, incorrect, ["def show", "def update", "class"]
        )
    edges: list[dict] = []
    _when(edges, nodes, source, "income_evidence_path", "accuracy", "income", "Details match")
    _when(edges, nodes, source, "evidence_accuracy_incorrect_reason_path", "accuracy", "incorrect", "Details do not match")
    _when(edges, nodes, source, "result_evidence_path", "income", "result", "Next")
    result_view = _read(root / "app/views/evidence/result.html.slim")
    _when(edges, nodes, result_view, "summary_evidence_path", "result", "summary", "Next")
    _when(edges, nodes, source, "confirmation_evidence_path", "summary", "confirmation", "Complete")
    if "confirmation" in nodes:
        nodes["confirmation"]["kind"] = "end"
    _mark_choices(nodes, edges)
    if "confirmation" in nodes:
        nodes["confirmation"]["kind"] = "end"
    _place(list(nodes.values()), ["accuracy", "incorrect", "income", "result", "summary", "confirmation"])
    _route(list(nodes.values()), edges)
    return {
        "title": "Check evidence",
        "note": "An evidence check. Arrows are the redirects, and the Next button on the income result.",
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def part_payment_chart(root: Path) -> dict | None:
    path = "app/controllers/part_payments_controller.rb"
    source = _read(root / path)
    if not source:
        return None
    nodes: dict[str, dict] = {}
    for step_id, label, needle in (
        ("accuracy", "Check the payment", "def accuracy"),
        ("summary", "Part-payment summary", "def summary"),
        ("confirmation", "Part-payment decision", "def confirmation"),
    ):
        if needle in source:
            nodes[step_id] = _node(step_id, label, path, source, [needle])
    edges: list[dict] = []
    # The redirect lives in the save action, not on the page that renders the form.
    _when(edges, nodes, _method(source, "accuracy_save"), "summary_part_payment_path", "accuracy", "summary", "Saved")
    _when(edges, nodes, _method(source, "summary_save"), "confirmation_part_payment_path", "summary", "confirmation", "Complete")
    if "confirmation" in nodes:
        nodes["confirmation"]["kind"] = "end"
    _mark_choices(nodes, edges)
    if "confirmation" in nodes:
        nodes["confirmation"]["kind"] = "end"
    _place(list(nodes.values()), ["accuracy", "summary", "confirmation"])
    _route(list(nodes.values()), edges)
    return {
        "title": "Check a part payment",
        "note": "A part-payment check. An arrow is a redirect in the part-payment controller.",
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _public_edges(
    pages: list[str],
    nodes: dict[str, dict],
    helper: str,
    ni: str,
    navigation: str,
) -> list[dict]:
    replaced: set[str] = set()
    labels: dict[str, str] = {}
    extra: list[tuple[str, str, str]] = []

    def branch(src: str, dst: str, label: str, *, instead: bool = False) -> None:
        if src not in pages:
            return
        if dst != "summary" and dst not in pages:
            return
        if instead:
            replaced.add(src)
        extra.append((src, dst, label))

    yes, no = _if_else(_method(ni, "ni_next_question"))
    related = _method(ni, "ni_related_question?")
    if yes and no and "applying_on_behalf:" in related:
        branch("applying_on_behalf", yes, "On behalf", instead=True)
        branch("applying_on_behalf", no, "For themselves", instead=True)
    else:
        for question, yes_id, no_id in _TERNARY.findall(_method(helper, "legal_representative?")):
            if question == "applying_on_behalf":
                branch(question, yes_id, "On behalf", instead=True)
                branch(question, no_id, "For themselves", instead=True)

    no_ni, has_ni = _false_ternary(_method(ni, "no_ni_number_page"))
    if no_ni and has_ni and "national_insurance:" in related:
        branch("national_insurance", no_ni, "No NI number", instead=True)
        branch("national_insurance", has_ni, "Has an NI number", instead=True)

    married, single = _if_else(_method(ni, "partner_ni_related_question?"))
    if married and single and ":marital_status" in _method(ni, "partner_ni_related_question?"):
        branch("marital_status", married, "Married", instead=True)
        branch("marital_status", single, "Not married", instead=True)

    adult, child = _if_else(_method(helper, "over_16?"))
    if adult and child and ":over_16" in _method(helper, "over_16?"):
        branch("over_16", adult, "16 or over", instead=True)
        branch("over_16", child, "Under 16", instead=True)

    for question, yes_id, no_id in _TERNARY.findall(_method(helper, "legal_representative?")):
        if question == "applicant_address":
            branch(question, yes_id, "Legal representative", instead=True)
            branch(question, no_id, "Not a representative", instead=True)

    if _method(helper, "skip_savings_and_investment_extra?") and "savings_and_investment_extra" in pages:
        labels["savings_and_investment"] = "Further savings"
        branch("savings_and_investment", "benefit", "No further savings")
        if "ho_number" in _method(helper, "skip_benefit?") or "over_16" in _method(helper, "skip_benefit?"):
            branch("savings_and_investment", "dependent", "Under 16 or HO number")
            branch("savings_and_investment_extra", "dependent", "Under 16 or HO number", instead=True)
            branch("savings_and_investment_extra", "benefit", "Otherwise", instead=True)

    probate = _first_symbol(_method(helper, "probate_or_claim"))
    skip_income = _method(helper, "skip_income?")
    if probate and ":benefit" in skip_income and "benefits?" in skip_income:
        nxt = _next_page(pages, "benefit")
        if nxt:
            branch("benefit", nxt, "Not on those benefits", instead=True)
        branch("benefit", probate, "On a listed benefit", instead=True)

    income_kind = _method(helper, "skip_income_range?")
    if ":income_kind" in income_kind and probate:
        labels["income_kind"] = "Has income"
        branch("income_kind", probate, "No income")

    if "refund?" in _method(helper, "skip_apply_type?") and ":contact" in navigation and "skip_apply_type?" in navigation:
        labels["contact"] = "Not a refund"
        branch("contact", "summary", "Refund")

    edges = _default_edges(pages, replaced, labels)
    for src, dst, label in extra:
        _link(edges, nodes, src, dst, label)
    _link_prefix(edges, nodes, pages)
    _link_suffix(edges, nodes, pages)
    return _dedupe(edges)


def _default_edges(pages: list[str], replaced: set[str], labels: dict[str, str]) -> list[dict]:
    edges = []
    for src, dst in zip(pages, pages[1:]):
        if src in replaced:
            continue
        edges.append({"from": _page_id(src), "to": _page_id(dst), "label": labels.get(src, "")})
    return edges


def _link_prefix(edges: list[dict], nodes: dict[str, dict], pages: list[str]) -> None:
    first = _page_id(pages[0])
    if "home" in nodes and "checklist" in nodes:
        edges.append({"from": "home", "to": "checklist", "label": "Start now"})
    if "checklist" in nodes:
        edges.append({"from": "checklist", "to": first, "label": "Continue"})
    elif "home" in nodes:
        edges.append({"from": "home", "to": first, "label": "Start"})


def _link_suffix(edges: list[dict], nodes: dict[str, dict], pages: list[str]) -> None:
    last = _page_id(pages[-1])
    if "summary" in nodes and not any(edge["from"] == last and edge["to"] == "summary" for edge in edges):
        # navigation.rb sends the last question to summary_path. The summary
        # controller is enough when that file does not name the method.
        edges.append({"from": last, "to": "summary", "label": ""})
    if "summary" in nodes and "submit" in nodes:
        edges.append({"from": "summary", "to": "submit", "label": "Sign and submit"})
    if "submit" in nodes and "confirmation" in nodes:
        edges.append({"from": "submit", "to": "confirmation", "label": "Accepted"})
    if "submit" in nodes and "refund" in nodes:
        edges.append({"from": "submit", "to": "refund", "label": "Refund accepted"})
    if "submit" in nodes and "summary" in nodes:
        edges.append({"from": "submit", "to": "summary", "label": "Could not submit"})
    if "confirmation" in nodes and "finish" in nodes:
        edges.append({"from": "confirmation", "to": "finish", "label": "Reference confirmed"})
    if "refund" in nodes and "finish" in nodes:
        edges.append({"from": "refund", "to": "finish", "label": "Reference confirmed"})


def _link(edges: list[dict], nodes: dict[str, dict], src: str, dst: str, label: str) -> None:
    source = src if src in nodes else _page_id(src)
    target = dst if dst in nodes else _page_id(dst)
    if source in nodes and target in nodes and source != target:
        edges.append({"from": source, "to": target, "label": label})


def _prefix_nodes(root: Path, nodes: dict[str, dict]) -> None:
    home = _read(root / "app/views/home/index.html.slim")
    checklist = _read(root / "app/views/home/checklist/_checklist.html.slim")
    if not checklist:
        checklist_path = "app/views/home/checklist.html.slim"
        checklist = _read(root / checklist_path)
    else:
        checklist_path = "app/views/home/checklist/_checklist.html.slim"
    if home and "checklist_path" in home:
        nodes["home"] = _node("home", "Start page", "app/views/home/index.html.slim", home, ["checklist_path"])
    if checklist and "start_session_path" in checklist:
        nodes["checklist"] = _node("checklist", "Checklist", checklist_path, checklist, ["start_session_path"])


def _suffix_nodes(root: Path, nodes: dict[str, dict]) -> None:
    summary = _read(root / "app/controllers/summaries_controller.rb")
    submit = _read(root / "app/controllers/submissions_controller.rb")
    confirmation = _read(root / "app/controllers/confirmations_controller.rb")
    finish = _read(root / "app/controllers/sessions_controller.rb")
    routes = _read(root / "config/routes.rb")
    if summary and "def show" in summary:
        nodes["summary"] = _node("summary", "Check your answers", "app/controllers/summaries_controller.rb", summary, ["def show"])
    if submit and ("resource :submission" in routes or "def create" in submit):
        nodes["submit"] = _node("submit", "Submit the application", "app/controllers/submissions_controller.rb", submit, ["def create"])
    if confirmation and "def show" in confirmation:
        nodes["confirmation"] = _node(
            "confirmation", "Confirmation", "app/controllers/confirmations_controller.rb", confirmation, ["def show"], kind="end"
        )
    if confirmation and "def refund" in confirmation and "refund_confirmation_path" in submit:
        nodes["refund"] = _node(
            "refund", "Refund confirmation", "app/controllers/confirmations_controller.rb", confirmation, ["def refund"], kind="end"
        )
    if finish and "def finish" in finish and "finish_session_path" in confirmation:
        nodes["finish"] = _node("finish", "Finish", "app/controllers/sessions_controller.rb", finish, ["def finish"], kind="end")


def _question_node(root: Path, symbol: str, settings: str, labels: dict[str, str]) -> dict:
    full = labels.get(symbol, "")
    label = _short(full) if full else _humanize(symbol)
    relatives = [f"app/models/forms/{symbol}.rb"]
    if symbol == "claim":
        relatives = [
            "app/models/forms/claim/default.rb",
            "app/models/forms/claim.rb",
        ]
    path, text = _first(root, relatives)
    if not text:
        view = f"app/views/questions/forms/_{symbol}.html.slim"
        path, text = _first(root, [view])
    if not text:
        path, text = "config/settings.yml", settings
        needles = [f"- :{symbol}"]
    else:
        needles = ["class ", "module "]
    return _node(
        _page_id(symbol),
        label,
        path,
        text,
        needles,
        detail=f"{full or label}\n{path}",
    )


def _locale_labels(root: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    locale = root / "config" / "locales"
    if not locale.is_dir():
        return labels
    for path in sorted(locale.rglob("en.yml")):
        for key, value in question_texts(_read(path)).items():
            labels.setdefault(key, value)
    return labels


def _public_order(pages: list[str]) -> list[str]:
    return ["home", "checklist", *[_page_id(page) for page in pages], "summary", "submit", "confirmation", "finish"]


def _page_id(symbol: str) -> str:
    if symbol in _RESERVED:
        return f"page-{symbol}"
    return symbol


def _next_page(pages: list[str], symbol: str) -> str | None:
    try:
        index = pages.index(symbol)
    except ValueError:
        return None
    if index + 1 >= len(pages):
        return None
    return pages[index + 1]


def _method(source: str, name: str) -> str:
    """Body of a Ruby method, through the start of the next method."""
    if not source:
        return ""
    match = re.search(
        rf"(?m)^[ \t]*def {re.escape(name)}[^\n]*\n(.*?)(?=^[ \t]*def |\Z)",
        source,
        re.S,
    )
    return match.group(1) if match else ""


def _if_else(body: str) -> tuple[str, str]:
    match = _IF_ELSE.search(body)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _false_ternary(body: str) -> tuple[str, str]:
    """Symbols of ``== false ? :a : :b``. The first is the negative answer."""
    match = re.search(r"==\s*false\s*\?\s*:(\w+)\s*:\s*:(\w+)", body)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _first_symbol(body: str) -> str:
    match = _SYMBOL.search(body)
    return match.group(1) if match else ""


def _add(
    nodes: dict[str, dict],
    root: Path,
    step_id: str,
    label: str,
    relatives: list[str],
    needles: list[str],
    kind: str = "step",
) -> str:
    path, text = _first(root, relatives)
    if not text:
        return ""
    nodes[step_id] = _node(step_id, label, path, text, needles, kind=kind)
    return text


def _when(
    edges: list[dict],
    nodes: dict[str, dict],
    source: str,
    token: str,
    src: str,
    dst: str,
    label: str,
) -> None:
    if token and token in source and src in nodes and dst in nodes:
        edges.append({"from": src, "to": dst, "label": label})


def _node(
    step_id: str,
    label: str,
    path: str,
    text: str,
    needles: list[str],
    kind: str = "step",
    detail: str = "",
) -> dict:
    line = _line_number(text, needles)
    return {
        "id": step_id,
        "label": label,
        "kind": kind,
        "path": path,
        "start_line": line,
        "end_line": line,
        "column": 0,
        "detail": detail or f"{label}\n{path}",
    }


def _mark_choices(nodes: dict[str, dict], edges: list[dict]) -> None:
    outgoing: dict[str, int] = {}
    for edge in edges:
        outgoing[edge["from"]] = outgoing.get(edge["from"], 0) + 1
    for node in nodes.values():
        if node["kind"] == "end":
            continue
        if outgoing.get(node["id"], 0) > 1:
            node["kind"] = "choice"


def _place(nodes: list[dict], order: list[str], shared: dict[str, tuple[str, int]] | None = None) -> None:
    """Give each node a row in journey order. ``shared`` copies another node's row."""
    shared = shared or {}
    row_of: dict[str, int] = {}
    row = 0
    for step_id in order:
        if any(node["id"] == step_id for node in nodes):
            row_of[step_id] = row
            row += 1
    for node in nodes:
        if node["id"] in shared and shared[node["id"]][0] in row_of:
            node["row"] = row_of[shared[node["id"]][0]]
            node["column"] = shared[node["id"]][1]
            continue
        if node["id"] in row_of:
            node["row"] = row_of[node["id"]]
        else:
            node["row"] = row
            row += 1
        node.setdefault("column", 0)


def _route(nodes: list[dict], edges: list[dict]) -> None:
    """Mark an edge as a straight step, a side step, or a lane around the spine."""
    row = {node["id"]: node["row"] for node in nodes}
    column = {node["id"]: node.get("column", 0) for node in nodes}
    lanes: list[int] = []
    skipping: list[dict] = []
    for edge in edges:
        src, dst = edge["from"], edge["to"]
        if src not in row or dst not in row:
            edge["route"] = "straight"
            continue
        if column[src] != column[dst] and abs(row[dst] - row[src]) <= 1:
            edge["route"] = "side"
            continue
        if column[src] == column[dst] and row[dst] == row[src] + 1:
            edge["route"] = "straight"
            continue
        edge["route"] = "lane"
        skipping.append(edge)
    skipping.sort(key=lambda edge: (min(row[edge["from"]], row[edge["to"]]), row[edge["from"]]))
    for edge in skipping:
        start = min(row[edge["from"]], row[edge["to"]])
        end = max(row[edge["from"]], row[edge["to"]])
        lane = 0
        while lane < len(lanes) and lanes[lane] >= start:
            lane += 1
        if lane == len(lanes):
            lanes.append(end)
        else:
            lanes[lane] = end
        edge["lane"] = lane


def _dedupe(edges: list[dict]) -> list[dict]:
    """Keep one arrow for each pair and label. An unlabeled copy of a labeled arrow goes."""
    kept: dict[tuple[str, str, str], dict] = {}
    for edge in edges:
        kept[(edge["from"], edge["to"], edge.get("label") or "")] = edge
    labeled = {(edge["from"], edge["to"]) for edge in kept.values() if edge.get("label")}
    return [
        edge for edge in kept.values()
        if edge.get("label") or (edge["from"], edge["to"]) not in labeled
    ]


def _first(root: Path, relatives: list[str]) -> tuple[str, str]:
    for relative in relatives:
        text = _read(root / relative)
        if text:
            return relative, text
    return "", ""


def _read(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _line_number(text: str, needles: list[str]) -> int:
    lines = text.splitlines()
    for needle in needles:
        for number, line in enumerate(lines, 1):
            if needle in line:
                return number
    return 1


def _yaml_scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    if text.startswith(("|", ">")):
        return ""
    return re.sub(r"\s+", " ", text.replace("''", "'")).strip()


def _short(text: str, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _humanize(symbol: str) -> str:
    words = {"dob": "Date of birth", "ni": "National Insurance"}
    if symbol in words:
        return words[symbol]
    return symbol.replace("_", " ").capitalize()


def unmapped_message(names: list[str]) -> str:
    if not names:
        return _UNMAPPED
    if len(names) == 1:
        return _UNMAPPED.replace("this repository", names[0], 1)
    joined = ", ".join(names)
    return _UNMAPPED.replace("this repository", joined, 1)
