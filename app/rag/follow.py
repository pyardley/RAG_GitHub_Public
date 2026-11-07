"""Follow a feature file to the step definitions and page objects that run it."""

from __future__ import annotations

import re

_FEATURE_PATH = re.compile(r"(?P<path>/?(?:[\w.-]+/)*[\w.-]+\.feature)\b")
_STEP_LINE = re.compile(
    r"(?m)^[ \t]*(?:Given|When|Then|And|But)\s+(?P<step>.+?)\s*$"
)
_QUALIFIED_CALL = re.compile(r"\b([a-z_][\w]*)\.([a-z_][\w?!]*)")
_BARE_CALL = re.compile(r"(?m)^[ \t]*([a-z_][\w?!]*)\s*$")
_SKIP_RECEIVERS = {
    "expect", "allow", "page", "subject", "eq", "eql", "be", "have", "match",
    "raise", "change", "receive", "and_return", "not", "to", "a",
}
_SKIP_METHODS = {
    "click", "content", "text", "visible", "displayed", "to", "not", "and", "or",
    "with", "by", "of", "new", "name", "first", "last", "all", "find", "end",
    "do", "if", "else", "elsif", "unless", "when", "then", "rescue", "ensure",
    "begin", "return", "next", "break", "yield", "super", "true", "false", "nil",
}
_SELECTED = """
    SELECT c.id, r.name, c.path, c.start_line, c.end_line, c.symbol, c.kind, c.content, 0
    FROM chunks c
    JOIN repositories r ON r.id = c.repository_id
"""


def feature_paths(question: str) -> list[str]:
    found: list[str] = []
    for match in _FEATURE_PATH.finditer(question):
        path = match.group("path").lstrip("/")
        if path not in found:
            found.append(path)
    return found


def step_phrases(content: str) -> list[str]:
    phrases: list[str] = []
    for match in _STEP_LINE.finditer(content):
        phrase = match.group("step").strip().strip("\"'")
        if len(phrase) < 8 or phrase in phrases:
            continue
        phrases.append(phrase)
    return phrases


def choose_steps(phrases: list[str]) -> list[str]:
    chosen: list[str] = []
    actions = [phrase for phrase in phrases if _looks_like_action(phrase)]
    for phrase in actions or phrases:
        if any(_same_step(phrase, other) for other in chosen):
            continue
        chosen.append(phrase)
        if len(chosen) == 1:
            break
    return chosen


def contains_step(content: str, phrase: str) -> bool:
    return re.search(
        rf"(?im)^[ \t]*(?:Given|When|Then|And|But)\s+{re.escape(phrase)}\s*$",
        content,
    ) is not None


def mentions_step(content: str, phrase: str) -> bool:
    return re.search(
        rf"(?i){re.escape(phrase)}(?=\s*(?:\$|\"|'|/|\)|\]|\Z))",
        content,
    ) is not None


def step_body(content: str, phrase: str) -> str:
    lines = content.splitlines()
    start = next((index for index, line in enumerate(lines) if phrase.lower() in line.lower()), None)
    if start is None:
        return content
    body = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r"^[ \t]*(Given|When|Then|Before|After)\b", line):
            break
        body.append(line)
        if len(body) >= 25:
            break
    return "\n".join(body)


def calls_in(content: str) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()

    def add(receiver: str | None, method: str) -> None:
        if method in _SKIP_METHODS or (receiver and receiver in _SKIP_RECEIVERS):
            return
        item = (receiver, method)
        if item in seen:
            return
        seen.add(item)
        found.append(item)

    for receiver, method in _QUALIFIED_CALL.findall(content):
        add(receiver, method)
    for method in _BARE_CALL.findall(content):
        add(None, method)
    return found


def page_symbol(receiver: str, method: str) -> str:
    class_name = "".join(part.capitalize() for part in receiver.split("_") if part)
    return f"{class_name}#{method}"


def definition_matches(content: str, phrase: str) -> bool:
    """A Cucumber step regexp in content matches the feature step text."""
    for line in content.splitlines():
        found = _DEFINITION.search(line)
        if found is None:
            continue
        flags = re.IGNORECASE if "i" in found.group(2) else 0
        try:
            pattern = re.compile(found.group(1), flags)
        except re.error:
            continue
        if pattern.search(phrase):
            return True
    return mentions_step(content, phrase)


def step_needles(phrase: str) -> list[str]:
    """Literal pieces of a feature step, outside quoted amounts and captures."""
    needles: list[str] = []
    for part in re.split(r"'[^']*'|\"[^\"]*\"|\d+", phrase):
        text = " ".join(part.split()).strip(" .,")
        if len(text) < 8 or text.lower() in {item.lower() for item in needles}:
            continue
        needles.append(text)
        if len(needles) == 3:
            break
    return needles


def follow_matched_steps(conn, repository_ids: list[int], question: str, hits: list) -> list:
    """Step definitions and page objects for feature lines a quoted search found."""
    from rag.answer import matching_lines
    from rag.search import _hit_from_row

    phrases: list[str] = []
    for path, _line, text in matching_lines(question, hits):
        if not path.endswith(".feature"):
            continue
        phrase = _bare_step(text)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    found: list = []
    seen: set[int] = set()
    for phrase in phrases[:4]:
        needles = step_needles(phrase)
        if not needles:
            continue
        params = {"repos": repository_ids}
        params.update({f"n{index}": f"%{_like(needle)}%" for index, needle in enumerate(needles)})
        for row in _rows(conn, _step_needles_sql(len(needles)), params):
            if not definition_matches(row[7], phrase):
                continue
            step = _hit_from_row(row)
            if _add(found, seen, step):
                anchor = needles[-1]
                _follow_calls(
                    conn, repository_ids, step_body(step.content, anchor), found, seen, depth=2
                )
            break
    return found


def _bare_step(text: str) -> str:
    return re.sub(r"^(?:Given|When|Then|And|But)\s+", "", text.strip(), count=1, flags=re.IGNORECASE)


def _step_needles_sql(count: int) -> str:
    clauses = " OR ".join(
        f"c.content ILIKE %(n{index})s ESCAPE '\\'" for index in range(count)
    )
    return _SELECTED + f"""
        WHERE c.repository_id = ANY(%(repos)s)
          AND c.path LIKE '%%step_definitions%%'
          AND ({clauses})
        ORDER BY c.path, c.start_line
        LIMIT 12
    """


def follow_features(conn, repository_ids: list[int], paths: list[str]) -> list:
    from rag.search import _hit_from_row

    hits = []
    seen: set[int] = set()
    for path in paths[:1]:
        feature_rows = _rows(conn, _PATH_SQL, {"repos": repository_ids, "path": path})
        phrases: list[str] = []
        for row in feature_rows:
            phrases.extend(step_phrases(row[7]))
        selected = choose_steps(phrases)
        for row in feature_rows:
            if not selected or any(contains_step(row[7], phrase) for phrase in selected):
                _add(hits, seen, _hit_from_row(row))
        for phrase in selected:
            for row in _rows(
                conn,
                _STEP_SQL,
                {"repos": repository_ids, "phrase": f"%{_like(phrase)}%"},
            ):
                if not mentions_step(row[7], phrase):
                    continue
                step = _hit_from_row(row)
                if _add(hits, seen, step):
                    _follow_calls(
                        conn, repository_ids, step_body(step.content, phrase), hits, seen, depth=2
                    )
                break
    return hits


def _follow_calls(conn, repository_ids, content: str, hits: list, seen: set[int], depth: int) -> None:
    from rag.search import _hit_from_row

    if depth < 1:
        return
    for receiver, method in calls_in(content):
        params = {
            "repos": repository_ids,
            "method": method.lower(),
            "symbol": page_symbol(receiver, method).lower() if receiver else method.lower(),
        }
        sql = _QUALIFIED_SQL if receiver else _BARE_SQL
        for row in _rows(conn, sql, params):
            hit = _hit_from_row(row)
            if _add(hits, seen, hit):
                _follow_calls(conn, repository_ids, hit.content, hits, seen, depth - 1)


def _add(hits: list, seen: set[int], hit) -> bool:
    if hit.chunk_id in seen or len(hits) >= 12:
        return False
    seen.add(hit.chunk_id)
    hits.append(hit)
    return True


def _rows(conn, sql: str, params: dict) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _same_step(left: str, right: str) -> bool:
    left, right = left.lower(), right.lower()
    return left == right or left.startswith(right + " ") or right.startswith(left + " ")


_DEFINITION = re.compile(
    r"""(?m)^[ \t]*(?:Given|When|Then|And|But)\s*\(\s*/(.*)/([a-z]*)\s*\)"""
)


def _looks_like_action(phrase: str) -> bool:
    lowered = phrase.lower()
    return lowered.startswith(("i enter", "i click", "i press", "i fill", "i select", "i choose", "i submit"))


_PATH_SQL = _SELECTED + """
    WHERE c.repository_id = ANY(%(repos)s)
      AND (c.path = %(path)s OR right(c.path, length(%(path)s) + 1) = '/' || %(path)s)
    ORDER BY c.start_line
    LIMIT 40
"""
_STEP_SQL = _SELECTED + r"""
    WHERE c.repository_id = ANY(%(repos)s)
      AND c.path LIKE '%%step_definitions%%'
      AND c.content ILIKE %(phrase)s ESCAPE '\'
    ORDER BY c.path, c.start_line
    LIMIT 8
"""
_QUALIFIED_SQL = _SELECTED + """
    WHERE c.repository_id = ANY(%(repos)s)
      AND lower(coalesce(c.symbol, '')) = %(symbol)s
    ORDER BY c.path, c.start_line
    LIMIT 2
"""
_BARE_SQL = _SELECTED + """
    WHERE c.repository_id = ANY(%(repos)s)
      AND c.path LIKE 'features/%%'
      AND lower(coalesce(c.symbol, '')) = %(method)s
    ORDER BY (c.path LIKE 'features/support/%%') DESC, c.path, c.start_line
    LIMIT 1
"""
