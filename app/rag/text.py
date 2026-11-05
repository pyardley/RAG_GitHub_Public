"""Plain-text helpers shared by indexing and search."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")

# Keep this aligned with files.EXT_LANG. Duplicated so text helpers stay free of
# file-system imports and easy to test.
_EXTENSIONS = {
    ".rb", ".rake", ".ru", ".gemspec", ".java", ".kt", ".kts", ".scala",
    ".groovy", ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go",
    ".cs", ".php", ".feature", ".slim", ".erb", ".html", ".htm", ".css",
    ".scss", ".yml", ".yaml", ".json", ".md", ".sql", ".sh", ".bash",
    ".xml", ".properties", ".toml", ".tf",
}


def search_text(*parts: str | None) -> str:
    """Build the string stored for keyword search.

    CamelCase and snake_case are split so a query for "fee" can match
    FeeCalculator and fee_calculator. The Postgres "simple" dictionary then
    lower-cases tokens without stemming them, which is what we want for code.
    """
    raw = "\n".join(part for part in parts if part)
    raw = raw.replace("::", " ").replace("_", " ")
    raw = raw.replace("/", " ").replace("\\", " ").replace("#", " ").replace(".", " ")
    raw = _CAMEL_BOUNDARY.sub(r"\1 \2", raw)
    raw = _ACRONYM_BOUNDARY.sub(r"\1 \2", raw)
    return raw


def last_identifier(term: str) -> str:
    pieces = _identifier_pieces(term)
    return pieces[-1] if pieces else term.strip()


def symbol_matches(symbol: str | None, term: str) -> bool:
    if not symbol or not term.strip():
        return False
    left = _normalise_symbol(symbol)
    right = _normalise_symbol(term)
    if left == right:
        return True
    # "save" matches "Application#save", but "Foo#save" does not match "Bar#save".
    if "#" not in right and "/" not in term and "\\" not in term:
        return left.split("#")[-1] == right
    return False


def path_matches(path: str, term: str) -> bool:
    norm_path = path.replace("\\", "/").lower()
    norm_term = term.strip().replace("\\", "/").lower()
    if not norm_term:
        return False
    if "/" in norm_term or norm_term.startswith("."):
        return norm_term in norm_path
    name = PurePosixPath(norm_path).name
    stem = PurePosixPath(norm_path).stem
    return norm_term == name or norm_term == stem


def looks_like_path(term: str) -> bool:
    normalised = term.strip().replace("\\", "/")
    if "/" in normalised or normalised.startswith("."):
        return True
    suffix = PurePosixPath(normalised).suffix.lower()
    return suffix in _EXTENSIONS


def has_declaration(content: str, term: str) -> bool:
    ident = re.escape(last_identifier(term))
    if not ident:
        return False
    pattern = re.compile(
        rf"(?m)^[ \t]*"
        rf"(?:(?:public|protected|private|internal|static|final|abstract|async|export|default|open|data|sealed|override|synchronized)\s+)*"
        rf"(?:class|module|interface|enum|trait|object|record|function|fun|def|type)\s+(?:self\.)?{ident}\b"
        rf"|^[ \t]*(?:(?:public|protected|private|internal|static|final|synchronized)\s+)+[\w.<>,\[\]?]+\s+{ident}\s*\("
    )
    return pattern.search(content) is not None


def identifier_regex(term: str) -> str:
    """Postgres ARE pattern that matches an identifier as a whole word.

    Foo#bar, Foo.bar and Foo::bar are treated as the same reference shape.
    A trailing ? or ! is kept, because those characters are part of Ruby names.
    """
    pieces = _identifier_pieces(term)
    if not pieces:
        pieces = [term.strip()]
    body = r"[\s#.:]+".join(re.escape(piece) for piece in pieces)
    if pieces[0][:1].isalnum() or pieces[0].startswith("_"):
        body = r"\m" + body
    if pieces[-1][-1:].isalnum() or pieces[-1].endswith("_"):
        body = body + r"\M"
    else:
        body = body + r"(?!\w)"
    return body


def _identifier_pieces(term: str) -> list[str]:
    normalised = term.strip().replace("\\", "/")
    if "/" in normalised:
        normalised = PurePosixPath(normalised).stem
    pieces = [piece for piece in re.split(r"[#:\s]+|(?<=\w)\.(?=\w)", normalised) if piece]
    if len(pieces) >= 2 and ("." + pieces[-1].lower()) in _EXTENSIONS:
        pieces = pieces[:-1]
    return pieces


def _normalise_symbol(value: str) -> str:
    return re.sub(r"::|\.", "#", value.strip().lower())
