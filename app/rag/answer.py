"""Optional written answer from a hosted OpenAI-compatible API or local Ollama."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from rag.config import (
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
)
from rag.search import Hit

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

_QUERY_INSTRUCTIONS = (
    "You answer questions about a software repository using only the retrieved "
    "code passages. Cite file paths and line numbers from the passages. If the "
    "passages do not contain the answer, say what is missing. Do not invent "
    "files, methods, or behaviour."
)
_IMPACT_INSTRUCTIONS = (
    "You assess the impact of changing a named part of a software repository. "
    "Use only the retrieved passages. Organise the answer as: what it is, where "
    "it is defined, what references it (including tests and views), and what to "
    "retest or update if it changes. Cite path and line numbers. If the evidence "
    "is thin, say so. Do not invent files or call sites."
)


class AnswerError(RuntimeError):
    pass


def language_model_configured() -> bool:
    if LLM_PROVIDER == "ollama":
        return bool(OLLAMA_MODEL)
    if LLM_PROVIDER == "openai":
        return bool(OPENAI_API_KEY)
    raise ValueError("LLM_PROVIDER must be openai or ollama")


def active_model(override: str | None = None) -> str:
    if override and override.strip():
        return override.strip()
    if LLM_PROVIDER == "ollama":
        return OLLAMA_MODEL
    return OPENAI_MODEL


def configuration_hint() -> str:
    return (
        "Set LLM_PROVIDER=ollama to use a local Ollama model, "
        "or set OPENAI_API_KEY to use a hosted model."
    )


def answer(question: str, hits: list[Hit], *, impact: bool, model: str | None = None) -> str | None:
    if not hits or not language_model_configured():
        return None
    chosen = active_model(model)
    if LLM_PROVIDER == "ollama":
        return _complete_ollama(question, hits, impact=impact, model=chosen)
    return _complete_openai(question, hits, impact=impact, model=chosen)


def visible_answer(text: str) -> str:
    """Drop a reasoning trace wrapped in think tags and keep the reply."""
    cleaned = _THINK_BLOCK.sub("", text).strip()
    return cleaned


def passage_limits(num_ctx: int) -> tuple[int, int, int]:
    """How many passages fit in an Ollama context window.

    Returns max hits, characters per passage, and total passage characters.
    Code tokenises at roughly three characters per token. Two thousand tokens
    stay reserved for the instructions, the question, and the reply.
    """
    usable_tokens = max(1024, num_ctx - 2048)
    total_chars = usable_tokens * 3
    per_passage = min(2500, total_chars)
    return 8, per_passage, total_chars


def ollama_payload(model: str, question: str, hits: list[Hit], *, impact: bool, num_ctx: int) -> dict:
    max_hits, per_passage, total_chars = passage_limits(num_ctx)
    return {
        "model": model,
        "stream": False,
        "messages": _messages(
            question,
            hits,
            impact=impact,
            max_hits=max_hits,
            per_passage=per_passage,
            total_chars=total_chars,
        ),
        "options": {"temperature": 0.1, "num_ctx": num_ctx, "num_predict": 1024},
    }


def _complete_openai(question: str, hits: list[Hit], *, impact: bool, model: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": _messages(question, hits, impact=impact, max_hits=12, per_passage=3500, total_chars=None),
    }
    request = urllib.request.Request(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    body = _post_json(request, timeout=90, unreachable="Language model request failed")
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AnswerError("Language model returned an unexpected response.") from exc
    return visible_answer(text) or text.strip()


def _complete_ollama(question: str, hits: list[Hit], *, impact: bool, model: str) -> str:
    root = OLLAMA_BASE_URL[:-3] if OLLAMA_BASE_URL.endswith("/v1") else OLLAMA_BASE_URL
    payload = ollama_payload(model, question, hits, impact=impact, num_ctx=OLLAMA_NUM_CTX)
    request = urllib.request.Request(
        f"{root}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body = _post_json(
        request,
        timeout=OLLAMA_TIMEOUT,
        unreachable=f"Could not reach Ollama at {root}. Start Ollama on the host and check OLLAMA_BASE_URL",
    )
    message = body.get("message") if isinstance(body, dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise AnswerError("Ollama returned an unexpected response.")
    cleaned = visible_answer(text)
    if cleaned:
        return cleaned
    raise AnswerError(
        "Ollama returned no answer text. A reasoning model can spend the request on its trace. "
        "qwen2.5-coder:32b-instruct-q5_K_M is a better fit for these questions."
    )


def _messages(
    question: str,
    hits: list[Hit],
    *,
    impact: bool,
    max_hits: int,
    per_passage: int,
    total_chars: int | None,
) -> list[dict[str, str]]:
    instructions = _IMPACT_INSTRUCTIONS if impact else _QUERY_INSTRUCTIONS
    passages = _format_passages(hits, max_hits=max_hits, per_passage=per_passage, total_chars=total_chars)
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": f"Question:\n{question}\n\nRetrieved passages:\n{passages}"},
    ]


def _post_json(request: urllib.request.Request, *, timeout: int, unreachable: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AnswerError(f"Language model request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise AnswerError(f"{unreachable}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise AnswerError("Language model returned an unexpected response.") from exc


def _format_passages(
    hits: list[Hit],
    *,
    max_hits: int,
    per_passage: int,
    total_chars: int | None,
) -> str:
    blocks: list[str] = []
    used = 0
    for hit in hits:
        if len(blocks) >= max_hits:
            break
        body = hit.content if len(hit.content) <= per_passage else hit.content[:per_passage] + "\n..."
        symbol = f" symbol {hit.symbol}" if hit.symbol else ""
        block = f"[{len(blocks) + 1}] {hit.repo} {hit.path}:{hit.start_line}-{hit.end_line}{symbol}\n{body}"
        if total_chars is not None and blocks and used + len(block) > total_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)
