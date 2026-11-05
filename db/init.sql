-- Runs once, when the Postgres volume is first created.
-- The application also creates this extension on startup.
CREATE EXTENSION IF NOT EXISTS vector;
