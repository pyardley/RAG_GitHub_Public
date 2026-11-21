"""Recent versions of one file, read from a local git clone for the History tab.

The indexed checkout stays on the commit that was embedded. File history is a
separate bare clone of the branch: commit and tree objects only, with each
file version loaded when it is shown. Nothing here calls the GitHub REST API.
"""

from __future__ import annotations

import re
import shutil
import threading
import urllib.parse
from pathlib import Path

from rag.chunker import find_blocks
from rag.files import language_for
from rag.gitrepo import git_bytes, git_text, history_clone_dir, run_git
from rag.log import logger

VERSION_LIMIT = 12
BLOCK_SCAN = 80
TEXT_LIMIT = 60_000
_BLOCK_LANGUAGES = {"ruby", "python", "javascript", "typescript", "java", "gherkin"}
_CLONE_TIMEOUT = 300
_FETCH_TIMEOUT = 300

_BLOB_URL = re.compile(r"https?://github\.com/[^/]+/[^/]+/blob/[^/]+/(.+)$")
_lock = threading.Lock()


class HistoryError(Exception):
    """The file history could not be read."""


def file_history(
    repo: str,
    branch: str,
    path: str,
    indexed_sha: str = "",
    *,
    block: str = "",
    url: str = "",
    root: Path | None = None,
) -> dict:
    """Versions of ``path`` on ``branch``, oldest of the recent window first.

    ``block`` is a method, class, or scenario. The result then keeps only the
    commits that change that block, and each version's text is the block alone.
    ``root`` is a git repository to read instead of the shared history clone.
    Tests pass a local repository. The indexed checkout is never that root.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise ValueError("Repository must be a GitHub name")
    if not _safe_branch(branch):
        raise ValueError("This repository has no branch recorded")
    relative = clean_history_path(path)
    name = clean_block_name(block)
    with _lock:
        checkout = root if root is not None else _ensure_clone(repo, branch, url)
        _update(checkout, branch, relative)
        if name:
            return _block_history(checkout, repo, branch, relative, name, indexed_sha)
        commits = _log(checkout, branch, relative, VERSION_LIMIT + 1)
        if not commits:
            raise HistoryError(f"No commit on {branch} changes {relative}.")
        # A shallow clone may still hide older commits. A full clone longer than
        # the window has older commits that this page does not load.
        truncated = len(commits) > VERSION_LIMIT or _is_shallow(checkout)
        versions = []
        for commit in reversed(commits[:VERSION_LIMIT]):
            text, omitted = _contents(checkout, commit)
            versions.append(_version(repo, commit, indexed_sha, text, omitted))
    return {
        "repo": repo,
        "branch": branch,
        "path": relative,
        "block": "",
        "symbol": "",
        "truncated": truncated,
        "versions": versions,
    }


def clean_block_name(value: str) -> str:
    """A method, class, or scenario name. Blank means the whole file."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Enter the name of a method, class, or scenario")
    text = " ".join(value.split())
    if not text:
        return ""
    if len(text) > 160 or any(ord(character) < 32 for character in text):
        raise ValueError("Enter the name of a method, class, or scenario")
    if not re.search(r"[A-Za-z0-9_]", text):
        raise ValueError("Enter the name of a method, class, or scenario")
    return text


def clean_history_path(value: str) -> str:
    """A repository-relative file path. A GitHub blob URL is reduced to its path."""
    if not isinstance(value, str):
        raise ValueError("Enter a file path inside the repository")
    text = value.strip()
    blob = _BLOB_URL.match(text.split("#", 1)[0].strip())
    if blob:
        text = urllib.parse.unquote(blob.group(1))
    relative = text.replace("\\", "/").strip()
    while relative.startswith("./"):
        relative = relative[2:]
    relative = relative.lstrip("/").rstrip("/")
    parts = relative.split("/")
    if (
        not relative
        or len(relative) > 300
        or any(part in {"", ".", ".."} for part in parts)
        or any(not re.fullmatch(r"[A-Za-z0-9_.@+-]+", part) for part in parts)
    ):
        raise ValueError("Enter a file path inside the repository")
    return relative


def _ensure_clone(name: str, branch: str, url: str) -> Path:
    destination = history_clone_dir(name)
    if (destination / "HEAD").exists():
        return destination
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    remote = _remote_url(name, url)
    logger.info("cloning %s for file history", name)
    try:
        run_git(
            [
                "clone", "--filter=blob:none", "--bare", "--depth", "1",
                "--single-branch", "--branch", branch, "--no-tags",
                remote, str(destination),
            ],
            timeout=_CLONE_TIMEOUT,
        )
    except RuntimeError as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise HistoryError(_git_message(exc)) from exc
    return destination


def _update(repo: Path, branch: str, path: str) -> None:
    """Bring the branch tip in, and fetch older commits when this file needs them."""
    if not (repo / "HEAD").exists() and not (repo / ".git").exists():
        raise HistoryError("The history for this repository is not available.")
    if not _has_origin(repo):
        return
    try:
        if _is_shallow(repo) and len(_log(repo, branch, path, VERSION_LIMIT + 1)) <= VERSION_LIMIT:
            logger.info("fetching older history of %s", branch)
            _fetch(repo, ["--unshallow", "--no-tags", "origin", branch])
            return
        _fetch(repo, ["--no-tags", "origin", branch])
    except HistoryError:
        raise
    except RuntimeError as exc:
        raise HistoryError(_git_message(exc)) from exc


def _log(repo: Path, branch: str, path: str, limit: int) -> list[dict]:
    ref = _history_ref(repo, branch)
    try:
        output = _git(
            repo,
            [
                "log", f"-n{limit}",
                f"--format=%x1e%H%x1f%an%x1f%aI%x1f%s",
                "--name-status", ref, "--", path,
            ],
        )
    except HistoryError:
        return []
    commits = []
    for piece in output.split("\x1e"):
        parsed = _commit(piece)
        if parsed:
            commits.append(parsed)
    return commits


def _commit(piece: str) -> dict | None:
    header, _, body = piece.strip("\n").partition("\n")
    header = header.strip()
    if not header or "\x1f" not in header:
        return None
    sha, author, when, subject = (header.split("\x1f", 3) + ["", "", "", ""])[:4]
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return None
    changed, deleted = _status(body)
    summary = " ".join(subject.split())[:200]
    return {
        "sha": sha,
        "author": author.strip()[:120],
        "date": when.strip(),
        "message": summary or "Commit",
        "path": changed,
        "deleted": deleted,
    }


def _status(body: str) -> tuple[str, bool]:
    path = ""
    deleted = False
    for line in body.splitlines():
        if "\t" not in line:
            continue
        status, _, names = line.partition("\t")
        deleted = status.startswith("D")
        path = names.split("\t")[-1]
    return path, deleted


def _block_history(
    checkout: Path,
    repo: str,
    branch: str,
    path: str,
    name: str,
    indexed_sha: str,
) -> dict:
    language = language_for(Path(path))
    if language not in _BLOCK_LANGUAGES:
        raise HistoryError(
            f"Blocks in {path} cannot be followed. Leave the block blank to show the whole file."
        )
    commits = _log(checkout, branch, path, BLOCK_SCAN + 1)
    if not commits:
        raise HistoryError(f"No commit on {branch} changes {path}.")
    extra = commits[BLOCK_SCAN] if len(commits) > BLOCK_SCAN else None
    window = list(reversed(commits[:BLOCK_SCAN]))
    loaded = {commit["sha"]: _contents(checkout, commit) for commit in window}
    if extra:
        loaded[extra["sha"]] = _contents(checkout, extra)
    previous = _one_block(language, loaded[extra["sha"]][0] if extra else None, name)
    symbol = previous[0] if previous else ""
    present = previous is not None
    versions = []
    for commit in window:
        text, omitted = loaded[commit["sha"]]
        if omitted or text is None:
            current = None
            marker = omitted or "deleted"
        else:
            current = _one_block(language, text, name)
            marker = "deleted"
            if current:
                present = True
                symbol = current[0]
        current_body = current[1] if current else None
        previous_body = previous[1] if previous else None
        if current_body != previous_body and (current is not None or previous is not None):
            if current:
                versions.append(_version(
                    repo, commit, indexed_sha, current[1], None, current[2], current[0],
                ))
            else:
                versions.append(_version(
                    repo, commit, indexed_sha, None, marker, symbol=symbol,
                ))
        previous = current
    if not versions:
        if present:
            raise HistoryError(f"No recent commit changes {name} in {path}.")
        raise HistoryError(f"No commit on {branch} changes {name} in {path}.")
    truncated = len(versions) > VERSION_LIMIT or len(commits) > BLOCK_SCAN or _is_shallow(checkout)
    shown = versions[-VERSION_LIMIT:]
    if not symbol:
        symbol = next((item["symbol"] for item in reversed(shown) if item.get("symbol")), name)
    return {
        "repo": repo,
        "branch": branch,
        "path": path,
        "block": name,
        "symbol": symbol,
        "truncated": truncated,
        "versions": shown,
    }


def _one_block(language: str, text: str | None, name: str) -> tuple[str, str, int] | None:
    if not text:
        return None
    matches = find_blocks(language, text, name)
    if not matches:
        return None
    symbols = sorted({item.symbol for item in matches})
    if len(symbols) > 1:
        raise ValueError(f"That name matches {', '.join(symbols[:6])}. Enter one of those names.")
    match = matches[0]
    return match.symbol, match.content, match.start_line


def _version(
    repo: str,
    commit: dict,
    indexed_sha: str,
    text: str | None,
    omitted: str | None,
    start_line: int | None = None,
    symbol: str = "",
) -> dict:
    payload = {
        "sha": commit["sha"],
        "short": commit["sha"][:7],
        "date": commit["date"],
        "author": commit["author"],
        "message": commit["message"],
        "url": f"https://github.com/{repo}/commit/{commit['sha']}",
        "indexed": _same_commit(commit["sha"], indexed_sha),
        "text": text,
        "omitted": omitted,
    }
    if start_line:
        payload["start_line"] = start_line
    if symbol:
        payload["symbol"] = symbol
    return payload


def _contents(repo: Path, commit: dict) -> tuple[str | None, str | None]:
    if commit["deleted"] or not commit["path"]:
        return None, "deleted"
    spec = f"{commit['sha']}:{commit['path']}"
    try:
        size = int(_git(repo, ["cat-file", "-s", spec], timeout=60).strip())
    except HistoryError:
        return None, "deleted"
    if size > TEXT_LIMIT:
        return None, "too large"
    try:
        data = git_bytes(repo, ["cat-file", "blob", spec], timeout=60)
    except RuntimeError as exc:
        raise HistoryError(_git_message(exc)) from exc
    if b"\0" in data[:8000]:
        return None, "binary"
    return data.decode("utf-8", "replace"), None


def _history_ref(repo: Path, branch: str) -> str:
    remote = f"refs/remotes/origin/{branch}"
    local = f"refs/heads/{branch}"
    if _ref_exists(repo, remote):
        return remote
    if _ref_exists(repo, local):
        return local
    raise HistoryError(f"The branch {branch} is not in the local history.")


def _ref_exists(repo: Path, ref: str) -> bool:
    try:
        git_text(repo, ["rev-parse", "--verify", "--quiet", ref], timeout=15)
    except RuntimeError:
        return False
    return True


def _has_origin(repo: Path) -> bool:
    try:
        return bool(git_text(repo, ["remote", "get-url", "origin"], timeout=15).strip())
    except RuntimeError:
        return False


def _is_shallow(repo: Path) -> bool:
    try:
        return git_text(repo, ["rev-parse", "--is-shallow-repository"], timeout=15).strip() == "true"
    except RuntimeError:
        return False


def _fetch(repo: Path, args: list[str]) -> None:
    """Fetch without blobs when the remote allows it, otherwise fetch normally."""
    try:
        _git(repo, ["fetch", "--filter=blob:none", *args], timeout=_FETCH_TIMEOUT)
    except HistoryError as exc:
        if "filter" not in str(exc).lower():
            raise
        _git(repo, ["fetch", *args], timeout=_FETCH_TIMEOUT)


def _git(repo: Path, args: list[str], timeout: float = 120) -> str:
    try:
        return git_text(repo, args, timeout=timeout)
    except RuntimeError as exc:
        raise HistoryError(_git_message(exc)) from exc


def _remote_url(name: str, url: str) -> str:
    candidate = (url or "").strip()
    if candidate.startswith("https://github.com/") and ".." not in candidate:
        return candidate
    return f"https://github.com/{name}.git"


def _git_message(exc: RuntimeError) -> str:
    line = str(exc).strip().splitlines()
    text = line[0].strip() if line else ""
    return text or "The file history could not be read."


def _same_commit(sha: str, indexed: str) -> bool:
    left = (sha or "").lower()
    right = (indexed or "").lower()
    if not left or not right:
        return False
    return left.startswith(right) or right.startswith(left)


def _safe_branch(branch: str) -> bool:
    if not branch or branch.startswith("-") or ".." in branch:
        return False
    return all(character.isalnum() or character in "._/-" for character in branch)
