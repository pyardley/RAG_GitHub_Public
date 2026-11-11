"""Command line for ingesting repositories and asking questions."""

from __future__ import annotations

import argparse
import sys

from rag.answer import AnswerError, active_model, answer, configuration_hint, language_model_configured
from rag.db import connect, list_repositories, resolve_repositories
from rag.ingest import delete_repository, ingest_repository
from rag.log import configure_logging, logger
from rag.search import Hit, impact, retrieve
from rag.usage import Meter
from rag.web import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rag",
        description="Index GitHub repositories and ask questions about the code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_parser = sub.add_parser("ingest", help="Clone a repository and index its code")
    ingest_parser.add_argument("repo", help="owner/name or GitHub URL")
    ingest_parser.add_argument("--branch", help="Branch to index. Defaults to the remote default branch")
    ingest_parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild the index even when this commit is already stored",
    )

    query_parser = sub.add_parser("query", help="Ask a question about indexed code")
    query_parser.add_argument("question", help="Question, in ordinary language")
    query_parser.add_argument("--repo", help="Limit the search to owner/name")
    query_parser.add_argument("-k", type=int, default=8, help="Number of passages to retrieve")
    query_parser.add_argument(
        "--no-answer",
        action="store_true",
        help="Print matching code only, even when a language model is configured",
    )
    query_parser.add_argument("--model", help="Language model for this question. Overrides the configured model")

    impact_parser = sub.add_parser(
        "impact",
        help="Show where a class, method, or file is defined and referenced",
    )
    impact_parser.add_argument("target", help="Symbol or path, such as Application#submit or app/models/fee.rb")
    impact_parser.add_argument("--repo", help="Limit the search to owner/name")
    impact_parser.add_argument("--no-answer", action="store_true")
    impact_parser.add_argument("--model", help="Language model for this question. Overrides the configured model")

    sub.add_parser("repos", help="List indexed repositories")

    remove_parser = sub.add_parser("remove", help="Delete one repository index")
    remove_parser.add_argument("repo", help="owner/name or GitHub URL")

    serve_parser = sub.add_parser("serve", help="Run the web interface")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Address to listen on")
    serve_parser.add_argument("--port", type=int, default=8080, help="Port to listen on")

    args = parser.parse_args(argv)
    configure_logging()
    try:
        if args.command == "ingest":
            return ingest_repository(args.repo, args.branch, args.force)
        if args.command == "repos":
            return _repos()
        if args.command == "remove":
            return _remove(args.repo)
        if args.command == "serve":
            serve(args.host, args.port)
            return 0
        if args.command == "query":
            return _query(args.question, args.repo, args.k, args.no_answer, args.model)
        if args.command == "impact":
            return _impact(args.target, args.repo, args.no_answer, args.model)
    except (ValueError, RuntimeError) as exc:
        logger.warning("%s failed: %s", args.command, exc)
        print(str(exc), file=sys.stderr)
        return 1
    return 1


def _remove(repo: str) -> int:
    removed = delete_repository(repo)
    sha = str(removed["commit_sha"])[:12]
    print(
        f"Removed {removed['name']} {removed['branch']} {sha} "
        f"({removed['files']} files, {removed['chunks']} chunks)."
    )
    return 0


def _repos() -> int:
    conn = connect()
    try:
        rows = list_repositories(conn)
    finally:
        conn.close()
    if not rows:
        print("No repositories are indexed.")
        return 0
    for row in rows:
        ingested = row["ingested_at"].strftime("%Y-%m-%d %H:%M")
        sha = row["commit_sha"][:12]
        print(
            f"{row['name']}  {row['branch']}  {sha}  {ingested}  "
            f"{row['files']} files  {row['chunks']} chunks"
        )
    return 0


def _query(question: str, repo: str | None, k: int, no_answer: bool, model: str | None) -> int:
    meter = Meter()
    conn = connect()
    try:
        repositories = resolve_repositories(conn, repo)
        _print_scope(repositories)
        hits, queries, truncated = retrieve(conn, [item["id"] for item in repositories], question, k, meter)
    finally:
        conn.close()
    if len(queries) > 1:
        print("Searched: " + " | ".join(queries))
    if truncated:
        print(f"More matches were found than the Passages limit of {k}.")
    if not hits:
        print("No matching code.")
        _print_usage(meter)
        return 0
    _print_hits(hits)
    status = _maybe_answer(question, hits, impact_mode=False, no_answer=no_answer, model=model, meter=meter)
    _print_usage(meter)
    return status


def _impact(target: str, repo: str | None, no_answer: bool, model: str | None) -> int:
    conn = connect()
    try:
        repositories = resolve_repositories(conn, repo)
        _print_scope(repositories)
        found = impact(conn, [item["id"] for item in repositories], target)
    finally:
        conn.close()
    definitions = found["definitions"]
    references = found["references"]
    related = found["related"]
    if not definitions and not references and not related:
        print(f"Nothing in the index mentions {target}.")
        return 0
    if found["truncated"]:
        print("This name appears in more chunks than are shown here.\n")
    _print_section("Definitions", definitions, found["definition_count"])
    _print_section("References", references, found["reference_count"])
    _print_caller_note(found.get("callers_omitted", 0), found.get("callers_capped", False))
    if found.get("possible_count"):
        print("⇢ is a call by a name that more than one class defines. That caller is possible, not certain.\n")
    if found.get("gaps"):
        _print_section("Unresolved", found["gaps"], len(found["gaps"]))
    _print_section("Related", related, len(related))
    evidence = definitions[:8] + references[:8] + found.get("gaps", [])[:4] + related[:4]
    return _maybe_answer(target, evidence, impact_mode=True, no_answer=no_answer, model=model)


def _maybe_answer(
    question: str,
    hits: list[Hit],
    *,
    impact_mode: bool,
    no_answer: bool,
    model: str | None,
    meter: Meter | None = None,
) -> int:
    if no_answer or not hits:
        return 0
    if not language_model_configured():
        print(
            "\nNo language model is configured, so this is the retrieved code only. "
            + configuration_hint()
        )
        return 0
    print(f"\nAsking {active_model(model)}...", flush=True)
    try:
        written = answer(question, hits, impact=impact_mode, model=model, meter=meter)
    except AnswerError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    if written:
        print("\nAnswer")
        print("------")
        print(written)
    return 0


def _print_usage(meter: Meter) -> None:
    usage = meter.as_dict()
    if not usage["stages"]:
        return
    print("\nTiming")
    print("------")
    for stage in usage["stages"]:
        tokens = ""
        if stage["total_tokens"] is not None:
            tokens = f"  {stage['prompt_tokens']} prompt + {stage['completion_tokens']} completion"
            if stage["model"]:
                tokens += f"  ({stage['model']})"
        note = f"  {stage['note']}" if stage["note"] else ""
        print(f"{stage['name']}: {stage['seconds']:.3f}s{tokens}{note}")
    print(
        f"Total: {usage['seconds']:.3f}s  "
        f"{usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion "
        f"= {usage['total_tokens']} tokens"
    )


def _print_scope(repositories: list[dict]) -> None:
    if len(repositories) == 1:
        repo = repositories[0]
        print(f"Repository {repo['name']} @ {repo['commit_sha'][:12]} ({repo['branch']})")
    else:
        names = ", ".join(repo["name"] for repo in repositories)
        print(f"Searching {len(repositories)} repositories: {names}")
    print()


def _print_caller_note(omitted: int, capped: bool) -> None:
    if omitted and capped:
        print(f"At least {omitted} more callers were left out of References.\n")
    elif omitted:
        print(f"{omitted} more callers were left out of References.\n")
    elif capped:
        print("More callers were left out of References.\n")


def _print_section(title: str, hits: list[Hit], total: int) -> None:
    print(title)
    print("-" * len(title))
    if not hits:
        print("None.")
        print()
        return
    extra = total - len(hits)
    _print_hits(hits, show_score=False)
    if extra > 0:
        print(f"... {extra} more not shown.")
        print()


def _print_hits(hits: list[Hit], show_score: bool = True) -> None:
    for hit in hits:
        symbol = f"  {hit.symbol}" if hit.symbol else ""
        score = f"  score {hit.score:.4f}" if show_score else ""
        print(f"{hit.repo}  {hit.path}:{hit.start_line}-{hit.end_line}{symbol}{score}")
        if hit.via:
            print(f"  via {hit.via}")
        print(_clip(hit.content))
        print()


def _clip(content: str, limit: int = 40) -> str:
    lines = content.splitlines() or [""]
    if len(lines) <= limit:
        return content
    hidden = len(lines) - limit
    return "\n".join(lines[:limit] + [f"... {hidden} more lines"])
