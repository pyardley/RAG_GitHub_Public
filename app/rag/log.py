"""Logs for diagnosing a search. Written to the process output, never a secret."""

from __future__ import annotations

import logging
import sys
import time

logger = logging.getLogger("rag")
logger.addHandler(logging.NullHandler())


def configure_logging() -> None:
    """Send rag logs to standard output once. Later calls leave the setup alone."""
    if any(not isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_retrieval(
    *,
    question: str,
    repos: list[str],
    k: int,
    path: str,
    queries: list[str],
    scopes: list[str],
    excluded: list[str],
    dropped: list[str],
    truncated: bool,
    hits: list,
) -> None:
    """Record how a question was searched and which passages were kept."""
    parts = [
        f"path={path}",
        f"k={k}",
        "repos=" + ("|".join(repos) if repos else "-"),
        "scopes=" + ("|".join(scopes) if scopes else "-"),
        "excluded=" + ("|".join(excluded) if excluded else "-"),
        f"truncated={str(truncated).lower()}",
        f"passages={len(hits)}",
        "queries=" + ("|".join(queries) if queries else "-"),
    ]
    if dropped:
        parts.append("dropped=" + "|".join(dropped))
    parts.append("question=" + repr(_clip(question, 300)))
    logger.info("retrieval %s", " ".join(parts))
    for hit in hits:
        symbol = f" {hit.symbol}" if hit.symbol else ""
        logger.info(
            "hit %.4f %s %s:%s-%s%s | %s",
            hit.score,
            hit.repo,
            hit.path,
            hit.start_line,
            hit.end_line,
            symbol,
            _clip(_first_line(hit.content), 160),
        )


def _first_line(content: str) -> str:
    for line in content.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
