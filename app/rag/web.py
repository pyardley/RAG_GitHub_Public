"""Web interface for querying and indexing repositories."""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import psycopg

from rag.answer import (
    AnswerError,
    active_model,
    answer,
    configuration_hint,
    language_model_configured,
    matching_lines,
)
from rag.config import DEFAULT_K, LLM_PROVIDER, resolve_fusion, resolve_index
from rag.db import connect, list_repositories, resolve_repositories
from rag.edges import POSSIBLE_ARROW, prepare_call_edges
from rag.features import matched_step_texts, retrieve_features
from rag.testing import command_phrases, retrieve_testing
from rag.files import parse_repo
from rag.history import HistoryError, file_history
from rag.flows import charts_for, unmapped_message
from rag.gitrepo import checkout_dir, list_branches, remote_commit, tip_checkout
from rag.journey import apply_journey_diff, branch_answers, journey_scopes, mark_visits
from rag.ingest import delete_repository, ingest_lock_held, ingest_repository
from rag.intent import impact_target
from rag.log import logger
from rag.plan import retrieval_model
from rag.search import Hit, impact, retrieve
from rag.usage import Meter, Stage

_PAGE = Path(__file__).with_name("static") / "index.html"
_VENDOR = Path(__file__).with_name("static") / "vendor"
_MAX_BODY = 1_000_000
_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0)
_STARTED_LABEL = _STARTED_AT.strftime("%Y-%m-%d %H:%M UTC")

_FRESHNESS_SECONDS = 600
_FRESHNESS_TIMEOUT = 20
_freshness_lock = threading.Lock()
_freshness: dict[tuple[str, str, str], tuple[float, str]] = {}

_ingest_lock = threading.Lock()
_ingest_state: dict[str, Any] = {
    "running": False,
    "repo": None,
    "log": "",
    "error": None,
    "progress": None,
}


def _vendor_file(path: str) -> Path | None:
    """A layout script under /vendor/, or nothing when the path leaves that folder."""
    prefix = "/vendor/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    folder = _VENDOR.resolve()
    file = (folder / name).resolve()
    if file.parent != folder or not file.is_file():
        return None
    return file


class Handler(BaseHTTPRequestHandler):
    server_version = "CodeRAG/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            page = _PAGE.read_bytes()
            self._send(200, "text/html; charset=utf-8", page)
            return
        vendor = _vendor_file(path)
        if vendor is not None:
            self._send(200, "text/javascript; charset=utf-8", vendor.read_bytes())
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
        if path == "/api/branches":
            repo = parse_qs(urlparse(self.path).query).get("repo", [""])[0]
            status, payload = lookup_branches(repo)
            self._json(payload, status)
            return
        if path == "/api/freshness":
            refresh = parse_qs(urlparse(self.path).query).get("refresh", [""])[0] == "1"
            status, payload = index_freshness(refresh)
            self._json(payload, status)
            return
        self._json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = _read_json(self)
        except ValueError as exc:
            logger.warning("POST %s rejected: %s", path, exc)
            self._json({"error": str(exc)}, 400)
            return
        routes = {
            "/api/query": run_query,
            "/api/search": run_search,
            "/api/impact": run_impact,
            "/api/flow": run_flow,
            "/api/features": run_features,
            "/api/testing": run_testing,
            "/api/history": run_history,
            "/api/ingest": start_ingest,
            "/api/remove": remove_index,
        }
        route = routes.get(path)
        if route is None:
            self._json({"error": "Not found"}, 404)
            return
        try:
            status, payload = route(body)
        except ValueError as exc:
            logger.warning("POST %s rejected: %s", path, exc)
            self._json({"error": str(exc)}, 400)
            return
        except Exception:
            logger.exception("POST %s failed", path)
            self._json({"error": "The request failed while running"}, 500)
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
    try:
        conn = connect()
        try:
            prepare_call_edges(conn)
        finally:
            conn.close()
    except (OSError, SystemExit, RuntimeError) as exc:
        logger.warning("call edges were not built: %s", exc)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Code RAG is at http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        httpd.server_close()


def lookup_default_branch(repo: str) -> tuple[int, dict]:
    status, payload = lookup_branches(repo)
    if status != 200:
        if status == 404:
            return 404, {"error": "The default branch could not be read"}
        return status, payload
    return 200, {"branch": payload["default"]}


def lookup_branches(repo: str) -> tuple[int, dict]:
    try:
        _name, url = parse_repo(repo)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    found = list_branches(url)
    if not found:
        return 404, {"error": "The branches could not be read"}
    return 200, found


def index_freshness(refresh: bool = False) -> tuple[int, dict]:
    """Compare each indexed commit with its branch on GitHub.

    This asks GitHub, so it is kept apart from the status the page polls, and
    an answer is reused for `_FRESHNESS_SECONDS` unless refresh is set.
    """
    try:
        conn = connect()
        try:
            rows = list_repositories(conn)
        finally:
            conn.close()
    except (OSError, SystemExit, RuntimeError) as exc:
        return 503, {"error": _failure_text(exc)}
    keys = {(row["name"], row["branch"], row["commit_sha"]) for row in rows}
    with _freshness_lock:
        for key in [key for key in _freshness if key not in keys]:
            del _freshness[key]

    def remote(row: dict) -> str | None:
        key = (row["name"], row["branch"], row["commit_sha"])
        with _freshness_lock:
            known = _freshness.get(key)
        if known and not refresh and time.monotonic() - known[0] < _FRESHNESS_SECONDS:
            return known[1]
        try:
            _name, url = parse_repo(row["name"])
            sha = remote_commit(url, row["branch"], timeout=_FRESHNESS_TIMEOUT)
        except ValueError:
            sha = None
        # A failed lookup is not kept, so the next request asks again.
        if sha:
            with _freshness_lock:
                _freshness[key] = (time.monotonic(), sha)
        return sha

    with ThreadPoolExecutor(max_workers=4) as pool:
        remotes = list(pool.map(remote, rows))
    return 200, {"repositories": [freshness_row(row, sha) for row, sha in zip(rows, remotes)]}


def freshness_row(row: dict, remote_sha: str | None) -> dict:
    """One repository's indexed commit beside the commit its branch is at now."""
    indexed = str(row["commit_sha"])
    return {
        "name": row["name"],
        "branch": row["branch"],
        "indexed": indexed[:12],
        "remote": remote_sha[:12] if remote_sha else None,
        "stale": bool(remote_sha) and not remote_sha.startswith(indexed),
    }


def public_status() -> tuple[int, dict]:
    try:
        conn = connect()
        try:
            repositories = _repository_rows(conn)
            elsewhere = ingest_lock_held(conn)
        finally:
            conn.close()
    except (OSError, SystemExit, RuntimeError, psycopg.Error) as exc:
        return 503, {"error": _failure_text(exc), "ingest": _ingest_view(), "started": _STARTED_LABEL}
    return 200, {
        "repositories": repositories,
        "model": _model_view(),
        "ingest": _ingest_view(elsewhere),
        "started": _STARTED_LABEL,
    }


def run_query(body: dict) -> tuple[int, dict]:
    question = _required_text(body, "question", "Enter a question")
    nodes, edges = _optional_chart(body)
    if nodes and edges:
        started = time.perf_counter()
        answers = branch_answers(nodes, edges, question)
        if answers:
            meter = Meter()
            meter.add(Stage(
                "Follow the arrows",
                time.perf_counter() - started,
                note=f"{len(answers)} answers from this journey.",
            ))
            return 200, {
                "journey": True,
                "branches": answers,
                "hits": [],
                "queries": [],
                "usage": meter.as_dict(),
            }
    scopes = _journey_scopes(body, nodes)
    target = impact_target(question)
    if target and not nodes:
        status, payload = run_impact({**body, "target": target})
        if status == 200:
            payload["routed"] = "impact"
            payload["target"] = target
        return status, payload
    if nodes and not scopes:
        return 200, {
            "journey": True,
            "scoped": True,
            "hits": [],
            "queries": [],
            "message": "This journey has no files to search.",
        }
    return _retrieve(
        body, question, written=_wants_answer(body), impact_mode=False,
        scopes=scopes, journey=bool(nodes or scopes),
    )


def run_search(body: dict) -> tuple[int, dict]:
    question = _required_text(body, "question", "Enter a search")
    return _retrieve(body, question, written=False, impact_mode=False)


def run_features(body: dict) -> tuple[int, dict]:
    question = _required_text(body, "question", "Enter a feature or a step")
    repo = _optional_repo(body)
    count = _passage_count(body)
    meter = Meter()
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
        hits, queries, truncated, drafting = retrieve_features(
            conn, [item["id"] for item in repositories], question, count, meter,
        )
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    phrases = matched_step_texts(question)
    text, answer_error = _written_answer(
        question, hits, impact_mode=False, enabled=_wants_answer(body), meter=meter,
        draft=drafting, phrases=phrases,
    )
    return 200, {
        "repositories": [_scope(item) for item in repositories],
        "queries": queries,
        "index": "code",
        "fusion": "rrf",
        "drafting": drafting,
        "truncated": truncated,
        "passage_limit": count,
        "hits": [_hit(item, _indexed_commits(repositories)) for item in hits],
        "matches": [
            {"path": path, "line": line, "text": line_text}
            for path, line, line_text in matching_lines(question, hits, phrases)
        ],
        "answer": text,
        "answer_error": answer_error,
        "usage": meter.as_dict(),
        "model": _model_view(),
    }


def run_testing(body: dict) -> tuple[int, dict]:
    question = _required_text(body, "question", "Enter a test to run or debug")
    repo = _optional_repo(body)
    count = _passage_count(body)
    meter = Meter()
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
        hits, queries, truncated = retrieve_testing(
            conn, [item["id"] for item in repositories], question, count, meter,
        )
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    phrases = command_phrases(question)
    text, answer_error = _written_answer(
        question, hits, impact_mode=False, enabled=_wants_answer(body), meter=meter,
        phrases=phrases, testing=True,
    )
    return 200, {
        "repositories": [_scope(item) for item in repositories],
        "queries": queries,
        "index": "code",
        "fusion": "rrf",
        "testing": True,
        "truncated": truncated,
        "passage_limit": count,
        "hits": [_hit(item, _indexed_commits(repositories)) for item in hits],
        "matches": [
            {"path": path, "line": line, "text": line_text}
            for path, line, line_text in matching_lines(question, hits, phrases)
        ],
        "answer": text,
        "answer_error": answer_error,
        "usage": meter.as_dict(),
        "model": _model_view(),
    }


def run_impact(body: dict) -> tuple[int, dict]:
    target = _required_text(body, "target", "Enter a class, method, or file")
    repo = _optional_repo(body)
    meter = Meter()
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
        started = time.perf_counter()
        found = impact(conn, [item["id"] for item in repositories], target)
        meter.add(Stage("Plan the question", 0.0, note=f"Impact lookup for {target}."))
        meter.add_search(0.0, time.perf_counter() - started)
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    passages = (
        int(found.get("definition_count") or len(found.get("definitions") or []))
        + int(found.get("reference_count") or len(found.get("references") or []))
        + len(found.get("gaps") or [])
        + len(found.get("related") or [])
    )
    meter.finish_retrieval(passages, embedded=False, direct="impact")
    evidence = (
        found["definitions"][:8]
        + found["references"][:8]
        + found.get("gaps", [])[:4]
        + found["related"][:4]
    )
    written, answer_error = _written_answer(
        target, evidence, impact_mode=True, enabled=_wants_answer(body), meter=meter,
    )
    commits = _indexed_commits(repositories)
    return 200, {
        "repositories": [_scope(item) for item in repositories],
        "definitions": [_hit(item, commits) for item in found["definitions"]],
        "references": [_hit(item, commits) for item in found["references"]],
        "related": [_hit(item, commits) for item in found["related"]],
        "gaps": [_hit(item, commits) for item in found.get("gaps", [])],
        "definition_count": found["definition_count"],
        "reference_count": found["reference_count"],
        "truncated": found["truncated"],
        "callers_omitted": found.get("callers_omitted", 0),
        "callers_capped": found.get("callers_capped", False),
        "possible_count": found.get("possible_count", 0),
        "diagram": found.get("diagram") or {"nodes": [], "edges": []},
        "answer": written,
        "answer_error": answer_error,
        "usage": meter.as_dict(),
        "model": _model_view(),
    }


def run_history(body: dict) -> tuple[int, dict]:
    """Recent versions of one file on the indexed repository's branch."""
    repo = _optional_repo(body)
    path = _required_text(body, "path", "Enter a file path")
    if not repo:
        raise ValueError("Choose one repository")
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    if len(repositories) != 1:
        raise ValueError("Choose one repository")
    item = repositories[0]
    started = time.perf_counter()
    try:
        history = file_history(
            item["name"],
            str(item.get("branch") or ""),
            path,
            str(item.get("commit_sha") or ""),
            block=_optional_block(body),
            url=str(item.get("url") or ""),
        )
    except HistoryError as exc:
        logger.warning("history failed for %s: %s", item.get("name"), exc)
        return 502, {"error": str(exc)}
    versions = history.get("versions") or []
    meter = Meter()
    count = len(versions)
    if count == 1:
        note = "1 version."
    elif count:
        note = f"{count} versions."
    else:
        note = "No versions were returned."
    meter.add(Stage("Read the file history", time.perf_counter() - started, note=note))
    return 200, {"repositories": [_scope(item)], **history, "usage": meter.as_dict()}


def run_flow(body: dict) -> tuple[int, dict]:
    """Draw the main journeys in the selected repositories."""
    repo = _optional_repo(body)
    start = _optional_start(body)
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    charts: list[dict] = []
    missing: list[str] = []
    unmapped: list[str] = []
    missed_start: list[str] = []
    read_seconds = 0.0
    compare_seconds = 0.0
    for item in repositories:
        root = checkout_dir(item["name"])
        if not root.is_dir():
            missing.append(item["name"])
            continue
        started = time.perf_counter()
        built = charts_for(root, start)
        followed = False
        for chart in built:
            if chart.pop("reader", None) == "decision":
                followed = True
            mark_visits(root, chart)
        read_seconds += time.perf_counter() - started
        if start and not followed:
            missed_start.append(item["name"])
        if not built:
            if not start:
                unmapped.append(item["name"])
            continue
        started = time.perf_counter()
        _compare_branch(item, built, start)
        compare_seconds += time.perf_counter() - started
        sha = str(item["commit_sha"])
        for chart in built:
            chart["repository"] = item["name"]
            _attach_flow_urls(chart, item["name"], sha)
            charts.append(chart)
    if len(repositories) > 1:
        for chart in charts:
            chart["title"] = f"{chart['repository']} — {chart['title']}"
    message = _flow_message(missing, unmapped, missed_start, start, bool(charts))
    meter = Meter()
    count = len(charts)
    if count == 1:
        journey_note = "1 journey."
    elif count:
        journey_note = f"{count} journeys."
    else:
        journey_note = "No journey was found."
    meter.add(Stage("Read the journeys", read_seconds, note=journey_note))
    meter.add(Stage(
        "Compare the branch",
        compare_seconds,
        note="The indexed commit was compared with the branch.",
    ))
    return 200, {
        "repositories": [_scope(item) for item in repositories],
        "charts": charts,
        "message": message,
        "usage": meter.as_dict(),
    }


def _compare_branch(item: dict, charts: list[dict], start: str | None) -> None:
    """When the branch has moved, record how this journey differs at the tip."""
    indexed = str(item.get("commit_sha") or "")
    remote = _remote_sha(item)
    if not remote or remote == indexed or indexed.startswith(remote) or remote.startswith(indexed):
        return
    try:
        tip = tip_checkout(item["name"], str(item.get("branch") or ""), remote)
    except (OSError, RuntimeError) as exc:
        logger.warning("branch tip for %s was not fetched: %s", item.get("name"), exc)
        return
    if tip is None:
        return
    try:
        later = charts_for(tip, start)
    except (OSError, RuntimeError) as exc:
        logger.warning("branch tip for %s could not be read: %s", item.get("name"), exc)
        return
    for chart in later:
        chart.pop("reader", None)
    apply_journey_diff(charts, later, remote)


def _remote_sha(item: dict) -> str | None:
    """The commit the indexed branch points at, reused from the freshness cache."""
    key = (item["name"], item.get("branch"), str(item.get("commit_sha")))
    with _freshness_lock:
        known = _freshness.get(key)
    if known and time.monotonic() - known[0] < _FRESHNESS_SECONDS:
        return known[1]
    url = item.get("url")
    if not url:
        try:
            _name, url = parse_repo(item["name"])
        except ValueError:
            return None
    try:
        sha = remote_commit(url, item.get("branch"), timeout=_FRESHNESS_TIMEOUT)
    except (ValueError, RuntimeError):
        return None
    if sha:
        with _freshness_lock:
            _freshness[key] = (time.monotonic(), sha)
    return sha


def _attach_flow_urls(chart: dict, repo: str, commit_sha: str) -> None:
    for node in chart.get("nodes") or []:
        url = github_blob_url(
            repo,
            commit_sha,
            str(node.get("path") or ""),
            int(node.get("start_line") or 1),
            int(node.get("end_line") or 1),
        )
        if url:
            node["url"] = url


def _flow_message(
    missing: list[str],
    unmapped: list[str],
    missed_start: list[str],
    start: str | None,
    drew: bool,
) -> str:
    parts: list[str] = []
    if missing:
        names = ", ".join(missing)
        parts.append(
            f"The checkout for {names} is not on disk, so its journey cannot be drawn. "
            "Index it again to restore the checkout."
        )
    if missed_start and start:
        names = ", ".join(missed_start)
        parts.append(f"No step at {start} was found in {names}.")
    if unmapped:
        parts.append(unmapped_message(unmapped))
    if not drew and not parts:
        parts.append(unmapped_message([]))
    return " ".join(parts)


def _optional_chart(body: dict) -> tuple[list[dict], list[dict]]:
    """Nodes and edges sent back with a question about the chart on screen."""
    if "nodes" not in body and "edges" not in body:
        return [], []
    nodes = body.get("nodes")
    edges = body.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("Journey must be a chart")
    if len(nodes) > 400 or len(edges) > 800:
        raise ValueError("Journey is too large")
    clean_nodes: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id or len(node_id) > 200 or ".." in node_id:
            continue
        url = str(node.get("url") or "")
        clean_nodes.append({
            "id": node_id,
            "label": str(node.get("label") or "")[:200],
            "path": _chart_path(node.get("path")),
            "url": url if url.startswith("https://github.com/") else "",
        })
    clean_edges: list[dict] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "").strip()
        target = str(edge.get("to") or "").strip()
        if not source or not target:
            continue
        clean_edges.append({
            "from": source[:200],
            "to": target[:200],
            "label": str(edge.get("label") or "")[:200],
        })
    return clean_nodes, clean_edges


def _journey_scopes(body: dict, nodes: list[dict]) -> list[str] | None:
    if nodes:
        repo = body.get("repo")
        root = None
        if isinstance(repo, str) and repo.strip():
            try:
                root = checkout_dir(parse_repo(repo)[0])
            except ValueError:
                root = None
            if root is not None and not root.is_dir():
                root = None
        return journey_scopes(nodes, root)
    raw = body.get("paths")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("Paths must be a list")
    scopes: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        path = _chart_path(item)
        if path and path not in scopes:
            scopes.append(path)
        if len(scopes) >= 400:
            break
    return scopes


def _chart_path(value: object) -> str:
    if not isinstance(value, str):
        return ""
    relative = value.replace("\\", "/").strip().lstrip("/")
    parts = relative.split("/")
    if not relative or len(relative) > 300 or any(part in {"", ".", ".."} for part in parts):
        return ""
    return relative


def _optional_start(body: dict) -> str | None:
    start = body.get("start")
    if start is None:
        return None
    if not isinstance(start, str):
        raise ValueError("Start path must be text")
    return start.strip() or None


def start_ingest(body: dict) -> tuple[int, dict]:
    repo = _required_text(body, "repo", "Enter a GitHub repository")
    parse_repo(repo)
    branch = body.get("branch") or None
    if branch is not None:
        if not isinstance(branch, str):
            raise ValueError("Branch must be text")
        branch = branch.strip() or None
    force = bool(body.get("force"))
    if _ingest_elsewhere():
        return 409, {"error": "Another process is building an index", "ingest": _ingest_view(True)}
    with _ingest_lock:
        busy = _ingest_state["running"]
        if not busy:
            _ingest_state.update(running=True, repo=repo.strip(), log="", error=None, progress=None)
    if busy:
        return 409, {"error": "An ingest is already running", "ingest": _ingest_view()}
    threading.Thread(target=_run_ingest, args=(repo.strip(), branch, force), daemon=True).start()
    return 202, {"ingest": _ingest_view()}


def remove_index(body: dict) -> tuple[int, dict]:
    repo = _required_text(body, "repo", "Choose a repository")
    with _ingest_lock:
        busy = _ingest_state["running"]
    if busy:
        return 409, {"error": "An ingest is already running", "ingest": _ingest_view()}
    try:
        removed = delete_repository(repo)
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    ingested = removed["ingested_at"]
    return 200, {
        "removed": {
            "name": removed["name"],
            "branch": removed["branch"],
            "commit_sha": str(removed["commit_sha"])[:12],
            "ingested_at": ingested.isoformat() if isinstance(ingested, datetime) else str(ingested),
            "files": removed["files"],
            "chunks": removed["chunks"],
        },
        "repositories": _repository_rows(None),
    }


def _retrieve(
    body: dict,
    question: str,
    *,
    written: bool,
    impact_mode: bool,
    scopes: list[str] | None = None,
    journey: bool = False,
) -> tuple[int, dict]:
    repo = _optional_repo(body)
    count = _passage_count(body)
    index = resolve_index(body["index"] if "index" in body else None)
    fusion = resolve_fusion(body["fusion"] if "fusion" in body else None)
    meter = Meter()
    try:
        conn = connect()
    except SystemExit as exc:
        return 503, {"error": _failure_text(exc)}
    try:
        repositories = resolve_repositories(conn, repo)
        hits, queries, truncated = retrieve(
            conn, [item["id"] for item in repositories], question, count, meter,
            index=index, fusion=fusion, scopes=scopes,
        )
    except SystemExit as exc:
        return 400, {"error": _failure_text(exc)}
    finally:
        conn.close()
    text, answer_error = _written_answer(
        question, hits, impact_mode=impact_mode, enabled=written, meter=meter,
    )
    payload = {
        "repositories": [_scope(item) for item in repositories],
        "queries": queries,
        "index": index,
        "fusion": fusion,
        "truncated": truncated,
        "passage_limit": count,
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
    if journey:
        payload["journey"] = True
        payload["scoped"] = bool(scopes)
    return 200, payload


def _written_answer(
    question: str,
    hits: list[Hit],
    *,
    impact_mode: bool,
    enabled: bool,
    meter: Meter | None = None,
    draft: bool = False,
    phrases: list[str] | None = None,
    testing: bool = False,
) -> tuple[str | None, str | None]:
    if not enabled or not hits or not language_model_configured():
        return None, None
    try:
        return answer(
            question, hits, impact=impact_mode, meter=meter, draft=draft, phrases=phrases,
            testing=testing,
        ), None
    except AnswerError as exc:
        logger.warning("answer failed: %s", str(exc)[:300])
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
        if error:
            logger.warning("ingest failed for %s: %s", repo, error)
        with _ingest_lock:
            _ingest_state["running"] = False
            _ingest_state["log"] = buffer.getvalue()[-20000:]
            _ingest_state["error"] = error


def _repository_rows(conn) -> list[dict]:
    if conn is None:
        own = connect()
        try:
            rows = list_repositories(own)
        finally:
            own.close()
    else:
        rows = list_repositories(conn)
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


def _ingest_elsewhere() -> bool:
    """True when another process, such as a command-line ingest, holds the ingest lock."""
    with _ingest_lock:
        if _ingest_state["running"]:
            return False
    try:
        conn = connect()
    except SystemExit:
        return False
    try:
        return ingest_lock_held(conn)
    except psycopg.Error:
        return False
    finally:
        conn.close()


def _ingest_view(elsewhere: bool = False) -> dict:
    with _ingest_lock:
        progress = _progress_view(_ingest_state["progress"])
        return {
            "running": _ingest_state["running"],
            "elsewhere": bool(elsewhere) and not _ingest_state["running"],
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
    if hit.via:
        payload["via"] = hit.via
        if POSSIBLE_ARROW in hit.via:
            payload["possible"] = True
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


def _optional_block(body: dict) -> str:
    value = body.get("block", "")
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("Enter the name of a method, class, or scenario")
    return value


def _required_text(body: dict, key: str, message: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()


def _wants_answer(body: dict) -> bool:
    return body.get("answer", True) is not False


def _passage_count(body: dict) -> int:
    count = body.get("k", DEFAULT_K)
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
