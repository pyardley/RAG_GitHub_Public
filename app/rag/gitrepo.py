"""Shallow-clone a GitHub repository into the repos volume."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rag.config import GITHUB_TOKEN, REPOS_DIR


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


def remote_commit(url: str, branch: str | None) -> str | None:
    """Return the remote commit, or None when the remote cannot be read."""
    if branch and not _safe_branch(branch):
        raise ValueError(f"Invalid branch name: {branch}")
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    try:
        output = _run(["git", "-c", "safe.directory=*", "ls-remote", url, ref])
    except RuntimeError:
        return None
    lines = output.strip().splitlines()
    if not lines:
        return None
    sha, _ref = lines[0].split(maxsplit=1)
    return sha


def _destination(name: str) -> Path:
    root = REPOS_DIR.resolve()
    destination = (root / name).resolve()
    if root != destination and root not in destination.parents:
        raise RuntimeError(f"Refusing to write outside {root}")
    return destination


def _run(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
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
