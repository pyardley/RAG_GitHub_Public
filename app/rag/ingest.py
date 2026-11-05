"""Clone a repository and store its chunks in Postgres."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from rag.chunker import Chunk, chunk_source, embed_text
from rag.db import connect, get_repository
from rag.embedder import Embedder
from rag.files import iter_source_files, parse_repo, read_text
from rag.gitrepo import checkout, remote_commit
from rag.text import search_text

_LOCK_KEY = 814263591
_EMBED_BATCH = 32


@dataclass
class _PendingFile:
    path: str
    language: str
    content_hash: str
    chunks: list[Chunk]


def ingest_repository(
    repo: str,
    branch: str | None,
    force: bool,
    progress: Callable[[dict], None] | None = None,
) -> int:
    name, url = parse_repo(repo)
    started = time.perf_counter()
    conn = connect()
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        conn.commit()
        try:
            return _ingest(conn, name, url, branch, force, started, progress)
        finally:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()
    finally:
        conn.close()


def _ingest(
    conn,
    name: str,
    url: str,
    branch: str | None,
    force: bool,
    started: float,
    progress: Callable[[dict], None] | None,
) -> int:
    existing = get_repository(conn, name)
    remote = remote_commit(url, branch)
    if (
        existing
        and remote
        and remote == existing["commit_sha"]
        and not force
        and (branch is None or branch == existing["branch"])
    ):
        print(f"{name} is already indexed at {remote[:12]} ({existing['branch']}).")
        print("Pass --force to rebuild the index for this commit.")
        _note(progress, phase="done", done=1, total=1, chunks=0, label="Already indexed")
        return 0

    _note(progress, phase="clone", done=0, total=0, chunks=0, label=f"Cloning {name}")
    print(f"Cloning {name}" + (f" branch {branch}" if branch else ""), flush=True)
    destination, sha, checked_out = checkout(name, url, branch)
    print(f"Checked out {sha[:12]} ({checked_out})", flush=True)
    if (
        existing
        and sha == existing["commit_sha"]
        and checked_out == existing["branch"]
        and not force
    ):
        print(f"{name} is already indexed at {sha[:12]} ({checked_out}).")
        _note(progress, phase="done", done=1, total=1, chunks=0, label="Already indexed")
        return 0

    repository_id = _ensure_repository(conn, name, url)
    known = _existing_documents(conn, repository_id)
    embedder = Embedder()
    pending: list[_PendingFile] = []
    pending_chunks = 0
    kept: list[str] = []
    changed = 0
    unchanged = 0
    skipped = 0
    stored_chunks = 0

    files = list(iter_source_files(destination))
    total = len(files)
    _note(progress, phase="index", done=0, total=total, chunks=0, label=f"0 of {total} files")
    for done, (path, relative, language) in enumerate(files, start=1):
        text = read_text(path)
        if text is None:
            skipped += 1
            _note_file(progress, done, total, stored_chunks)
            continue
        kept.append(relative)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not force and known.get(relative) == digest:
            unchanged += 1
            _note_file(progress, done, total, stored_chunks)
            continue
        pending.append(
            _PendingFile(relative, language, digest, chunk_source(language, text))
        )
        pending_chunks += max(1, len(pending[-1].chunks))
        changed += 1
        if pending_chunks >= _EMBED_BATCH:
            stored_chunks += _flush(conn, embedder, repository_id, pending)
            pending = []
            pending_chunks = 0
            print(
                f"  {changed} files updated, {unchanged} unchanged, {stored_chunks} chunks stored",
                flush=True,
            )
        _note_file(progress, done, total, stored_chunks)

    if pending:
        stored_chunks += _flush(conn, embedder, repository_id, pending)
        _note_file(progress, total, total, stored_chunks)
    _delete_missing(conn, repository_id, kept)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE repositories
            SET branch = %s, commit_sha = %s, ingested_at = now()
            WHERE id = %s
            """,
            (checked_out, sha, repository_id),
        )
        cur.execute("ANALYZE chunks")
    conn.commit()
    elapsed = time.perf_counter() - started
    print(
        f"Indexed {name} {sha[:12]} ({checked_out}): "
        f"{changed} files updated, {unchanged} unchanged, "
        f"{skipped} skipped, {stored_chunks} chunks stored, {elapsed:.0f}s."
    )
    _note(
        progress,
        phase="done",
        done=total,
        total=total,
        chunks=stored_chunks,
        label=f"Indexed {changed} files, {stored_chunks} chunks",
    )
    return 0


def _note(progress: Callable[[dict], None] | None, **fields) -> None:
    if progress is not None:
        progress(fields)


def _note_file(progress: Callable[[dict], None] | None, done: int, total: int, chunks: int) -> None:
    _note(
        progress,
        phase="index",
        done=done,
        total=total,
        chunks=chunks,
        label=f"{done} of {total} files · {chunks} chunks stored",
    )


def _ensure_repository(conn, name: str, url: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repositories (name, url, branch, commit_sha)
            VALUES (%s, %s, 'pending', 'pending')
            ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
            RETURNING id
            """,
            (name, url),
        )
        repository_id = cur.fetchone()[0]
    conn.commit()
    return repository_id


def _existing_documents(conn, repository_id: int) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path, content_hash FROM documents WHERE repository_id = %s",
            (repository_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _flush(conn, embedder: Embedder, repository_id: int, pending: list[_PendingFile]) -> int:
    texts: list[str] = []
    owners: list[tuple[_PendingFile, Chunk]] = []
    for item in pending:
        for chunk in item.chunks:
            owners.append((item, chunk))
            texts.append(embed_text(item.path, item.language, chunk.symbol, chunk.content))
    vectors = embedder.embed_documents(texts) if texts else []
    stored = 0
    with conn.cursor() as cur:
        vector_index = 0
        for item in pending:
            cur.execute(
                """
                INSERT INTO documents (repository_id, path, language, content_hash)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repository_id, path) DO UPDATE
                SET language = EXCLUDED.language,
                    content_hash = EXCLUDED.content_hash
                RETURNING id
                """,
                (repository_id, item.path, item.language, item.content_hash),
            )
            document_id = cur.fetchone()[0]
            cur.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            for chunk in item.chunks:
                cur.execute(
                    """
                    INSERT INTO chunks (
                        repository_id, document_id, path, language, start_line, end_line,
                        symbol, kind, content, search_text, embedding
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    """,
                    (
                        repository_id,
                        document_id,
                        item.path,
                        item.language,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.symbol,
                        chunk.kind,
                        chunk.content,
                        search_text(item.path, chunk.symbol, chunk.content),
                        _vector_literal(vectors[vector_index]),
                    ),
                )
                vector_index += 1
                stored += 1
    conn.commit()
    return stored


def _delete_missing(conn, repository_id: int, kept: list[str]) -> None:
    with conn.cursor() as cur:
        if kept:
            cur.execute(
                """
                DELETE FROM documents
                WHERE repository_id = %s AND NOT (path = ANY(%s))
                """,
                (repository_id, kept),
            )
        else:
            cur.execute("DELETE FROM documents WHERE repository_id = %s", (repository_id,))
    conn.commit()


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in values) + "]"
