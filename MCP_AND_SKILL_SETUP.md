# Enabling the code-rag MCP and creating the list-repos skill

This file records what was done to get the `code-rag` MCP server working in this
project and to turn the `list-repos` skill into a runnable command. It is a record
of the steps, not a reference to the server itself — see `README.md` for the full
MCP documentation and `API.md` for the HTTP API.

## What the MCP is

`mcp/server.py` is a stdio MCP server. It speaks JSON-RPC to the client (Claude
Code, Cursor) on standard input and calls the RAG app over HTTP at
`http://127.0.0.1:8080`. It exposes the tools `repositories`, `branches`,
`default_branch`, `features`, `flow`, `impact`, `ingest`, `query`,
`remove_index`, `search`, and `testing`. The app must already be running for any
of them to answer.

The server was already configured before this session — the project root ships
`.mcp.json` (added in `df3cc70`, "Add a project .mcp.json so Claude Code can use
the MCP server"). It is not something this session created.

## Steps taken to enable the MCP

### 1. Start with the configured server, not a new one

The first attempt was to use the `code-pet` MCP server. It failed to connect:

```
ENOENT: no such file or directory, posix_spawn 'python3 /Users/paul.yardley/projects/RAG_GitHub_Public/mcp/server.py'
```

That error names the same `mcp/server.py` the `code-rag` server uses, so the
server file exists but the client could not spawn it. The request was redirected
to the `code-rag` server instead, which is the server this project is configured
to use.

### 2. Use the code-rag configuration that already exists

Two config files exist in the repo, one per client:

`.mcp.json` (Claude Code, including the VS Code extension):

```json
{
  "mcpServers": {
    "code-rag": {
      "type": "stdio",
      "command": "${CODE_RAG_PYTHON:-python3}",
      "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp/server.py"],
      "env": {
        "CODE_RAG_URL": "${CODE_RAG_URL:-http://127.0.0.1:8080}"
      }
    }
  }
}
```

`.cursor/mcp.json` (Cursor):

```json
{
  "mcpServers": {
    "code-rag": {
      "command": "python",
      "args": ["${workspaceFolder}/mcp/server.py"],
      "env": {
        "CODE_RAG_URL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

`.mcp.json` uses `python3`, the name macOS and Linux use. On Windows, where the
command is `python`, set `CODE_RAG_PYTHON=python` before starting VS Code or
`claude`.

### 3. Confirm the app is answering

The server is a thin client over the app. Before asking the agent to use the
tools, check that the app is up:

```bash
curl http://127.0.0.1:8080/api/status
```

`README.md` documents this for Cursor. The same check applies to Claude Code.

### 4. Approve the server in the client

Claude Code does not read `.cursor/mcp.json`. It reads `.mcp.json` in the project
root. The documented steps are: open the project folder, type `/mcp` in the Claude
chat, and approve `code-rag`. Until it is approved, it is listed as pending and
its tools are not offered. Start a new chat after the approval, because an open
chat does not gain the tools.

In this session, `/mcp` and `/mcp enable code-rag` were not recognized actions
("isn't a recognized /mcp action"; "Reconnect, enable, and disable aren't
available in this session"). The server was not switched on through that command,
but the `code-rag` tools were still offered to the session and were used. Check
what Claude Code has loaded with:

```bash
claude mcp get code-rag
```

### 5. Invoke the tool

The tools are invoked through the deferred ToolSearch mechanism:

1. `ToolSearch` with query `select:mcp__code-rag__repositories` loads the schema.
2. The tool is then called directly.

The `repositories` tool returns a JSON object: `repositories[]` (each with `name`,
`branch`, `commit_sha`, `ingested_at`, `files`, `chunks`), `model` (provider, name,
retrieval, configured), `started` (app start time), and `ingest` (running, repo,
error, progress).

Example output from this session:

| Repository | Branch | Commit | Ingested | Files | Chunks |
| --- | --- | --- | --- | --- | --- |
| hmcts/et-full-system | develop | 4177fdb444f2 | 2026-11-17T17:41:13Z | 346 | 2522 |
| hmcts/hwf-publicapp | master | b3063dc39ed1 | 2026-11-17T17:12:29Z | 477 | 2913 |
| hmcts/hwf-staffapp | master | 19fd6b35a125 | 2026-11-18T08:34:07Z | 1354 | 12491 |
| ministryofjustice/c100-application | master | e009daaac8fd | 2026-11-17T18:59:36Z | 1764 | 10066 |

Model in use: `ollama inkling-2-mini:latest` (configured). App started:
2026-11-26 08:47 UTC. Ingest: not running, no errors.

## Steps taken to create the skill

### 1. Create the skill file at the project root

The skill is a directory with a `SKILL.md`, following the existing
`implement-hwf-feature/SKILL.md` pattern. It was created at:

```
list-repos/SKILL.md
```

It has YAML frontmatter (`name: list-repos`, a `description` with "Use when..."
guidance) and a body that tells the agent to call the `repositories` tool on the
`code-rag` MCP server, render one markdown table row per repository, and report
the model, app start time, and ingest status. It also states what to do when the
server is unavailable: say so plainly and do not invent a list.

A root-level skill folder does **not** register as a slash command. Claude Code
only discovers skills under `.claude/skills/<name>/SKILL.md` (project level) or
`~/.claude/skills/<name>/SKILL.md` (user level). The frontmatter `name` must
match the directory name. `implement-hwf-feature/SKILL.md` has the same shape and
is equally unregistered.

### 2. Wire it into `.claude/skills/` as a symlink

To make the skill runnable as `/list-repos`, the skill was linked into the
discovery path while keeping the file at its canonical path:

```bash
mkdir -p .claude/skills/list-repos
ln -s ../../../list-repos/SKILL.md .claude/skills/list-repos/SKILL.md
```

The relative path climbs three levels from `.claude/skills/list-repos/` to the
project root, then `list-repos/SKILL.md`.

The first attempt used only two levels (`../../list-repos/SKILL.md`), which
landed on `.claude/list-repos/SKILL.md` — nonexistent, and `cat` failed with "No
such file or directory". The fix was to remove the bad link and recreate it with
three levels:

```bash
rm .claude/skills/list-repos/SKILL.md
ln -s ../../../list-repos/SKILL.md .claude/skills/list-repos/SKILL.md
```

### 3. Verify the wiring

- `cat .claude/skills/list-repos/SKILL.md` resolves the full skill content.
- `git check-ignore .claude` exits 1 — `.claude/` is not gitignored.
- `git status --short` shows `.claude/` and `list-repos/` as untracked
  (`?? .claude/`, `?? list-repos/`), so both are committable.
- The harness's available-skills list includes `list-repos`.
- The skill was invoked through the Skill tool and ran the `repositories` tool
  end-to-end, returning the same JSON as above.

## What is deliberately not done

- Nothing is committed. `.claude/` and `list-repos/` are untracked and left for
  the user to commit.
- The `.env` file is not read, copied, or committed. It is gitignored.
- `implement-hwf-feature/SKILL.md` is not wired the same way; it stays at the
  project root and remains unregistered. Wiring it the same way is a separate,
  unrequested step.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ENOENT ... posix_spawn 'python3 .../mcp/server.py'` | Client could not spawn the server | Use the `code-rag` server instead; check the app is up at `http://127.0.0.1:8080/api/status`; on Windows set `CODE_RAG_PYTHON=python` |
| `/mcp` or `/mcp enable code-rag` not recognized | Action unavailable in this session | Use the tools directly via the harness; check with `claude mcp get code-rag` |
| `cat` on the symlink fails with "No such file or directory" | Relative path climbs too few levels | Recreate with `../../../list-repos/SKILL.md` (three levels) |
| Skill not in the available-skills list | Skill file not under `.claude/skills/<name>/SKILL.md` | Symlink into `.claude/skills/`; name must match the directory |
