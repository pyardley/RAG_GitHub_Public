"""Postgres schema and connection handling."""

from __future__ import annotations

import time
from typing import Any

import psycopg

from rag.config import DATABASE_URL, EMBEDDING_DIM, EMBEDDING_MODEL
from rag.files import parse_repo

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS repositories (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL,
        branch TEXT NOT NULL,
        commit_sha TEXT NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        id BIGSERIAL PRIMARY KEY,
        repository_id BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        language TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        UNIQUE (repository_id, path)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS chunks (
        id BIGSERIAL PRIMARY KEY,
        repository_id BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
        document_id BIGINT NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        language TEXT NOT NULL,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        symbol TEXT,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        search_text TEXT NOT NULL,
        embedding vector({EMBEDDING_DIM}) NOT NULL,
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
    )
    """,
    "CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repository_id)",
    "CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv)",
    """
    CREATE INDEX IF NOT EXISTS chunks_embedding_idx
        ON chunks USING hnsw (embedding vector_cosine_ops)
    """,
    """
    CREATE TABLE IF NOT EXISTS edges (
        id BIGSERIAL PRIMARY KEY,
        repository_id BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
        src_chunk_id BIGINT NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
        dst_chunk_id BIGINT REFERENCES chunks (id) ON DELETE CASCADE,
        dst_symbol TEXT,
        edge_type TEXT NOT NULL,
        confidence TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS edges_dst_chunk_idx ON edges (dst_chunk_id)",
    "CREATE INDEX IF NOT EXISTS edges_src_idx ON edges (src_chunk_id)",
]


def connect():
    last_error: Exception | None = None
    for _ in range(30):
        try:
            conn = psycopg.connect(DATABASE_URL)
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
            continue
        ensure_schema(conn)
        return conn
    raise SystemExit(f"Could not connect to Postgres at {DATABASE_URL}: {last_error}")


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
        cur.execute("SELECT value FROM settings WHERE key = 'embedding_model'")
        model_row = cur.fetchone()
        cur.execute("SELECT value FROM settings WHERE key = 'embedding_dim'")
        dim_row = cur.fetchone()
        if model_row is None:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s), (%s, %s)",
                ("embedding_model", EMBEDDING_MODEL, "embedding_dim", str(EMBEDDING_DIM)),
            )
        else:
            stored_model = model_row[0]
            stored_dim = dim_row[0] if dim_row else "?"
            if stored_model != EMBEDDING_MODEL or stored_dim != str(EMBEDDING_DIM):
                conn.rollback()
                raise SystemExit(
                    "This database was built with embedding model "
                    f"{stored_model} ({stored_dim} dimensions). "
                    f"The app is configured for {EMBEDDING_MODEL} ({EMBEDDING_DIM}). "
                    "Use the original model, or remove the volume and ingest again: "
                    "docker compose down -v"
                )
    conn.commit()
    _prefer_iterative_scan(conn)


def get_repository(conn, name: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, url, branch, commit_sha, ingested_at
            FROM repositories
            WHERE name = %s
            """,
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "url": row[2],
        "branch": row[3],
        "commit_sha": row[4],
        "ingested_at": row[5],
    }


def list_repositories(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.name, r.branch, r.commit_sha, r.ingested_at,
                   (SELECT count(*) FROM documents d WHERE d.repository_id = r.id) AS files,
                   (SELECT count(*) FROM chunks c WHERE c.repository_id = r.id) AS chunks
            FROM repositories r
            ORDER BY r.name
            """
        )
        rows = cur.fetchall()
    return [
        {
            "name": row[0],
            "branch": row[1],
            "commit_sha": row[2],
            "ingested_at": row[3],
            "files": row[4],
            "chunks": row[5],
        }
        for row in rows
    ]


def resolve_repositories(conn, repo: str | None) -> list[dict[str, Any]]:
    if repo:
        name, _ = parse_repo(repo)
        found = get_repository(conn, name)
        if found is None:
            known = ", ".join(item["name"] for item in list_repositories(conn)) or "(none)"
            raise SystemExit(f"Repository {name} is not indexed. Indexed repositories: {known}")
        return [found]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, url, branch, commit_sha, ingested_at FROM repositories ORDER BY name"
        )
        rows = cur.fetchall()
    if not rows:
        raise SystemExit(
            "No repositories are indexed. Ingest one first, for example:\n"
            "  docker compose run --rm -T rag ingest https://github.com/hmcts/hwf-publicapp"
        )
    return [
        {
            "id": row[0],
            "name": row[1],
            "url": row[2],
            "branch": row[3],
            "commit_sha": row[4],
            "ingested_at": row[5],
        }
        for row in rows
    ]


def _prefer_iterative_scan(conn) -> None:
    for statement in (
        "SET hnsw.iterative_scan = strict_order",
        "SET hnsw.ef_search = 100",
    ):
        try:
            conn.execute(statement)
            conn.commit()
        except psycopg.Error:
            conn.rollback()
