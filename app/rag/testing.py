"""Advice on running and debugging the Cucumber suite under features/.

Query would plan "how do I run the tests" into a short search and mix in
RSpec and Minitest. Testing skips that planner. It looks up the commands the
repository already documents, then a written answer may quote only those.
"""

from __future__ import annotations

import re

from rag.follow import feature_paths
from rag.log import logger
from rag.search import Hit, literal_hits, search
from rag.usage import Meter, Stage

# Phrases that appear in the indexed READMEs and cucumber profiles.
_COMMANDS = (
    "bundle exec cucumber",
    "parallel:features",
    "SHOW_BROWSER",
    "DRIVER=chrome",
    "not @wip",
    "@wip:3",
    "parallel_cucumber",
    "cucumber -p smoke",
)
_EXCLUDE = ("spec", "test", "app")
_TAG = re.compile(r"@[\w-]+")


def command_phrases(question: str) -> list[str]:
    """Command text to find, plus any Cucumber tag the question names."""
    found = list(_COMMANDS)
    for match in _TAG.finditer(question or ""):
        tag = match.group(0)
        if tag not in found:
            found.append(tag)
    return found


def retrieve_testing(
    conn,
    repository_ids: list[int],
    question: str,
    k: int,
    meter: Meter | None = None,
) -> tuple[list[Hit], list[str], bool]:
    """Passages that show how this repository runs Cucumber under features/.

    Returns the passages, the searches that ran, and whether the list was cut
    to the passage limit.
    """
    if k < 1:
        raise ValueError("Result count must be at least 1")
    if meter is not None:
        meter.add(Stage(
            "Plan the question",
            0.0,
            note="Testing looks up the repository's Cucumber commands and leaves RSpec and Minitest out.",
        ))
    phrases = command_phrases(question)
    paths = resolve_feature_paths(feature_paths(question), indexed_feature_paths(conn, repository_ids))
    excluded = list(_EXCLUDE)
    command_hits, elapsed, _more = literal_hits(
        conn, repository_ids, [], phrases, max(k, 40), excluded,
    )
    if meter is not None:
        meter.add_search(0.0, elapsed)
    named: list[Hit] = []
    for path in paths:
        found, extra, _more_file = literal_hits(
            conn, repository_ids, [path], ["Scenario"], 2, [],
        )
        named.extend(found)
        if meter is not None:
            meter.add_search(0.0, extra)
    subject = (question or "").strip()
    text = (
        "how to run the cucumber suite under features, one feature file, "
        "a tag or profile, and debug a scenario being written. "
        + subject
    )
    semantic, _semantic_more = search(
        conn, repository_ids, text, max(k, 20), meter,
        excluded=excluded, index="code", fusion="rrf",
    )
    ordered = _prefer(command_hits, named, semantic, paths)
    hits = ordered[:k]
    truncated = len(ordered) > k
    queries = [" | ".join(phrases)]
    if paths:
        queries.append("feature: " + ", ".join(paths))
    if subject and subject not in queries:
        queries.append(subject)
    if meter is not None:
        meter.finish_retrieval(len(hits), embedded=True, direct=None)
    logger.info("testing passages=%s queries=%s", len(hits), " | ".join(queries))
    return hits, queries, truncated


def indexed_feature_paths(conn, repository_ids: list[int]) -> list[str]:
    """Feature files stored for these repositories."""
    if conn is None or not repository_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT path
            FROM chunks
            WHERE repository_id = ANY(%(repos)s)
              AND lower(path) LIKE 'features/%%.feature'
            ORDER BY path
            """,
            {"repos": repository_ids},
        )
        found = [row[0] for row in cur.fetchall()]
    conn.commit()
    return found


def resolve_feature_paths(named: list[str], indexed: list[str]) -> list[str]:
    """Use the stored path when a question names a feature file.

    dob_married.feature and features/dob_married.feature both become
    features/dob/dob_married.feature when that is the file in the index.
    A path that is already stored is left as it is.
    """
    available: list[str] = []
    for path in indexed:
        cleaned = path.replace("\\", "/").lstrip("/")
        if cleaned not in available:
            available.append(cleaned)
    resolved: list[str] = []
    for raw in named:
        cleaned = raw.replace("\\", "/").lstrip("/")
        exact = [path for path in available if path.lower() == cleaned.lower()]
        if exact:
            chosen = exact
        else:
            base = cleaned.rsplit("/", 1)[-1].lower()
            chosen = [
                path for path in available
                if path.lower() == base or path.lower().endswith("/" + base)
            ]
        if not chosen:
            chosen = [cleaned]
        for path in chosen:
            if path not in resolved:
                resolved.append(path)
    return resolved


def _prefer(
    command_hits: list[Hit],
    named: list[Hit],
    semantic: list[Hit],
    paths: list[str],
) -> list[Hit]:
    """README and cucumber.yml first, then the named feature, then other guides.

    A feature file is kept only when the question names it. spec/, test/, and
    app/ are left out, as are step definitions and page objects.
    """
    guides = [hit for hit in command_hits if _is_guide(hit.path)]
    guides.extend(
        hit for hit in semantic
        if _is_guide(hit.path) and hit.chunk_id not in {item.chunk_id for item in guides}
    )
    ordered = _spread(_by_band(guides))
    seen = {hit.chunk_id for hit in ordered}
    kept: list[Hit] = []
    for hit in named:
        if hit.chunk_id in seen or not _names_feature(hit.path, paths):
            continue
        seen.add(hit.chunk_id)
        kept.append(hit)
    # The named feature stays with the command notes, ahead of later guides,
    # so a short passage list still shows its full path.
    head = [hit for hit in ordered if _band(hit.path) <= 1]
    return head + kept + [hit for hit in ordered if hit not in head]


def _by_band(hits: list[Hit]) -> list[Hit]:
    ranked = sorted(enumerate(hits), key=lambda item: (_band(item[1].path), item[0]))
    return [hit for _index, hit in ranked]


def _spread(hits: list[Hit]) -> list[Hit]:
    """One chunk from each file, in the current order, then any later chunks."""
    seen: set[str] = set()
    first: list[Hit] = []
    rest: list[Hit] = []
    for hit in hits:
        key = hit.path.replace("\\", "/").lower()
        if key in seen:
            rest.append(hit)
            continue
        seen.add(key)
        first.append(hit)
    return first + rest


def _band(path: str) -> int:
    lowered = path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    if name == "readme.md":
        return 0
    if name == "cucumber.yml":
        return 1
    if name == "rakefile":
        return 2
    if "features/support/" in lowered and any(
        token in name for token in ("env.rb", "driver", "capybara")
    ):
        return 3
    if lowered.startswith("bin/") or "/bin/" in lowered:
        return 4
    return 5


def _is_guide(path: str) -> bool:
    """True for notes and config that say how Cucumber is run."""
    lowered = path.replace("\\", "/").lower().lstrip("/")
    name = lowered.rsplit("/", 1)[-1]
    if name in {"readme.md", "cucumber.yml", "rakefile"}:
        return True
    if lowered.startswith("bin/") or "/bin/" in f"/{lowered}":
        return True
    if lowered.startswith(".github/") or lowered.startswith("docs/"):
        return True
    if "docker" in lowered and name.endswith((".md", ".yml", ".yaml", ".sh")):
        return True
    if "features/support/" in lowered and any(
        token in name for token in ("env.rb", "driver", "capybara")
    ):
        return True
    if name.endswith(".md") and not lowered.startswith("features/"):
        return True
    return False


def _names_feature(path: str, paths: list[str]) -> bool:
    lowered = path.replace("\\", "/").lower().lstrip("/")
    for named in paths:
        cleaned = named.replace("\\", "/").lower().lstrip("/")
        if lowered == cleaned:
            return True
    return False
