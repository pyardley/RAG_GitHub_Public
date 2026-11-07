"""Turn a natural-language question into a few search queries.

The retrieval model is the small one. It does not write the answer. If it
fails, retrieval uses the question as it was asked.
"""

from __future__ import annotations

import json
import re
import time

from rag.answer import AnswerError, visible_answer
from rag.config import (
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_RETRIEVAL_MODEL,
    OLLAMA_TIMEOUT,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_RETRIEVAL_MODEL,
)
from rag.follow import feature_paths
from rag.search import asks_for_features, code_literals, path_scopes, quoted_phrases
from rag.log import logger
from rag.usage import Meter, Stage, token_counts

_MAX_QUERIES = 4
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_PROMPT = (
    "You turn a question about a source-code repository into search queries "
    "for a keyword index. Reply with JSON only, in the shape "
    '{"queries": ["...", "..."]}. Return two queries. One is the single most '
    "specific noun in the question. The other is two words copied from the question. "
    "Use only words that appear in the question. Do not add synonyms. Do not invent "
    "class names, method names, or file names. Do not answer the question."
)


def retrieval_model() -> str | None:
    if LLM_PROVIDER == "ollama":
        return OLLAMA_RETRIEVAL_MODEL or None
    if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
        return OPENAI_RETRIEVAL_MODEL or None
    return None


def question_needs_plan(question: str) -> bool:
    if not question.strip():
        return False
    if asks_for_features(question) or feature_paths(question):
        return False
    if quoted_phrases(question):
        return False
    if path_scopes(question) and code_literals(question):
        return False
    return retrieval_model() is not None


def search_queries(question: str, meter: Meter | None = None) -> list[str]:
    started = time.perf_counter()
    model = retrieval_model()
    if not question_needs_plan(question):
        _record_plan(meter, started, model=None, note="The question was searched as written.")
        return [question]
    try:
        text, body = _complete(model or "", _PROMPT, question)
    except (AnswerError, OSError, TimeoutError) as exc:
        logger.warning("planning failed (%s); searching the question as written", str(exc)[:300])
        _record_plan(
            meter, started, model=model,
            note="Planning failed, so the question was searched as written.",
        )
        return [question]
    prompt, completion = token_counts(body)
    _record_plan(meter, started, model=model, prompt=prompt, completion=completion)
    return parse_queries(text, question)


def _record_plan(
    meter: Meter | None,
    started: float,
    *,
    model: str | None,
    prompt: int | None = None,
    completion: int | None = None,
    note: str | None = None,
) -> None:
    if meter is None:
        return
    meter.add(Stage(
        "Plan the question",
        time.perf_counter() - started,
        prompt_tokens=prompt,
        completion_tokens=completion,
        model=model,
        note=note,
    ))


def parse_queries(text: str, question: str) -> list[str]:
    cleaned = _FENCE.sub("", visible_answer(text).strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return [question]
    raw = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [question]
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        query = item.strip()
        if query and query not in found and _grounded(query, question):
            found.append(query)
    if question not in found:
        found.insert(0, question)
    return found[:_MAX_QUERIES] or [question]


def _grounded(query: str, question: str) -> bool:
    source = _content_words(question)
    terms = _content_words(query)
    if not terms or not terms <= source:
        return False
    return any(len(word) >= 6 and word not in _GENERIC for word in terms)


_GENERIC = {
    "where", "does", "have", "with", "this", "that", "from", "your", "what",
    "when", "which", "code", "decide", "about", "would", "there", "their",
    "should", "could", "please",
}


def _content_words(text: str) -> set[str]:
    return {word for word in re.findall(r"[A-Za-z0-9]+", text.lower()) if len(word) > 2}


def _complete(model: str, system: str, user: str) -> tuple[str, dict]:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if LLM_PROVIDER == "ollama":
        return _complete_ollama(model, messages)
    return _complete_openai(model, messages)


def _complete_openai(model: str, messages: list[dict[str, str]]) -> tuple[str, dict]:
    import urllib.request

    from rag.answer import _post_json

    request = urllib.request.Request(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=json.dumps({"model": model, "temperature": 0, "max_tokens": 300, "messages": messages}).encode(),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    body = _post_json(request, timeout=45, unreachable="Search planning failed")
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AnswerError("Search planning returned an unexpected response.") from exc
    if not isinstance(text, str) or not text.strip():
        raise AnswerError("Search planning returned no text.")
    return text, body


def _complete_ollama(model: str, messages: list[dict[str, str]]) -> tuple[str, dict]:
    import urllib.request

    from rag.answer import _post_json

    root = OLLAMA_BASE_URL[:-3] if OLLAMA_BASE_URL.endswith("/v1") else OLLAMA_BASE_URL
    payload = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 300},
    }
    request = urllib.request.Request(
        f"{root}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body = _post_json(
        request,
        timeout=min(120, OLLAMA_TIMEOUT),
        unreachable=f"Could not reach Ollama at {root} to plan the search",
    )
    message = body.get("message") if isinstance(body, dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not visible_answer(text):
        raise AnswerError("Search planning returned no text.")
    return text, body
