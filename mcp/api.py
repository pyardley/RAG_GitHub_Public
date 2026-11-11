"""Calls the running Code RAG web API and shortens the passages for a model."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_CONTENT_CHARS = 1500
_TIMEOUT = 120


class RagError(Exception):
    """The app refused the call or could not be reached."""


def base_url() -> str:
    return os.environ.get("CODE_RAG_URL", "http://127.0.0.1:8080").rstrip("/")


def repositories() -> dict:
    payload = _request("GET", "/api/status")
    ingest = payload.get("ingest") or {}
    return {
        "repositories": payload.get("repositories") or [],
        "model": payload.get("model"),
        "started": payload.get("started"),
        "ingest": {
            "running": bool(ingest.get("running")),
            "repo": ingest.get("repo"),
            "error": ingest.get("error"),
            "progress": ingest.get("progress"),
        },
    }


def query(question: str, repo: str | None = None, k: int = 8, answer: bool = False) -> dict:
    body = _lookup_body(question, "question", repo, k)
    body["answer"] = bool(answer)
    return _passages(_request("POST", "/api/query", body), include_queries=True)


def search(question: str, repo: str | None = None, k: int = 8) -> dict:
    body = _lookup_body(question, "question", repo, k)
    return _passages(_request("POST", "/api/search", body), include_queries=True)


def impact(target: str, repo: str | None = None, answer: bool = False) -> dict:
    text = _required(target, "Enter a class, method, or file")
    body: dict = {"target": text, "answer": bool(answer)}
    if repo and repo.strip():
        body["repo"] = repo.strip()
    payload = _request("POST", "/api/impact", body)
    result = {
        "repositories": payload.get("repositories") or [],
        "definitions": [_passage(hit) for hit in payload.get("definitions") or []],
        "references": [_passage(hit) for hit in payload.get("references") or []],
        "gaps": [_passage(hit) for hit in payload.get("gaps") or []],
        "related": [_passage(hit) for hit in payload.get("related") or []],
        "diagram": payload.get("diagram") or {"nodes": [], "edges": []},
        "definition_count": payload.get("definition_count", 0),
        "reference_count": payload.get("reference_count", 0),
        "callers_omitted": payload.get("callers_omitted", 0),
        "callers_capped": payload.get("callers_capped", False),
        "possible_count": payload.get("possible_count", 0),
        "truncated": payload.get("truncated", False),
    }
    _keep_answer(result, payload)
    return result


def default_branch(repo: str) -> dict:
    name = _required(repo, "Enter a GitHub repository")
    quoted = urllib.request.quote(name, safe="")
    return _request("GET", f"/api/default-branch?repo={quoted}")


def branches(repo: str) -> dict:
    name = _required(repo, "Enter a GitHub repository")
    quoted = urllib.request.quote(name, safe="")
    payload = _request("GET", f"/api/branches?repo={quoted}")
    return {"default": payload.get("default"), "branches": payload.get("branches") or []}


def ingest(repo: str, branch: str | None = None, force: bool = False) -> dict:
    name = _required(repo, "Enter a GitHub repository")
    body: dict = {"repo": name, "force": bool(force)}
    if branch and branch.strip():
        body["branch"] = branch.strip()
    payload = _request("POST", "/api/ingest", body)
    return {"ingest": _ingest(payload.get("ingest") or {})}


def remove_index(repo: str, confirm: bool = False) -> dict:
    if confirm is not True:
        raise RagError("Set confirm to true to delete that repository index and its checkout.")
    name = _required(repo, "Choose a repository")
    payload = _request("POST", "/api/remove", {"repo": name})
    return {
        "removed": payload.get("removed"),
        "repositories": payload.get("repositories") or [],
    }


def _lookup_body(question: str, key: str, repo: str | None, k: int) -> dict:
    body: dict = {key: _required(question, "Enter a question"), "k": _passages_count(k)}
    if repo and repo.strip():
        body["repo"] = repo.strip()
    return body


def _passages(payload: dict, *, include_queries: bool) -> dict:
    result = {
        "repositories": payload.get("repositories") or [],
        "passages": [_passage(hit) for hit in payload.get("hits") or []],
        "matches": payload.get("matches") or [],
        "truncated": payload.get("truncated", False),
        "passage_limit": payload.get("passage_limit"),
    }
    if include_queries and payload.get("queries"):
        result["queries"] = payload["queries"]
    _keep_answer(result, payload)
    return result


def _keep_answer(result: dict, payload: dict) -> None:
    if payload.get("answer"):
        result["answer"] = payload["answer"]
    if payload.get("answer_error"):
        result["answer_error"] = payload["answer_error"]


def _passage(hit: dict) -> dict:
    content = str(hit.get("content") or "")
    shortened = len(content) > _CONTENT_CHARS
    if shortened:
        content = content[:_CONTENT_CHARS].rstrip() + "\n…"
    item = {
        "repo": hit.get("repo"),
        "path": hit.get("path"),
        "start_line": hit.get("start_line"),
        "end_line": hit.get("end_line"),
        "symbol": hit.get("symbol"),
        "content": content,
    }
    if shortened:
        item["content_shortened"] = True
    if hit.get("via"):
        item["via"] = hit["via"]
    if hit.get("possible"):
        item["possible"] = True
    if hit.get("url"):
        item["url"] = hit["url"]
    score = hit.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and score:
        item["score"] = score
    return item


def _ingest(ingest: dict) -> dict:
    return {
        "running": bool(ingest.get("running")),
        "repo": ingest.get("repo"),
        "error": ingest.get("error"),
        "progress": ingest.get("progress"),
    }


def _passages_count(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 30:
        raise RagError("Passages must be a number from 1 to 30")
    return k


def _required(value: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RagError(message)
    return value.strip()


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = base_url() + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = _json_object(raw)
        message = payload.get("error") if isinstance(payload, dict) else None
        raise RagError(str(message or f"The app returned {exc.code}")) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RagError(
            f"Code RAG is not reachable at {base_url()}. Start it with docker compose up -d. {reason}"
        ) from exc
    payload = _json_object(raw)
    if not isinstance(payload, dict):
        raise RagError("The app returned a response that was not a JSON object")
    if payload.get("error"):
        raise RagError(str(payload["error"]))
    return payload


def _json_object(raw: str) -> dict | None:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
