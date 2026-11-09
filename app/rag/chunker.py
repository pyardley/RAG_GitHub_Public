"""Turn a source file into retrieval chunks.

Definitions become their own chunks, with the enclosing type kept in the
symbol name (Application#submit, FeeService.calculate). Ruby files also split
on RSpec groups and examples, SitePrism sections, and FactoryBot factories
and traits. The splitter is a heuristic based on indentation and declaration
lines. It does not build a call graph, and a declaration hidden inside a
string can be mistaken for code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag.config import EMBED_TEXT_CHARS, LINE_OVERLAP, MAX_CHUNK_CHARS, MAX_LINES

CLASS_KINDS = {"class", "module", "object", "trait", "interface", "enum", "record", "feature"}
CONTAINER_KINDS = CLASS_KINDS | {
    "describe",
    "context",
    "factory",
    "section",
    "shared_examples",
    "shared_examples_for",
    "shared_context",
}
METHOD_KINDS = {"def", "function", "method", "fun", "constructor"}

_RUBY = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>class|module|def)\s+"
    r"(?P<name>self\.[A-Za-z_][\w?!]*|[A-Za-z_][\w:?!]*)"
)
_DESCRIBE_STRING = re.compile(
    r"^(?P<indent>[ \t]*)(?:RSpec\.)?(?P<kind>describe|context)\s+"
    r"(?P<quote>['\"])(?P<name>.+?)(?P=quote)"
)
_DESCRIBE_CONST = re.compile(
    r"^(?P<indent>[ \t]*)(?:RSpec\.)?(?P<kind>describe|context)\s+"
    r"(?P<name>[A-Z][\w:]*)\b"
)
_FACTORY = re.compile(
    r"^(?P<indent>[ \t]*)factory\s+:(?P<name>[A-Za-z_]\w*)\b"
)
_FACTORY_TRAIT = re.compile(
    r"^(?P<indent>[ \t]*)trait\s+:(?P<name>[A-Za-z_]\w*)\b"
)
_SECTION = re.compile(
    r"^(?P<indent>[ \t]*)sections?\s+:(?P<name>[A-Za-z_]\w*)\b"
)
_SHARED = re.compile(
    r"^(?P<indent>[ \t]*)(?:RSpec\.)?(?P<kind>shared_examples_for|shared_examples|shared_context)\s+"
    r"(?P<quote>['\"])(?P<name>.+?)(?P=quote)"
)
_EXAMPLE = re.compile(
    r"^(?P<indent>[ \t]*)(?:it|specify|example)\s+"
    r"(?P<quote>['\"])(?P<name>.+?)(?P=quote)"
)
_PAGE_DECLARATION = re.compile(r"<\s*SitePrism::|<\s*BasePage\b")
_SET_URL = re.compile(r"^[ \t]*set_url\b", re.MULTILINE)
_GROUP_LABELS = {
    "shared_examples": "shared examples",
    "shared_examples_for": "shared examples",
    "shared_context": "shared context",
}
_SLASH_KINDS = {
    "describe",
    "context",
    "example",
    "scenario",
    "scenario outline",
    "background",
    "feature",
    "shared_examples",
    "shared_examples_for",
    "shared_context",
}
_PY = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>class|def)\s+(?P<name>[A-Za-z_][\w]*)"
)
_JS_FUNCTION = re.compile(
    r"^(?P<indent>[ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?P<kind>function|class)\s+(?P<name>[A-Za-z_$][\w$]*)"
)
_JS_METHOD = re.compile(
    r"^(?P<indent>[ \t]+)(?:async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*\([^;\n]*\)\s*\{?\s*$"
)
_JS_SKIP = {"if", "for", "while", "switch", "catch", "function", "return", "do"}
_JAVA_TYPE = re.compile(
    r"^(?P<indent>[ \t]*)(?:[\w\[\]<>,?]+\s+)*(?P<kind>class|interface|enum|record)\s+"
    r"(?P<name>[A-Za-z_][\w]*)\b"
)
_JAVA_METHOD = re.compile(
    r"^(?P<indent>[ \t]+)"
    r"(?:(?:public|protected|private|static|final|synchronized|native|default|abstract)\s+)+"
    r"(?:[\w.<>,\[\]?]+\s+)+"
    r"(?P<name>[A-Za-z_][\w]*)\s*\([^;]*\)\s*\{?\s*$"
)
_JAVA_CTOR = re.compile(
    r"^(?P<indent>[ \t]+)(?:public|protected|private)\s+"
    r"(?P<name>[A-Z][\w]*)\s*\([^;]*\)\s*\{?\s*$"
)
_GHERKIN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>Feature|Scenario Outline|Scenario|Background)\s*:\s*"
    r"(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Chunk:
    start_line: int
    end_line: int
    symbol: str | None
    kind: str
    content: str


@dataclass(frozen=True)
class _Defn:
    line: int
    indent: int
    kind: str
    name: str


def chunk_source(
    language: str,
    text: str,
    *,
    max_lines: int = MAX_LINES,
    overlap: int = LINE_OVERLAP,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[Chunk]:
    lines = text.splitlines()
    if not lines or not any(line.strip() for line in lines):
        return []
    defs = _find_defs(language, lines)
    if not defs:
        return _emit_span(
            lines, 0, len(lines), None, "file", max_lines=max_lines, overlap=overlap, max_chars=max_chars
        )
    chunks: list[Chunk] = []
    if defs[0].line > 0:
        chunks.extend(
            _emit_span(
                lines, 0, defs[0].line, None, "file",
                max_lines=max_lines, overlap=overlap, max_chars=max_chars,
            )
        )
    for index, defn in enumerate(defs):
        end = _span_end(defs, index, len(lines))
        symbol = _format_symbol(language, _enclosing(defs, index), defn)
        kind = _stored_kind(defn.kind)
        chunks.extend(
            _emit_span(
                lines, defn.line, end, symbol, kind,
                max_lines=max_lines, overlap=overlap, max_chars=max_chars,
            )
        )
    return chunks


def source_role(path: str, content: str) -> str | None:
    """Label the Ruby files these repositories are built from.

    The label is stored with the chunk so a question about a SitePrism page,
    an RSpec example, or a FactoryBot trait is aimed at that kind of file.
    """
    normalised = path.replace("\\", "/").lower()
    if "/factories/" in f"/{normalised}":
        return "FactoryBot factory"
    if "page_object" in normalised or _PAGE_DECLARATION.search(content) or _SET_URL.search(content):
        return "SitePrism page"
    if normalised.endswith("_spec.rb") or "/spec/" in f"/{normalised}":
        return "RSpec"
    if normalised.endswith(".feature"):
        return "Cucumber feature"
    if "/step_definitions/" in f"/{normalised}":
        return "Cucumber step definition"
    return None


def embed_text(path: str, language: str, symbol: str | None, content: str) -> str:
    header = f"path: {path}\nlanguage: {language}"
    role = source_role(path, content)
    if role:
        header += f"\nrole: {role}"
    if symbol:
        header += f"\nsymbol: {symbol}"
    text = f"{header}\n\n{content}"
    return text[:EMBED_TEXT_CHARS]


def _find_defs(language: str, lines: list[str]) -> list[_Defn]:
    found: list[_Defn] = []
    for index, line in enumerate(lines):
        matched = _match_line(language, line)
        if matched is not None:
            found.append(_Defn(index, matched[0], matched[1], matched[2]))
    return found


def _match_line(language: str, line: str) -> tuple[int, str, str] | None:
    if language == "ruby":
        return _match_ruby(line)
    if language == "python":
        return _from_named(_PY, line, lambda kind, name: (kind, name))
    if language in {"javascript", "typescript"}:
        return _match_js(line)
    if language == "java":
        return _match_java(line)
    if language == "gherkin":
        matched = _GHERKIN.match(line)
        if matched:
            return (
                _indent_width(matched.group("indent")),
                matched.group("kind").lower(),
                matched.group("name").strip(),
            )
    return None


def _match_ruby(line: str) -> tuple[int, str, str] | None:
    matched = _RUBY.match(line)
    if matched:
        return (
            _indent_width(matched.group("indent")),
            matched.group("kind"),
            matched.group("name"),
        )
    for pattern, kind in (
        (_FACTORY, "factory"),
        (_FACTORY_TRAIT, "factory_trait"),
        (_SECTION, "section"),
    ):
        found = pattern.match(line)
        if found:
            return (_indent_width(found.group("indent")), kind, found.group("name"))
    shared = _SHARED.match(line)
    if shared:
        return (
            _indent_width(shared.group("indent")),
            shared.group("kind"),
            shared.group("name"),
        )
    example = _EXAMPLE.match(line)
    if example:
        return (
            _indent_width(example.group("indent")),
            "example",
            example.group("name"),
        )
    described = _DESCRIBE_STRING.match(line) or _DESCRIBE_CONST.match(line)
    if described:
        return (
            _indent_width(described.group("indent")),
            described.group("kind"),
            described.group("name"),
        )
    return None


def _match_js(line: str) -> tuple[int, str, str] | None:
    matched = _JS_FUNCTION.match(line)
    if matched:
        kind = "method" if matched.group("kind") == "function" else "class"
        return (_indent_width(matched.group("indent")), kind, matched.group("name"))
    method = _JS_METHOD.match(line)
    if method and method.group("name") not in _JS_SKIP:
        return (_indent_width(method.group("indent")), "method", method.group("name"))
    return None


def _match_java(line: str) -> tuple[int, str, str] | None:
    matched = _JAVA_TYPE.match(line)
    if matched:
        return (
            _indent_width(matched.group("indent")),
            matched.group("kind"),
            matched.group("name"),
        )
    method = _JAVA_METHOD.match(line)
    if method and method.group("name") not in _JS_SKIP:
        return (_indent_width(method.group("indent")), "method", method.group("name"))
    ctor = _JAVA_CTOR.match(line)
    if ctor:
        return (_indent_width(ctor.group("indent")), "constructor", ctor.group("name"))
    return None


def _from_named(pattern: re.Pattern[str], line: str, build):
    matched = pattern.match(line)
    if not matched:
        return None
    kind, name = build(matched.group("kind"), matched.group("name"))
    return (_indent_width(matched.group("indent")), kind, name)


def _enclosing(defs: list[_Defn], index: int) -> list[_Defn]:
    current = defs[index]
    ancestors: list[_Defn] = []
    for prev_index, prev in enumerate(defs[:index]):
        if prev.kind not in CONTAINER_KINDS or prev.indent >= current.indent:
            continue
        closed = any(mid.indent <= prev.indent for mid in defs[prev_index + 1 : index])
        if not closed:
            ancestors.append(prev)
    return ancestors


def _span_end(defs: list[_Defn], index: int, line_count: int) -> int:
    current = defs[index]
    end = line_count
    for later in defs[index + 1 :]:
        if later.indent <= current.indent:
            end = later.line
            break
    for later in defs[index + 1 :]:
        if later.line >= end:
            break
        if later.indent > current.indent:
            end = later.line
            break
    return end


def _format_symbol(language: str, ancestors: list[_Defn], defn: _Defn) -> str:
    if defn.kind == "factory":
        return f"factory :{defn.name}"
    if defn.kind == "factory_trait":
        factory = next((ancestor.name for ancestor in reversed(ancestors) if ancestor.kind == "factory"), None)
        if factory:
            return f"factory :{factory} trait :{defn.name}"
        return f"trait :{defn.name}"
    if defn.kind == "section":
        class_names = [ancestor.name for ancestor in ancestors if ancestor.kind in CLASS_KINDS]
        prefix = "::".join(class_names)
        return f"{prefix} section :{defn.name}" if prefix else f"section :{defn.name}"
    if defn.kind in _SLASH_KINDS:
        names = [_group_name(ancestor) for ancestor in ancestors] + [_group_name(defn)]
        return " / ".join(names)
    class_names = [ancestor.name for ancestor in ancestors if ancestor.kind in CLASS_KINDS]
    if defn.kind in CLASS_KINDS:
        return "::".join(class_names + [defn.name])
    own = defn.name
    separator = "."
    if language == "ruby" and defn.kind == "def":
        if own.startswith("self."):
            own = own[len("self.") :]
            separator = "."
        else:
            separator = "#"
    if class_names:
        return "::".join(class_names) + separator + own
    return own


def _group_name(defn: _Defn) -> str:
    label = _GROUP_LABELS.get(defn.kind)
    return f"{label} {defn.name}" if label else defn.name


def _stored_kind(kind: str) -> str:
    if kind in CLASS_KINDS:
        return kind
    if kind in METHOD_KINDS or kind == "def":
        return "method"
    return kind


def _emit_span(
    lines: list[str],
    start: int,
    end: int,
    symbol: str | None,
    kind: str,
    *,
    max_lines: int,
    overlap: int,
    max_chars: int,
) -> list[Chunk]:
    while end > start and not lines[end - 1].strip():
        end -= 1
    if end <= start:
        return []
    chunks: list[Chunk] = []
    for piece_start, piece_end in _pack(lines, start, end, max_lines, overlap, max_chars):
        body = "\n".join(lines[piece_start:piece_end])
        if not re.search(r"\w", body):
            continue
        chunks.append(
            Chunk(
                start_line=piece_start + 1,
                end_line=piece_end,
                symbol=symbol,
                kind=kind,
                content=body,
            )
        )
    return chunks


def _pack(
    lines: list[str],
    start: int,
    end: int,
    max_lines: int,
    overlap: int,
    max_chars: int,
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + max_lines, end)
        while stop > cursor + 1 and _width(lines, cursor, stop) > max_chars:
            stop -= 1
        if stop == cursor + 1 and _width(lines, cursor, stop) > max_chars:
            # A single huge line is still one chunk; embedding truncates it.
            stop = cursor + 1
        pieces.append((cursor, stop))
        if stop >= end:
            break
        cursor = max(cursor + 1, stop - overlap)
    return pieces


def _width(lines: list[str], start: int, end: int) -> int:
    return sum(len(lines[index]) + 1 for index in range(start, end))


def _indent_width(indent: str) -> int:
    return len(indent.replace("\t", "  "))
