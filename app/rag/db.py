"""Postgres schema and connection handling."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import psycopg

from rag.config import (
    CODE_EMBEDDING_DIM,
    CODE_EMBEDDING_MODEL,
    DATABASE_URL,
    TEXT_EMBEDDING_DIM,
    TEXT_EMBEDDING_MODEL,
)
from rag.files import parse_repo

TEXT_INDEX = "chunks_embedding_text_idx"
CODE_INDEX = "chunks_embedding_code_idx"
LEGACY_INDEX = "chunks_embedding_idx"

# Tables only. The embedding indexes are created by migration_statements so an
# existing database can rename its column before the new indexes are built.
TABLE_STATEMENTS = [
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
        embedding_text vector({TEXT_EMBEDDING_DIM}) NOT NULL,
        embedding_code vector({CODE_EMBEDDING_DIM}),
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
    )
    """,
    "CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repository_id)",
    "CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv)",
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

SCHEMA_STATEMENTS = TABLE_STATEMENTS

_VECTOR_TYPE = re.compile(r"(?:[A-Za-z_][\w]*\.)?vector\((\d+)\)")


@dataclass(frozen=True)
class ChunkSchema:
    """Columns and index names on ``chunks``, as the catalogue reports them."""

    columns: dict[str, str]
    indexes: frozenset[str]


def migration_statements(schema: ChunkSchema) -> list[str]:
    """Statements that bring ``chunks`` onto the two embedding columns.

    Safe to run more than once. A second call, after the renames have been
    applied, only repeats ``IF NOT EXISTS`` additions. The old ``embedding``
    column is renamed: 384 dimensions become ``embedding_text``, and 768
    dimensions become ``embedding_code`` because that volume was built with
    the code model alone. The old HNSW index is renamed in that same pass,
    while this plan still knows which column it was built on.
    """
    columns = dict(schema.columns)
    statements: list[str] = []
    legacy = columns.get("embedding")
    if legacy is not None:
        dimension = _vector_dimension(legacy)
        if dimension == TEXT_EMBEDDING_DIM:
            _rename_column(statements, columns, "embedding", "embedding_text")
        elif dimension == CODE_EMBEDDING_DIM:
            _rename_column(statements, columns, "embedding", "embedding_code")
        else:
            raise SystemExit(
                "This database has an embedding column of "
                f"{legacy}, which is neither the text model "
                f"({TEXT_EMBEDDING_DIM} dimensions) nor the code model "
                f"({CODE_EMBEDDING_DIM} dimensions). "
                "Remove the volume and ingest again: docker compose down -v"
            )
    _require_dimension(columns, "embedding_text", TEXT_EMBEDDING_DIM, TEXT_EMBEDDING_MODEL)
    _require_dimension(columns, "embedding_code", CODE_EMBEDDING_DIM, CODE_EMBEDDING_MODEL)
    if LEGACY_INDEX in schema.indexes and _renamed_to(statements, "embedding_text") and TEXT_INDEX not in schema.indexes:
        statements.append(f"ALTER INDEX {LEGACY_INDEX} RENAME TO {TEXT_INDEX}")
    elif LEGACY_INDEX in schema.indexes and _renamed_to(statements, "embedding_code") and CODE_INDEX not in schema.indexes:
        statements.append(f"ALTER INDEX {LEGACY_INDEX} RENAME TO {CODE_INDEX}")
    statements.append(
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS "
        f"embedding_text vector({TEXT_EMBEDDING_DIM})"
    )
    statements.append(
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS "
        f"embedding_code vector({CODE_EMBEDDING_DIM})"
    )
    statements.append(
        f"CREATE INDEX IF NOT EXISTS {TEXT_INDEX} "
        "ON chunks USING hnsw (embedding_text vector_cosine_ops)"
    )
    statements.append(
        f"CREATE INDEX IF NOT EXISTS {CODE_INDEX} "
        "ON chunks USING hnsw (embedding_code vector_cosine_ops)"
    )
    return statements


def settings_plan(stored: dict[str, str]) -> dict[str, str]:
    """Settings to store for the two models.

    An empty database, or one whose single model was the text model, records
    both. A database whose single model was the code model records that too,
    so the renamed column is not treated as a mismatch. Anything else stops
    the process: those vectors cannot be read as either index.
    """
    desired = {
        "embedding_text_model": TEXT_EMBEDDING_MODEL,
        "embedding_text_dim": str(TEXT_EMBEDDING_DIM),
        "embedding_code_model": CODE_EMBEDDING_MODEL,
        "embedding_code_dim": str(CODE_EMBEDDING_DIM),
    }
    text_model = stored.get("embedding_text_model")
    code_model = stored.get("embedding_code_model")
    if text_model is None and code_model is None:
        legacy_model = stored.get("embedding_model")
        legacy_dim = stored.get("embedding_dim")
        if legacy_model is not None and not _legacy_matches(legacy_model, legacy_dim):
            raise SystemExit(
                "This database was built with embedding model "
                f"{legacy_model} ({legacy_dim or '?'} dimensions). "
                f"The text index is {TEXT_EMBEDDING_MODEL} ({TEXT_EMBEDDING_DIM}) "
                f"and the code index is {CODE_EMBEDDING_MODEL} ({CODE_EMBEDDING_DIM}). "
                "Use those models, or remove the volume and ingest again: "
                "docker compose down -v"
            )
        return desired
    _require_setting(stored, "embedding_text_model", TEXT_EMBEDDING_MODEL, "embedding_text_dim", TEXT_EMBEDDING_DIM)
    _require_setting(stored, "embedding_code_model", CODE_EMBEDDING_MODEL, "embedding_code_dim", CODE_EMBEDDING_DIM)
    return desired


def _legacy_matches(model: str, dim: str | None) -> bool:
    return (model == TEXT_EMBEDDING_MODEL and dim == str(TEXT_EMBEDDING_DIM)) or (
        model == CODE_EMBEDDING_MODEL and dim == str(CODE_EMBEDDING_DIM)
    )


def _require_setting(stored: dict[str, str], model_key: str, model: str, dim_key: str, dim: int) -> None:
    stored_model = stored.get(model_key)
    stored_dim = stored.get(dim_key)
    if stored_model == model and stored_dim == str(dim):
        return
    raise SystemExit(
        f"This database records {model_key} as {stored_model} ({stored_dim or '?'} dimensions). "
        f"The app is configured for {model} ({dim}). "
        "Use the original models, or remove the volume and ingest again: "
        "docker compose down -v"
    )


def _rename_column(statements: list[str], columns: dict[str, str], source: str, target: str) -> None:
    if target in columns:
        raise SystemExit(
            f"chunks has both {source} and {target}. "
            "Leave one of them, or remove the volume and ingest again: docker compose down -v"
        )
    statements.append(f"ALTER TABLE chunks RENAME COLUMN {source} TO {target}")
    columns[target] = columns.pop(source)


def _renamed_to(statements: list[str], target: str) -> bool:
    return any(statement.endswith(f" TO {target}") for statement in statements)


def _require_dimension(columns: dict[str, str], name: str, expected: int, model: str) -> None:
    declared = columns.get(name)
    if declared is None:
        return
    dimension = _vector_dimension(declared)
    if dimension != expected:
        raise SystemExit(
            f"Column {name} is {declared}, but {model} stores {expected} dimensions. "
            "Remove the volume and ingest again: docker compose down -v"
        )


def _vector_dimension(type_name: str) -> int | None:
    matched = _VECTOR_TYPE.fullmatch(type_name.strip())
    if matched is None:
        return None
    return int(matched.group(1))


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
        for statement in TABLE_STATEMENTS:
            cur.execute(statement)
        schema = _read_chunk_schema(cur)
        if schema is None:
            raise SystemExit("The chunks table was not created")
        try:
            for statement in migration_statements(schema):
                cur.execute(statement)
            _apply_settings(cur)
        except SystemExit:
            conn.rollback()
            raise
    conn.commit()
    _prefer_iterative_scan(conn)


def _read_chunk_schema(cur) -> ChunkSchema | None:
    cur.execute("SELECT to_regclass('public.chunks')")
    if cur.fetchone()[0] is None:
        return None
    cur.execute(
        """
        SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod)
        FROM pg_catalog.pg_attribute a
        WHERE a.attrelid = 'public.chunks'::regclass
          AND a.attnum > 0
          AND NOT a.attisdropped
        """
    )
    columns = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'chunks'
        """
    )
    return ChunkSchema(columns, frozenset(row[0] for row in cur.fetchall()))


def _apply_settings(cur) -> None:
    cur.execute("SELECT key, value FROM settings")
    stored = {row[0]: row[1] for row in cur.fetchall()}
    for key, value in settings_plan(stored).items():
        if stored.get(key) == value:
            continue
        cur.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )


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
