# HTTP API

The app at `http://127.0.0.1:8080` answers JSON. Start it with `docker compose up -d` before calling anything. These are the same routes the page and the MCP server use. A program can call them directly. The MCP server does not add a second API: it posts to these routes and shortens the passage text before it returns the result to the model.

There is no authentication. The server does not send CORS headers, so call it from a script or another process on this machine. A page served from a different origin cannot call it from the browser.

POST bodies are JSON objects. The largest accepted body is 1 MB. Send `Content-Type: application/json`. An empty body, a body that is not JSON, or a JSON value that is not an object is rejected.

A failed call returns an object with one field:

```json
{"error": "Enter a question"}
```

| Status | When |
| --- | --- |
| 200 | The call finished. |
| 202 | An ingest was accepted and is running in the background. |
| 400 | The body or a query parameter was rejected. `error` is the reason. |
| 404 | The path is unknown, or GitHub could not be read for a branch lookup. |
| 409 | An ingest is already running. The body also includes `ingest`. |
| 500 | The call failed while running. |
| 502 | File history could not be read from git. |
| 503 | Postgres could not be reached. `GET /api/status` also includes `ingest` and `started`. |

A written answer waits on the language model. With Ollama that wait can run to `OLLAMA_TIMEOUT`, which defaults to 600 seconds. Give `query`, `features`, `testing`, and `impact` at least that long when `answer` is left on. `search` does not call the model.

`answer` defaults to true on these routes. Set it to false when you only want the passages. The MCP tools default the other way and leave the app's written answer off unless you ask for it.

`repo` accepts `owner/name`, an `https://github.com/owner/name` URL, or `git@github.com:owner/name.git`. Omit it to use every indexed repository. A name that is not indexed returns 400 and lists the ones that are.

## Contents

- [Calling it](#calling-it)
- [Objects](#objects)
- [GET /api/status](#get-apistatus)
- [GET /api/branches](#get-apibranches)
- [GET /api/default-branch](#get-apidefault-branch)
- [GET /api/freshness](#get-apifreshness)
- [POST /api/query](#post-apiquery)
- [POST /api/search](#post-apisearch)
- [POST /api/features](#post-apifeatures)
- [POST /api/testing](#post-apitesting)
- [POST /api/impact](#post-apiimpact)
- [POST /api/flow](#post-apiflow)
- [POST /api/history](#post-apihistory)
- [POST /api/ingest](#post-apiingest)
- [POST /api/remove](#post-apiremove)

## Calling it

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/status
```

```powershell
$body = @{
    question = "where is the application submitted"
    repo = "hmcts/hwf-publicapp"
    answer = $false
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/query -ContentType "application/json" -Body $body
```

The same call in Python, using only the standard library:

```python
import json
import urllib.request

def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:8080" + path,
        data=data,
        headers={} if data is None else {"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)

passages = call("POST", "/api/query", {
    "question": "where is the application submitted",
    "repo": "hmcts/hwf-publicapp",
    "answer": False,
})
```

`urllib` raises on a status other than 200. Read the response body of that error for the `error` string. `202` from ingest is a success for this purpose.

## Objects

These objects appear on more than one route.

### Repository

Returned by status, freshness is separate, and by remove.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | `owner/name`. |
| `branch` | string | Branch that was indexed. |
| `commit_sha` | string | First 12 characters of the indexed commit. |
| `ingested_at` | string | ISO-8601 time the index was stored. |
| `files` | integer | Files stored for this repository. |
| `chunks` | integer | Chunks stored for this repository. |

A retrieval response uses a shorter repository object: `name`, `branch`, and `commit_sha` only.

### Model

| Field | Type | Meaning |
| --- | --- | --- |
| `configured` | boolean | A language model is available to write answers. |
| `provider` | string | `openai` or `ollama`. |
| `name` | string or null | Model that writes the answer. Null when none is configured. |
| `retrieval` | string or null | Model that turns a question into searches. |
| `hint` | string or null | How to configure a model, when none is configured. |

### Hit

One retrieved passage. The HTTP response includes the full chunk. The MCP server cuts `content` at 1500 characters.

| Field | Type | Meaning |
| --- | --- | --- |
| `repo` | string | `owner/name`. |
| `path` | string | Path inside the repository. |
| `start_line` | integer | First line of the chunk, 1-based. |
| `end_line` | integer | Last line of the chunk. |
| `symbol` | string or null | Method, class, or scenario name when the chunk has one. |
| `kind` | string | What the chunk is, such as a class, method, or scenario. |
| `content` | string | The chunk text. |
| `score` | number | Closeness to the question, rounded to 4 decimal places. |
| `url` | string | Link to those lines on the indexed commit. Present when the commit and path can form a GitHub URL. |
| `via` | string | Call path, on an impact reference that was reached through other names. A `⇢` in the path is an uncertain hop. |
| `possible` | boolean | True when `via` contains an uncertain hop. |

### Usage

Present on a finished query, search, features, testing, impact, flow, or history response.

| Field | Type | Meaning |
| --- | --- | --- |
| `seconds` | number | Total of the stages. |
| `prompt_tokens` | integer | Language-model prompt tokens. Embedding adds none. |
| `completion_tokens` | integer | Language-model completion tokens. |
| `total_tokens` | integer | Sum of the two token counts. |
| `stages` | array | One object per stage: `name`, `seconds`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `model`, `note`. A count is null when that stage did not call a model. |

### Ingest

| Field | Type | Meaning |
| --- | --- | --- |
| `running` | boolean | This server is building an index. |
| `elsewhere` | boolean | Another process holds the ingest lock. The page shows this as "Another process is building an index". |
| `repo` | string or null | Repository the running ingest was given. |
| `log` | string | Tail of the ingest log, kept after it finishes. At most 20000 characters. |
| `error` | string or null | Why the last ingest failed. |
| `progress` | object or null | `phase` is `clone`, `index`, or `done`. `done` and `total` count files. `chunks` is how many chunks are stored. `label` is the line the page shows. `percent` is 0 to 100, or null while `total` is 0. |

Only one index is built at a time. A second ingest is refused whether it came from this API, the page, or the command line.

## GET /api/status

What is indexed, which model is configured, and whether an ingest is running. This does not ask GitHub. Poll it while an ingest runs.

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/status
```

```json
{
  "repositories": [
    {
      "name": "hmcts/hwf-publicapp",
      "branch": "master",
      "commit_sha": "abc123def456",
      "ingested_at": "2026-11-21T12:00:00+00:00",
      "files": 420,
      "chunks": 3100
    }
  ],
  "model": {
    "configured": true,
    "provider": "ollama",
    "name": "qwen2.5-coder:32b-instruct-q5_K_M",
    "retrieval": "qwen2.5-coder:7b",
    "hint": null
  },
  "ingest": {
    "running": false,
    "elsewhere": false,
    "repo": null,
    "log": "",
    "error": null,
    "progress": null
  },
  "started": "2026-11-23 17:00 UTC"
}
```

`started` is when this process began, in `YYYY-MM-DD HH:MM UTC`.

## GET /api/branches

Branch names on GitHub for a repository that may not be indexed yet. The default branch is first. The lookup runs `git ls-remote`.

Query parameter `repo` is required.

```powershell
Invoke-RestMethod "http://127.0.0.1:8080/api/branches?repo=hmcts/hwf-publicapp"
```

```json
{
  "default": "master",
  "branches": ["master", "develop"]
}
```

400 when `repo` is missing or not `owner/name` or a GitHub URL. 404 when the remote cannot be read, with `error` set to `The branches could not be read`.

## GET /api/default-branch

The default branch only. Same `repo` parameter and same failure cases as `/api/branches`. A 404 here says `The default branch could not be read`.

```json
{"branch": "master"}
```

## GET /api/freshness

Compares each indexed commit with the commit that branch points at on GitHub. An answer is reused for ten minutes. `?refresh=1` asks GitHub again. Each remote lookup waits up to 20 seconds. A branch that cannot be read has `remote` null and `stale` false.

```powershell
Invoke-RestMethod "http://127.0.0.1:8080/api/freshness?refresh=1"
```

```json
{
  "repositories": [
    {
      "name": "hmcts/hwf-publicapp",
      "branch": "master",
      "indexed": "abc123def456",
      "remote": "def456abc123",
      "stale": true
    }
  ]
}
```

`indexed` and `remote` are the first 12 characters of the commit. `stale` is true when the remote commit was read and is not the indexed commit.

## POST /api/query

Nearest passages for a question, and a written answer when `answer` is true and a model is configured.

```json
{
  "question": "where is the application submitted",
  "repo": "hmcts/hwf-publicapp",
  "k": 12,
  "answer": false,
  "index": "code",
  "fusion": "rrf",
  "paths": ["app/controllers/submissions_controller.rb"]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `question` | yes | The question. |
| `repo` | no | Limit the search to one repository. |
| `k` | no | How many passages to return. An integer from 1 to 30. The default is 12, or `RAG_K` when that is set. |
| `answer` | no | Write a reply from the passages. Default true. Set false to skip the model. |
| `index` | no | `text`, `code`, or `both`. Default `code`, or `RAG_INDEX`. `code` uses the code embedding model. `text` uses the general-English model. `both` searches both and merges the rankings. |
| `fusion` | no | `rrf` or `score`. Default `rrf`, or `RAG_FUSION`. `rrf` is reciprocal rank fusion. `score` adds the cosine similarity to the keyword rank. Use `score` with `index` `both`. |
| `paths` | no | Files or directories to search. Other files are left out. At most 400 paths are kept. |
| `nodes`, `edges` | no | A flow chart already returned by `/api/flow`. See [Asking about a journey](#asking-about-a-journey). |

```json
{
  "repositories": [
    {"name": "hmcts/hwf-publicapp", "branch": "master", "commit_sha": "abc123def456"}
  ],
  "queries": ["application submitted", "submissions controller"],
  "index": "code",
  "fusion": "rrf",
  "truncated": false,
  "passage_limit": 12,
  "hits": [],
  "matches": [],
  "answer": null,
  "answer_error": null,
  "usage": {"seconds": 1.2, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "stages": []},
  "model": {"configured": true, "provider": "ollama", "name": "qwen2.5-coder:7b", "retrieval": "qwen2.5-coder:7b", "hint": null}
}
```

`queries` is the list of searches the retrieval model produced, or the question itself when that step is skipped. `truncated` is true when more matches exist than `k`. `hits` is the passages, closest first. `matches` is lines inside those passages that match the question: `{path, line, text}`.

`answer` is the written reply, or null when `answer` was false, no passages were found, or no model is configured. `answer_error` is set when a reply was requested and the model failed. The reply is Markdown and cites paths and line numbers. It is written only from the passages.

A question that asks what calls a class or method is answered as an impact lookup. The body is the [impact](#post-apiimpact) shape, with two extra fields: `routed` is `"impact"` and `target` is the name that was looked up. That routing is skipped when `nodes` are sent.

400 messages you will see: `Enter a question`, `Passages must be a number from 1 to 30`, `Index must be text, code, or both`, `Fusion must be rrf or score`, `Paths must be a list`, and `Repository <name> is not indexed. Indexed repositories: ...`.

### Asking about a journey

Send the chart's `nodes` and `edges` back with the question, and send the same `repo`. A question that names a step and an answer on an arrow returns that arrow and does not search:

```json
{
  "journey": true,
  "branches": [
    {
      "from_id": "fee",
      "from": "Fee",
      "label": "yes",
      "to_id": "income",
      "to": "Income",
      "path": "app/models/forms/income.rb",
      "url": "https://github.com/hmcts/hwf-publicapp/blob/abc123def456/app/models/forms/income.rb#L1"
    }
  ],
  "hits": [],
  "queries": [],
  "usage": {}
}
```

Any other question is searched only in the files for the steps on that chart. The response then has `journey` true and `scoped` true, plus the usual `hits`. When the chart has no files, `hits` is empty and `message` is `This journey has no files to search.`

`paths` is the same restriction without sending the chart. Each entry is a file or directory relative to the repository root. `..` is dropped.

A chart is rejected with `Journey must be a chart` when `nodes` or `edges` is not a list, and with `Journey is too large` when there are more than 400 nodes or 800 edges.

## POST /api/search

Passages that contain the text. A quoted phrase is matched exactly. A regular expression inside quotes is used as written. This route does not write an answer.

```json
{
  "question": "look under /features for \"with income '8000' and average\"",
  "repo": "hmcts/hwf-publicapp",
  "k": 12
}
```

`question` is required. `repo` and `k` match query. `index` and `fusion` are accepted and behave as they do on query; omit them to use the defaults. The response has the same fields as query. `answer` and `answer_error` are null.

400 when `question` is blank: `Enter a search`.

## POST /api/features

Cucumber steps already in the suite. A pasted Gherkin line is matched as that line, including its step definition. A request to write a feature is searched as the behaviour. The question planner is not used. The search stays on the code index with `rrf`.

```json
{
  "question": "When I submit the application",
  "repo": "hmcts/hwf-publicapp",
  "k": 12,
  "answer": true
}
```

The response matches query, with `index` `"code"`, `fusion` `"rrf"`, and one extra field: `drafting` is true when the question was treated as a feature to write. When `drafting` is true and `answer` is true, the written answer quotes only steps that appear in the passages.

400 when `question` is blank: `Enter a feature or a step`.

## POST /api/testing

How to run the Cucumber suite under `features/`, a group of scenarios, or one scenario, and how to debug a scenario that is being written. Commands are quoted from that repository. RSpec and Minitest are left out. The search stays on the code index with `rrf`.

```json
{
  "question": "how do I run the fee scenario",
  "repo": "hmcts/hwf-publicapp",
  "answer": true
}
```

The response matches features, with `testing` true instead of `drafting`.

400 when `question` is blank: `Enter a test to run or debug`.

## POST /api/impact

Definitions, callers, and a diagram for a class, method, file, or FactoryBot call such as `FactoryBot.create(:claim)`.

```json
{
  "target": "OnlineApplication",
  "repo": "hmcts/hwf-publicapp",
  "answer": false
}
```

`target` is required. `repo` is optional. `answer` defaults to true. There is no `k`; the lookup returns the definitions and references it found, and the written answer reads a short sample of them.

```json
{
  "repositories": [],
  "definitions": [],
  "references": [],
  "related": [],
  "gaps": [],
  "definition_count": 1,
  "reference_count": 4,
  "truncated": false,
  "callers_omitted": 0,
  "callers_capped": false,
  "possible_count": 0,
  "diagram": {"nodes": [], "edges": []},
  "answer": null,
  "answer_error": null,
  "usage": {},
  "model": {}
}
```

`definitions`, `references`, `related`, and `gaps` are [hits](#hit). A reference reached through a call has `via`. `possible_count` is how many of those hops are uncertain. `callers_omitted` is callers left out of the list, and `callers_capped` is true when that list was cut. `truncated` is true when the result was cut. `definition_count` and `reference_count` count what was found, including omitted callers.

Each diagram node is `{id, label, kind, depth, path}`. `kind` is `definition` or `caller`. `depth` is 0 on the definition and increases back along the callers. `path` is the file, when one is known. Each edge is `{from, to}` and points toward the definition. An uncertain edge also has `possible` true. A mention with no call path is left off the diagram.

400 when `target` is blank: `Enter a class, method, or file`.

## POST /api/flow

Journeys found in the indexed checkout. Help with Fees comes from the application's question list or its redirects. A test suite comes from its page objects and the journey that clicks through them. A start path such as `/steps/opening/start_or_continue` draws the journey an application chooses in its decision tree.

```json
{
  "repo": "ministryofjustice/c100-application",
  "start": "/steps/opening/start_or_continue"
}
```

Both fields are optional. Omit `repo` to read every indexed repository. Omit `start` unless you want a decision-tree journey.

```json
{
  "repositories": [],
  "charts": [
    {
      "title": "Apply for help with fees",
      "repository": "hmcts/hwf-publicapp",
      "note": "Each box opens the file for that step.",
      "nodes": [
        {
          "id": "fee",
          "label": "Fee",
          "kind": "step",
          "path": "app/models/forms/fee.rb",
          "start_line": 1,
          "end_line": 1,
          "url": "https://github.com/hmcts/hwf-publicapp/blob/abc123def456/app/models/forms/fee.rb#L1"
        }
      ],
      "edges": [
        {"from": "fee", "to": "income", "label": "yes"}
      ]
    }
  ],
  "message": "",
  "usage": {}
}
```

When more than one repository is drawn, each `title` is prefixed with `owner/name — `. An arrow label is the answer that chooses it. An empty label follows the question list.

A node may include `visited` true, `visit_count`, and `visits` (up to four feature or step-definition paths) when a feature names that step. `kind` is `step` or `end`.

When the branch has commits newer than the index, a chart includes `changes`:

| Field | Meaning |
| --- | --- |
| `remote` | First 12 characters of the branch tip. |
| `added` | Step labels present at the tip and not in the index. At most 12. |
| `removed` | Step labels in the index and not at the tip. At most 12. |
| `rewired` | Steps whose outgoing arrows differ. Each item has `id`, `label`, `before`, and `after`. |
| `added_count`, `removed_count`, `rewired_count` | Totals before the lists were cut. |
| `unchanged` | True when nothing was added, removed, or rewired. |
| `unavailable` | True when the tip could not be compared. |

`message` is empty when every selected repository produced a chart. Otherwise it says the checkout is missing, the start path was not found, or no journey was found. A missing checkout asks you to index that repository again.

## POST /api/history

Recent versions of one file on the indexed repository's branch. The versions come from a local git clone. The indexed checkout stays on the commit that was embedded. At most twelve versions are returned, oldest of that window first. This route is not exposed by MCP.

```json
{
  "repo": "hmcts/hwf-publicapp",
  "path": "app/helpers/application_helper.rb",
  "block": "address_lookup_access_token"
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `repo` | yes | One indexed repository. |
| `path` | yes | File path inside the repository, or a GitHub blob URL for that file. |
| `block` | no | A method, class, or scenario. Blank means the whole file. When set, only commits that change that block are kept, and each version's text is the block alone. |

```json
{
  "repositories": [],
  "repo": "hmcts/hwf-publicapp",
  "branch": "master",
  "path": "app/helpers/application_helper.rb",
  "block": "address_lookup_access_token",
  "symbol": "address_lookup_access_token",
  "truncated": true,
  "versions": [
    {
      "sha": "0123456789abcdef0123456789abcdef01234567",
      "short": "0123456",
      "date": "2026-11-01T09:00:00+00:00",
      "author": "A Developer",
      "message": "Look up the address token",
      "url": "https://github.com/hmcts/hwf-publicapp/commit/0123456789abcdef0123456789abcdef01234567",
      "indexed": false,
      "text": "def address_lookup_access_token\nend\n",
      "omitted": null,
      "start_line": 40,
      "symbol": "address_lookup_access_token"
    }
  ],
  "usage": {}
}
```

`indexed` is true on the version that is the indexed commit. `text` is the file or the block. `omitted` is `deleted`, `too large`, or `binary` when the text is left out, and null otherwise. `truncated` is true when older commits exist beyond the twelve shown, or the clone is still shallow. `start_line` and `symbol` are present on a block version.

400 when the repository is missing (`Choose one repository`), the path is missing (`Enter a file path`), or the block name is not a single method, class, or scenario. 502 when git cannot produce the history. The first call for a repository clones it, so that call can take a few minutes.

## POST /api/ingest

Start an index build. The call returns as soon as the build has started. Poll `GET /api/status` until `ingest.running` is false, then read `ingest.error` and `ingest.progress`.

```json
{
  "repo": "https://github.com/hmcts/hwf-publicapp",
  "branch": "master",
  "force": false
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `repo` | yes | Repository to index. |
| `branch` | no | Branch to check out. Omit it to use the remote default. |
| `force` | no | Build again even when this commit is already indexed. Default false. |

202:

```json
{"ingest": {"running": true, "elsewhere": false, "repo": "hmcts/hwf-publicapp", "log": "", "error": null, "progress": null}}
```

409 when this server is already ingesting (`An ingest is already running`) or another process holds the lock (`Another process is building an index`). The body includes `ingest`.

Do not restart the container while `ingest.running` is true.

## POST /api/remove

Delete one repository index and its checkout. Other repositories stay. The page asks before it calls this. The HTTP route does not: a successful POST deletes the index. The MCP tool adds its own `confirm` check before it calls this route.

```json
{"repo": "hmcts/hwf-publicapp"}
```

```json
{
  "removed": {
    "name": "hmcts/hwf-publicapp",
    "branch": "master",
    "commit_sha": "abc123def456",
    "ingested_at": "2026-11-21T12:00:00+00:00",
    "files": 420,
    "chunks": 3100
  },
  "repositories": []
}
```

`repositories` is the list that remains. 400 when `repo` is blank (`Choose a repository`) or that name is not indexed (`<name> is not indexed`). 409 when this server is ingesting.
