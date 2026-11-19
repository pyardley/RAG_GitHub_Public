"""Feature lookup that stays with Cucumber steps already in the index.

Query plans a question into short searches. A pasted step was planned into
"error message" and "date future", and a request to write a feature was
planned into "refund". The answer then wrote Gherkin that is not in the
suite. Features skips that planner. A Gherkin line is matched as that line
under features/. A request to write a feature is searched as the behaviour
left once the writing instruction is removed.
"""

from __future__ import annotations

import re

from rag.follow import follow_matched_steps
from rag.log import logger
from rag.search import Hit, literal_hits, quoted_phrases, search
from rag.usage import Meter, Stage

_STEP_LINE = re.compile(
    r"(?im)^[ \t]*((?:Given|When|Then|And|But)\s+\S.*?)\s*$"
)
_STEP_KEYWORD = re.compile(r"(?i)^(?:Given|When|Then|And|But)\s+")
_DRAFTING = re.compile(
    r"(?i)(?:\b(?:write|draft|generate|create)\b(?:\s+\w+){0,6}\s+features?\b|\breuse existing steps?\b)"
)
_LEAD = re.compile(
    r"(?i)^(?:please\s+)?(?:write|draft|generate|create)\s+(?:a|an|the)?\s*"
    r"(?:new\s+)?(?:cucumber\s+)?feature(?:\s+file)?\s*(?:for|about|that|:)?\s*"
)
_DROP = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"reuse existing steps?(?:\s+\w+){0,4}"
    r"|do not invent\b.*"
    r"|don't invent\b.*"
    r"|use only existing\b.*"
    r"|without inventing\b.*"
    r")\.?$"
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def gherkin_lines(question: str) -> list[str]:
    """Step lines in the question, with Given, When, Then, And, or But kept."""
    found: list[str] = []
    for match in _STEP_LINE.finditer(question or ""):
        line = " ".join(match.group(1).split())
        if len(line) < 12 or line in found:
            continue
        found.append(line)
    return found


def step_text(line: str) -> str:
    """The step with its keyword removed.

    Given, When, Then, And, and But are the same step. A scenario writes the
    second Then, Given, or When as And, so the keyword is not part of the match.
    """
    return _STEP_KEYWORD.sub("", " ".join((line or "").split()), count=1).strip()


def matched_step_texts(question: str) -> list[str]:
    """Step texts to find. A Gherkin keyword on the line is ignored."""
    found: list[str] = []
    for line in gherkin_lines(question):
        text = step_text(line)
        if text and text not in found:
            found.append(text)
    for phrase in quoted_phrases(question):
        text = step_text(phrase)
        if text and text not in found:
            found.append(text)
    return found


def is_drafting_request(question: str) -> bool:
    """True when the question asks to write a feature or to reuse existing steps."""
    return _DRAFTING.search(question or "") is not None


def behaviour_text(question: str) -> str:
    """The behaviour to search, with the instruction to write a feature removed.

    A question that is not a drafting request is returned unchanged.
    """
    text = (question or "").strip()
    if not is_drafting_request(text):
        return text
    kept: list[str] = []
    for sentence in _sentences(text):
        if _DROP.match(sentence):
            continue
        stripped = _LEAD.sub("", sentence).strip()
        if stripped and not _DROP.match(stripped):
            kept.append(stripped)
    return " ".join(kept).strip() or text


def retrieve_features(
    conn,
    repository_ids: list[int],
    question: str,
    k: int,
    meter: Meter | None = None,
) -> tuple[list[Hit], list[str], bool, bool]:
    """Passages for a feature request.

    Returns the passages, the searches that ran, whether more matches were
    cut off, and whether the question asked for a new feature.
    """
    if k < 1:
        raise ValueError("Result count must be at least 1")
    if meter is not None:
        meter.add(Stage(
            "Plan the question",
            0.0,
            note="Features matches a Gherkin step as written, or searches the behaviour with the writing instruction removed.",
        ))
    phrases = matched_step_texts(question)
    drafting = is_drafting_request(question)
    queries: list[str] = []
    phrase_hits: list[Hit] = []
    chain: list[Hit] = []
    semantic: list[Hit] = []
    truncated = False
    embedded = False

    if phrases:
        phrase_hits, elapsed, more = literal_hits(
            conn, repository_ids, ["features"], phrases, max(k, 8),
        )
        truncated = more
        if meter is not None:
            meter.add_search(0.0, elapsed)
        chain = follow_matched_steps(conn, repository_ids, _quoted(phrases), phrase_hits)
        queries.append("step: " + " | ".join(phrases))

    if drafting or not phrases:
        text = behaviour_text(question) if drafting else question.strip()
        if text:
            # Ask for a deeper list so a feature file past the first page can
            # still be lifted above application code.
            found, more = search(
                conn, repository_ids, text, max(k, 40), meter, index="code", fusion="rrf",
            )
            semantic = found
            truncated = truncated or more
            embedded = True
            queries.append(text)

    ordered = _prefer(phrase_hits, chain, semantic)
    hits = ordered[:k]
    truncated = truncated or len(ordered) > k
    if meter is not None:
        meter.finish_retrieval(
            len(hits),
            embedded=embedded,
            direct=None if embedded else "text",
        )
    logger.info(
        "features drafting=%s passages=%s queries=%s",
        drafting,
        len(hits),
        " | ".join(queries),
    )
    return hits, queries, truncated, drafting


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(text.strip()) if part.strip()]


def _quoted(phrases: list[str]) -> str:
    """A question whose quotes are the steps, so step following can see them."""
    return " ".join('"' + phrase.replace('"', "'") + '"' for phrase in phrases)


def _under_features(path: str) -> bool:
    """True for the Cucumber features directory, not a spec path that contains the word."""
    lowered = path.replace("\\", "/").lower().lstrip("/")
    return lowered == "features" or lowered.startswith("features/")


def _features_first(hits: list[Hit]) -> list[Hit]:
    """Keep the retrieved order inside each group, and list features/ before the rest."""
    return sorted(hits, key=lambda hit: 0 if _under_features(hit.path) else 1)


def _prefer(
    phrase_hits: list[Hit],
    chain: list[Hit],
    semantic: list[Hit],
) -> list[Hit]:
    """Exact steps first, then the code that runs them, then the behaviour search.

    Files under features/ stay above application code and specs.
    """
    merged: list[Hit] = []
    seen: set[int] = set()

    def add(hit: Hit) -> None:
        if hit.chunk_id in seen:
            return
        seen.add(hit.chunk_id)
        merged.append(hit)

    for hit in phrase_hits:
        add(hit)
    for hit in chain:
        add(hit)
    for hit in semantic:
        add(hit)
    return _features_first(merged)
