-- Runs once, when the Postgres volume is first created.
-- The application also creates these objects on startup, and migrates a
-- volume that still has the single embedding column.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    branch TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE (repository_id, path)
);

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
    embedding_text vector(384) NOT NULL,
    embedding_code vector(768),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);

CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repository_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_text_idx
    ON chunks USING hnsw (embedding_text vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_embedding_code_idx
    ON chunks USING hnsw (embedding_code vector_cosine_ops);

CREATE TABLE IF NOT EXISTS edges (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    src_chunk_id BIGINT NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    dst_chunk_id BIGINT REFERENCES chunks (id) ON DELETE CASCADE,
    dst_symbol TEXT,
    edge_type TEXT NOT NULL,
    confidence TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS edges_dst_chunk_idx ON edges (dst_chunk_id);
CREATE INDEX IF NOT EXISTS edges_src_idx ON edges (src_chunk_id);
