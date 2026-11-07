"""Elapsed time and language-model tokens for one question."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Stage:
    name: str
    seconds: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        total = None
        if self.prompt_tokens is not None or self.completion_tokens is not None:
            total = (self.prompt_tokens or 0) + (self.completion_tokens or 0)
        return {
            "name": self.name,
            "seconds": round(self.seconds, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": total,
            "model": self.model,
            "note": self.note,
        }


@dataclass
class Meter:
    stages: list[Stage] = field(default_factory=list)
    embed_seconds: float = 0.0
    database_seconds: float = 0.0
    searches: int = 0

    def add(self, stage: Stage) -> None:
        self.stages.append(stage)

    def add_search(self, embed_seconds: float, database_seconds: float) -> None:
        self.embed_seconds += embed_seconds
        self.database_seconds += database_seconds
        self.searches += 1

    def finish_retrieval(self, passages: int, *, embedded: bool = True, direct: str | None = None) -> None:
        if embedded:
            note = "Local embedding model. This stage has no language-model token count."
        elif direct == "regular expression":
            note = "The regular expression was matched directly, so nothing was embedded."
        elif direct == "text":
            note = "The text was matched directly, so nothing was embedded."
        else:
            note = "The values were matched directly, so nothing was embedded."
        self.add(Stage(
            "Embed the searches",
            self.embed_seconds,
            note=note,
        ))
        self.add(Stage(
            "Search the index",
            self.database_seconds,
            note=f"{self.searches} searches, {passages} passages kept.",
        ))

    def as_dict(self) -> dict:
        prompt = sum(stage.prompt_tokens or 0 for stage in self.stages)
        completion = sum(stage.completion_tokens or 0 for stage in self.stages)
        return {
            "seconds": round(sum(stage.seconds for stage in self.stages), 3),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "stages": [stage.as_dict() for stage in self.stages],
        }


def token_counts(body: dict) -> tuple[int | None, int | None]:
    """Read prompt and completion counts from an OpenAI or Ollama response."""
    if not isinstance(body, dict):
        return None, None
    usage = body.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if prompt is not None or completion is not None:
            return _count(prompt), _count(completion)
    if "prompt_eval_count" in body or "eval_count" in body:
        return _count(body.get("prompt_eval_count")), _count(body.get("eval_count"))
    return None, None


def _count(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
