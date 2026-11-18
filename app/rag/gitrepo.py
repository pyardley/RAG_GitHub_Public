"""Shallow-clone a GitHub repository into the repos volume."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from rag.config import GITHUB_TOKEN, REPOS_DIR

_tip_lock = threading.Lock()


def checkout(name: str, url: str, branch: str | None) -> tuple[Path, str, str]:
    """Clone the repository and return (path, commit sha, branch name)."""
    if branch and not _safe_branch(branch):
        raise ValueError(f"Invalid branch name: {branch}")
    destination = _destination(name)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "-c", "safe.directory=*", "clone", "--depth", "1"]
    if branch:
        args.extend(["--branch", branch])
    args.extend([url, str(destination)])
    _run(args)
    sha = _run(["git", "-c", "safe.directory=*", "-C", str(destination), "rev-parse", "HEAD"]).strip()
    checked_out = _run(
        ["git", "-c", "safe.directory=*", "-C", str(destination), "rev-parse", "--abbrev-ref", "HEAD"]
    ).strip()
    return destination, sha, checked_out


def remote_commit(url: str, branch: str | None, timeout: float | None = None) -> str | None:
    """Return the remote commit, or None when the remote cannot be read in time."""
    if branch and not _safe_branch(branch):
        raise ValueError(f"Invalid branch name: {branch}")
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    try:
        output = _run(["git", "-c", "safe.directory=*", "ls-remote", url, ref], timeout=timeout)
    except RuntimeError:
        return None
    lines = output.strip().splitlines()
    if not lines:
        return None
    sha, _ref = lines[0].split(maxsplit=1)
    return sha


def default_branch(url: str) -> str | None:
    """Return the remote HEAD branch name, such as master or main."""
    found = list_branches(url)
    return found["default"] if found else None


def list_branches(url: str) -> dict[str, object] | None:
    """Remote branch names, with the default branch first."""
    try:
        output = _run([
            "git", "-c", "safe.directory=*", "ls-remote", "--symref",
            url, "HEAD", "refs/heads/*",
        ])
    except RuntimeError:
        return None
    return branch_list(output)


def branch_list(output: str) -> dict[str, object] | None:
    """Parse `git ls-remote --symref` output into a default and a name list."""
    default = default_branch_name(output)
    names: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "ref:":
            continue
        ref = parts[1]
        if not ref.startswith("refs/heads/"):
            continue
        name = ref.removeprefix("refs/heads/")
        if not name or name in seen or not _safe_branch(name):
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return None
    if default not in seen:
        default = names[0]
    names.sort(key=lambda name: (name != default, name.lower()))
    return {"default": default, "branches": names}


def default_branch_name(output: str) -> str | None:
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "ref:" and parts[1].startswith("refs/heads/"):
            name = parts[1].removeprefix("refs/heads/")
            if name:
                return name
    return None


def checkout_dir(name: str) -> Path:
    """Local checkout for an indexed repository, whether or not it is still there."""
    return _destination(name)


def tip_checkout(name: str, branch: str, sha: str) -> Path | None:
    """A detached checkout of a branch tip, leaving the indexed checkout where it is.

    The indexed clone stays on the commit that was embedded. A later commit is
    fetched into its own worktree so a journey can be compared without moving
    that commit. ``sha`` is the tip the caller already read from the remote.
    A worktree already at that commit is reused. None means the tip could not
    be fetched.
    """
    if not _safe_branch(branch) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
        return None
    source = checkout_dir(name)
    if not (source / ".git").exists():
        return None
    destination = _tip_dir(name, sha)
    if _at_commit(destination, sha):
        return destination
    with _tip_lock:
        if _at_commit(destination, sha):
            return destination
        try:
            _run(
                ["git", "-c", "safe.directory=*", "-C", str(source), "fetch", "--depth", "1", "origin", branch],
                timeout=60,
            )
            fetched = _run(
                ["git", "-c", "safe.directory=*", "-C", str(source), "rev-parse", "FETCH_HEAD"]
            ).strip()
        except RuntimeError:
            return None
        if fetched != sha and not fetched.startswith(sha) and not sha.startswith(fetched):
            return None
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(
                ["git", "-c", "safe.directory=*", "-C", str(source), "worktree", "prune"],
                timeout=30,
            )
            _run(
                [
                    "git", "-c", "safe.directory=*", "-C", str(source),
                    "worktree", "add", "--detach", str(destination), fetched,
                ],
                timeout=60,
            )
        except RuntimeError:
            shutil.rmtree(destination, ignore_errors=True)
            return None
    return destination if _at_commit(destination, sha) else None


def remove_checkout(name: str) -> bool:
    """Delete the local checkout for one repository. Missing checkouts are fine."""
    destination = _destination(name)
    existed = destination.exists()
    if existed:
        shutil.rmtree(destination)
    parent = destination.parent
    root = REPOS_DIR.resolve()
    if parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return existed


def _destination(name: str) -> Path:
    return _inside_repos(name)


def _tip_dir(name: str, sha: str) -> Path:
    return _inside_repos(".tips", name, sha)


def _inside_repos(*parts: str) -> Path:
    root = REPOS_DIR.resolve()
    destination = root.joinpath(*parts).resolve()
    if root != destination and root not in destination.parents:
        raise RuntimeError(f"Refusing to write outside {root}")
    return destination


def _at_commit(destination: Path, sha: str) -> bool:
    if not destination.is_dir() or not (destination / ".git").exists():
        return False
    try:
        current = _run(
            ["git", "-c", "safe.directory=*", "-C", str(destination), "rev-parse", "HEAD"]
        ).strip()
    except RuntimeError:
        return False
    return current == sha or current.startswith(sha) or sha.startswith(current)


def _run(args: list[str], timeout: float | None = None) -> str:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git did not answer in time") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "git failed").strip()
        if GITHUB_TOKEN:
            message = message.replace(GITHUB_TOKEN, "***")
        raise RuntimeError(message)
    return completed.stdout


def _git_env() -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not GITHUB_TOKEN:
        return env
    # Passed only to this process, so the token is not written into .git/config.
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: bearer {GITHUB_TOKEN}"
    return env


def _safe_branch(branch: str) -> bool:
    if not branch or branch.startswith("-") or ".." in branch:
        return False
    return all(character.isalnum() or character in "._/-" for character in branch)
