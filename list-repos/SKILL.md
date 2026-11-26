---
name: list-repos
description: >-
  List the indexed repositories in the code-rag MCP (the repositories tool),
  including each repository's branch, commit SHA, ingestion time, file and
  chunk counts, the model in use, the app start time, and whether an ingest is
  running. Use when the user asks which repositories are indexed, to see the
  indexed repos, or to check ingest status.
---

# List indexed repositories

Call the `repositories` tool on the `code-rag` MCP server. Do not run anything
else; the MCP tool is the source of truth.

The tool returns a JSON object with four indexed repositories, each with
`name` (owner/name), `branch`, `commit_sha`, `ingested_at`, `files`, and
`chunks`, plus `model` (the model in use, with `provider`, `name`,
`retrieval`, and `configured`), `started` (app start time), and `ingest`
(`running`, `repo`, `error`, `progress`).

Render the result as a markdown table with one row per repository:

| Repository | Branch | Commit | Ingested | Files | Chunks |
| --- | --- | --- | --- | --- | --- |

Then report the model in use (provider and name), the app start time, and the
ingest status (none running, or the running repo and error if any).

Example output:

```
Repository                          Branch    Commit        Ingested                Files  Chunks
hmcts/et-full-system                develop  4177fdb444f2  2026-11-17T17:41:13Z   346    2522
hmcts/hwf-publicapp                 master   b3063dc39ed1  2026-11-17T17:12:29Z   477    2913
hmcts/hwf-staffapp                  master   19fd6b35a125  2026-11-18T08:34:07Z   1354   12491
ministryofjustice/c100-application  master   e009daaac8fd  2026-11-17T18:59:36Z   1764   10066
Model in use: ollama inkling-2-mini:latest (configured)
App started: 2026-11-26 08:47 UTC
Ingest status: none running, no errors
```

If the `code-rag` server is unavailable (the MCP server fails to connect), say
so plainly and do not invent a list of repositories.
