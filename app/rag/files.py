"""Decide which files in a checkout are worth indexing."""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

from rag.config import MAX_FILE_BYTES

SKIP_PARTS = {
    ".git",
    ".bundle",
    ".cache",
    ".yarn",
    "node_modules",
    "vendor",
    "tmp",
    "log",
    "coverage",
    "vcr_cassettes",
    "storage",
    "dist",
    "build",
    "packs",
}

SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".key")

EXT_LANG = {
    ".rb": "ruby",
    ".rake": "ruby",
    ".ru": "ruby",
    ".gemspec": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".groovy": "groovy",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".cs": "csharp",
    ".php": "php",
    ".feature": "gherkin",
    ".slim": "slim",
    ".erb": "erb",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".xml": "xml",
    ".properties": "properties",
    ".toml": "toml",
    ".tf": "hcl",
}

NAME_LANG = {
    "dockerfile": "dockerfile",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "procfile": "procfile",
    "makefile": "make",
    "jenkinsfile": "groovy",
    "config.ru": "ruby",
}

LOCKFILES = {
    "yarn.lock",
    "package-lock.json",
    "gemfile.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "composer.lock",
    "poetry.lock",
}


def parse_repo(spec: str) -> tuple[str, str]:
    """Return (owner/name, clone URL) for a GitHub repository argument."""
    spec = spec.strip()
    if not spec:
        raise ValueError("Repository is empty")
    if spec.startswith("git@github.com:"):
        path = spec.split(":", 1)[1].removesuffix(".git")
        _require_owner_name(path, spec)
        url = spec if spec.endswith(".git") else spec + ".git"
        return path, url
    if "://" in spec:
        parsed = urllib.parse.urlparse(spec)
        path = parsed.path.strip("/").removesuffix(".git")
        _require_owner_name(path, spec)
        return path, spec
    name = spec.removesuffix(".git")
    _require_owner_name(name, spec)
    return name, f"https://github.com/{name}.git"


def iter_source_files(root: Path):
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(root)
            if is_skipped(relative):
                continue
            language = language_for(path)
            if language is None:
                continue
            yield path, relative.as_posix(), language


def is_skipped(relative: Path) -> bool:
    if set(relative.parts) & SKIP_PARTS:
        return True
    posix = relative.as_posix()
    if posix == "public/assets" or posix.startswith("public/assets/"):
        return True
    name = relative.name.lower()
    if name in LOCKFILES:
        return True
    if _is_secret(name):
        return True
    return False


def language_for(path: Path) -> str | None:
    lowered = path.name.lower()
    if lowered in NAME_LANG:
        return NAME_LANG[lowered]
    if lowered.startswith("jenkinsfile"):
        return "groovy"
    if lowered.startswith("dockerfile"):
        return "dockerfile"
    if lowered.startswith("procfile"):
        return "procfile"
    if lowered.startswith(".env") and ("example" in lowered or "sample" in lowered):
        return "config"
    return EXT_LANG.get(path.suffix.lower())


def read_text(path: Path) -> str | None:
    size = path.stat().st_size
    if size == 0 or size > MAX_FILE_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_secret(name: str) -> bool:
    if name in {"master.key", "credentials.yml.enc", ".env"}:
        return True
    if name.startswith(".env") and "example" not in name and "sample" not in name:
        return True
    return name.endswith(SECRET_SUFFIXES)


def _require_owner_name(path: str, original: str) -> None:
    owner, separator, repo = path.partition("/")
    if (
        not separator
        or "/" in repo
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
        or owner in {".", ".."}
        or repo in {".", ".."}
    ):
        raise ValueError(
            f"Expected a GitHub repository as owner/name or a repository URL, got {original}"
        )
