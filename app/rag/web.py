"""Web interface for querying and indexing repositories."""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from rag.answer import (
    AnswerError,
    active_model,
    answer,
    configuration_hint,
    language_model_configured,
    matching_lines,
)
from rag.config import LLM_PROVIDER
from rag.db import connect, list_repositories, resolve_repositories
from rag.files import parse_repo
from rag.gitrepo import default_branch
from rag.ingest import ingest_repository
from rag.plan import retrieval_model
from rag.search import Hit, impact, retrieve
from rag.usage import Meter

_PAGE = Path(__file__).with_name("static") / "index.html"
_MAX_BODY = 1_000_000
_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0)
_STARTED_LABEL = _STARTED_AT.strftime("%Y-%m-%d %H:%M UTC")

_ingest_lock = threading.Lock()
_ingest_state: dict[str, Any] = {
    "running": False,
    "repo": None,
    "log": "",
    "error": None,
    "progress": None,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodeRAG/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            page = _PAGE.read_bytes()
            self._send(200, "text/html; charset=utf-8", page)
            return
        if path == "/api/status":
            status, payload = public_status()
            self._json(payload, status)
            return
        if path == "/api/default-branch":
            repo = parse_qs(urlparse(self.path).query).get("repo", [""])[0]
            status, payload = lookup_default_branch(repo)
            self._json(payload, status)
            return
        self._json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = _read_json(self)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        routes = {
            "/api/query": run_query,
            "/api/search": run_search,
            "/api/impact": run_impact,
            "/api/ingest": start_ingest,
        }
        route = routes.get(path)
        if route is None:
            self._json({"error": "Not found"}, 404)
            return
        try:
            status, payload = route(body)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json(payload, status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int) -> None:
    if not _PAGE.is_file():
        raise SystemExit(f"Web page is missing: {_PAGE}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Code RAG is at http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        httpd.server_close()


def lookup_default_branch(repo: str) -> tuple[int, dict]:
    try:
        _name, url = parse_repo(repo)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    branch = default_branch(url)
    if not branch:
        return 404, {"error": "The default branch could not be read"}
    return 200, {"branch": branch}


def public_status() -> tuple[int, dict]:
    try:
        repositories = _repository_rows()
    except (OSError, SystemExit, RuntimeError) as exc:
        return 503, {"error": _failure_text(exc), "ingest": _ingest_view(), "started": _STARTED_LABEL}
    return 200, {
        "repositories": repositories,
        "model": _model_view(),
        "ingest": _ingest_view(),
        "started": _STARTED_LABEL,
    }


def run_query(body: dict) -> tuple[int, dict]:
    question = _required_text(body, "question", "Enter a question")
    return _retrieve(body, question, written=_wants_answer(body), impact_mode=False)


def run_search(body: dict) -> tuple[int, dict]:
    question = _required_text(body, "question", "Enter a search")
    return _retrieve(body, question, written=False, impact_mode=False)


def run_impact(body: dict) -> tuple[int, dict]:
    target = _required_text(body, "target", "Enter a class, method, or file")
    repo = _optional_repo(body)
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
        found = impact(conn, [item["id"] for item in repositories], target)
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    evidence = found["definitions"][:8] + found["references"][:8] + found["related"][:4]
    written, answer_error = _written_answer(target, evidence, impact_mode=True, enabled=_wants_answer(body))
    commits = _indexed_commits(repositories)
    return 200, {
        "repositories": [_scope(item) for item in repositories],
        "definitions": [_hit(item, commits) for item in found["definitions"]],
        "references": [_hit(item, commits) for item in found["references"]],
        "related": [_hit(item, commits) for item in found["related"]],
        "definition_count": found["definition_count"],
        "reference_count": found["reference_count"],
        "truncated": found["truncated"],
        "answer": written,
        "answer_error": answer_error,
        "model": _model_view(),
    }


def start_ingest(body: dict) -> tuple[int, dict]:
    repo = _required_text(body, "repo", "Enter a GitHub repository")
    parse_repo(repo)
    branch = body.get("branch") or None
    if branch is not None:
        if not isinstance(branch, str):
            raise ValueError("Branch must be text")
        branch = branch.strip() or None
    force = bool(body.get("force"))
    with _ingest_lock:
        if _ingest_state["running"]:
            return 409, {"error": "An ingest is already running", "ingest": _ingest_view()}
        _ingest_state.update(running=True, repo=repo.strip(), log="", error=None, progress=None)
    threading.Thread(target=_run_ingest, args=(repo.strip(), branch, force), daemon=True).start()
    return 202, {"ingest": _ingest_view()}


def _retrieve(body: dict, question: str, *, written: bool, impact_mode: bool) -> tuple[int, dict]:
    repo = _optional_repo(body)
    count = _passage_count(body)
    meter = Meter()
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
        hits, queries = retrieve(conn, [item["id"] for item in repositories], question, count, meter)
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    text, answer_error = _written_answer(
        question, hits, impact_mode=impact_mode, enabled=written, meter=meter,
    )
    return 200, {
        "repositories": [_scope(item) for item in repositories],
        "queries": queries,
        "hits": [_hit(item, _indexed_commits(repositories)) for item in hits],
        "matches": [
            {"path": path, "line": line, "text": text}
            for path, line, text in matching_lines(question, hits)
        ],
        "answer": text,
        "answer_error": answer_error,
        "usage": meter.as_dict(),
        "model": _model_view(),
    }


def _written_answer(
    question: str,
    hits: list[Hit],
    *,
    impact_mode: bool,
    enabled: bool,
    meter: Meter | None = None,
) -> tuple[str | None, str | None]:
    if not enabled or not hits or not language_model_configured():
        return None, None
    try:
        return answer(question, hits, impact=impact_mode, meter=meter), None
    except AnswerError as exc:
        return None, str(exc)


def _run_ingest(repo: str, branch: str | None, force: bool) -> None:
    buffer = io.StringIO()
    error: str | None = None
    try:
        with contextlib.redirect_stdout(buffer):
            code = ingest_repository(repo, branch, force, progress=_report_progress)
        if code != 0:
            error = "Ingest failed"
    except SystemExit as exc:
        error = _failure_text(exc)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
    finally:
        with _ingest_lock:
            _ingest_state["running"] = False
            _ingest_state["log"] = buffer.getvalue()[-20000:]
            _ingest_state["error"] = error


def _repository_rows() -> list[dict]:
    conn = connect()
    try:
        rows = list_repositories(conn)
    finally:
        conn.close()
    published = []
    for row in rows:
        ingested = row["ingested_at"]
        published.append(
            {
                "name": row["name"],
                "branch": row["branch"],
                "commit_sha": row["commit_sha"][:12],
                "ingested_at": ingested.isoformat() if isinstance(ingested, datetime) else str(ingested),
                "files": row["files"],
                "chunks": row["chunks"],
            }
        )
    return published


def _model_view() -> dict:
    configured = language_model_configured()
    return {
        "configured": configured,
        "provider": LLM_PROVIDER,
        "name": active_model() if configured else None,
        "retrieval": retrieval_model() if configured else None,
        "hint": None if configured else configuration_hint(),
    }


def _report_progress(update: dict) -> None:
    with _ingest_lock:
        _ingest_state["progress"] = dict(update)


def _ingest_view() -> dict:
    with _ingest_lock:
        progress = _progress_view(_ingest_state["progress"])
        return {
            "running": _ingest_state["running"],
            "repo": _ingest_state["repo"],
            "log": _ingest_state["log"],
            "error": _ingest_state["error"],
            "progress": progress,
        }


def _progress_view(progress: dict | None) -> dict | None:
    if not progress:
        return None
    total = int(progress.get("total") or 0)
    done = int(progress.get("done") or 0)
    percent = None if total <= 0 else min(100, round(100 * done / total))
    return {
        "phase": progress.get("phase") or "index",
        "done": done,
        "total": total,
        "chunks": int(progress.get("chunks") or 0),
        "label": progress.get("label") or "",
        "percent": percent,
    }


def _hit(hit: Hit, commits: dict[str, str]) -> dict:
    payload = {
        "repo": hit.repo,
        "path": hit.path,
        "start_line": hit.start_line,
        "end_line": hit.end_line,
        "symbol": hit.symbol,
        "kind": hit.kind,
        "content": hit.content,
        "score": round(hit.score, 4),
    }
    url = github_blob_url(hit.repo, commits.get(hit.repo, ""), hit.path, hit.start_line, hit.end_line)
    if url:
        payload["url"] = url
    return payload


def github_blob_url(repo: str, commit_sha: str, path: str, start_line: int, end_line: int) -> str | None:
    """Link to the indexed lines. A short or full commit keeps the link stable."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit_sha or ""):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return None
    if start_line < 1 or end_line < 1:
        return None
    parts = path.replace("\\", "/").split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    quoted = "/".join(quote(part, safe="") for part in parts)
    anchor = f"#L{start_line}" if end_line <= start_line else f"#L{start_line}-L{end_line}"
    return f"https://github.com/{repo}/blob/{commit_sha}/{quoted}{anchor}"


def _indexed_commits(repositories: list[dict]) -> dict[str, str]:
    return {repo["name"]: str(repo["commit_sha"]) for repo in repositories}


def _scope(repo: dict) -> dict:
    return {
        "name": repo["name"],
        "branch": repo["branch"],
        "commit_sha": str(repo["commit_sha"])[:12],
    }


def _optional_repo(body: dict) -> str | None:
    repo = body.get("repo")
    if repo is None:
        return None
    if not isinstance(repo, str):
        raise ValueError("Repository must be a name or GitHub URL")
    return repo.strip() or None


def _required_text(body: dict, key: str, message: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()


def _wants_answer(body: dict) -> bool:
    return body.get("answer", True) is not False


def _passage_count(body: dict) -> int:
    count = body.get("k", 8)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 30:
        raise ValueError("Passages must be a number from 1 to 30")
    return count


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length_header = handler.headers.get("Content-Length", "0")
    try:
        length = int(length_header)
    except ValueError as exc:
        raise ValueError("Expected a JSON body") from exc
    if length < 0 or length > _MAX_BODY:
        raise ValueError("Request is too large")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        raise ValueError("Expected a JSON body")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Expected a JSON body") from exc
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def _failure_text(exc: BaseException) -> str:
    code = exc.code if isinstance(exc, SystemExit) else None
    if isinstance(code, str) and code.strip():
        return code.strip()
    text = str(exc).strip()
    return text or "Request failed"
