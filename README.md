# Code RAG

A local retrieval system for GitHub repositories. Postgres stores the index. Its image is the official Postgres 16 image with the pgvector extension installed. A second container clones a repository, splits it into code chunks, and answers command-line questions from that index. Both containers are defined in Docker Compose, so the same commands work on Windows, macOS, and Linux.

The first repository to index is [hmcts/hwf-publicapp](https://github.com/hmcts/hwf-publicapp).

## Table of contents

- [What you need](#what-you-need)
- [Start](#start)
- [Web](#web)
- [Ask questions](#ask-questions)
- [Index another repository](#index-another-repository)
- [Written answers](#written-answers)
  - [Search model and answer model](#search-model-and-answer-model)
  - [Local model with Ollama](#local-model-with-ollama)
  - [Hosted model](#hosted-model)
- [How the index is built](#how-the-index-is-built)
- [Data](#data)
- [Tests](#tests)

## What you need

- Docker, with Compose v2 (`docker compose`)

The app container already contains Git and the embedding model. Nothing else has to be installed on the machine.

## Start

From this directory:

```powershell
docker compose build
docker compose run --rm -T rag ingest https://github.com/hmcts/hwf-publicapp
```

The first build downloads the embedding model (about 700 MB) and takes a few minutes. The first ingest clones the repository and embeds it on CPU. Later ingests of the same commit return immediately.

## Web

```powershell
docker compose up -d
```

Open http://localhost:8080. Switch between Query, Impact, and Search, choose an indexed repository, and run it. Each passage links to those lines in the indexed commit on GitHub. Index another GitHub repository from the same page. A first index takes several minutes and the page shows its progress.

`docker compose run` still runs one command, so the wrappers keep working while the web app is up.

On macOS or Linux the same commands work. `rag.ps1` and `rag.sh` are thin wrappers:

```powershell
.\rag.ps1 query "where is the application submitted"
```

```sh
./rag.sh query "where is the application submitted"
```

## Ask questions

```powershell
docker compose run --rm -T rag query "where is the application submitted"
docker compose run --rm -T rag query "how are fees calculated" -k 12
docker compose run --rm -T rag impact "OnlineApplication"
docker compose run --rm -T rag repos
```

`query` retrieves the code passages most relevant to a question. A question that names a directory and a value, such as `look under /app for 4250 or min_threshold_base`, searches that directory for those values and leaves the rest of the repository out. Quote the text to match it exactly: `look under /features for "with income '8000' and average"` finds that step and leaves other income files out. Inside the quotes, a regular expression is used as written. `look under /features for "with income '[0-9]*' and average"` matches any amount in that step. Use `[0-9]` for a digit. When the quoted text is not in the index, the result is empty. The page shows when this process started. A question that asks to show or list features returns feature files only. When a question names a `.feature` file and asks how that step runs, retrieval follows it into its step definition and the page-object methods that fill fields and click buttons. `impact` looks up a class, method, or file and groups the result into definitions, references, and related code. References include specs and feature tests when those files mention the name.

Search covers every indexed repository. Pass `--repo hmcts/hwf-publicapp` when more than one is loaded and you want only one.

## Index another repository

```powershell
docker compose run --rm -T rag ingest https://github.com/owner/name
docker compose run --rm -T rag ingest owner/name --branch main
docker compose run --rm -T rag ingest owner/name --force
```

`--force` rebuilds the index even when that commit is already stored. Use it after the chunking code changes.

For a private repository, set `GITHUB_TOKEN` in a `.env` file next to `docker-compose.yml`. Compose reads that file when it starts the container. `.env.example` shows the variables. Do not commit `.env`.

## Written answers

Retrieval works with no language model. The commands print the matching source.

The answer is written only from the retrieved passages, and it cites file paths and line numbers. `--no-answer` prints the passages and skips the model. `--model` selects a different answer model for one command.

### Search model and answer model

A question is not sent to the index as one sentence. A small model first turns it into a few short searches, in the same way CorpRAG rewrites a question before retrieval. Each search runs on its own, and the passages are pooled. The answer model then reads those passages and writes the reply. The repository itself is still split on declarations: a method or a scenario stays in one chunk.

Questions that ask for a list of features, or that name a `.feature` file, skip this step and use the question as written.

Each response reports the elapsed time and the language-model tokens for planning the question, embedding the searches, searching the index, and writing the answer. Embedding runs locally, so that stage has a time and no token count. The total is the sum of those stages.

With Ollama, `OLLAMA_RETRIEVAL_MODEL` plans the searches and `OLLAMA_MODEL` writes the answer. With a hosted API, `OPENAI_RETRIEVAL_MODEL` plans the searches and `OPENAI_MODEL` writes the answer. Leave the retrieval model on the small one.

### Local model with Ollama

On the machine that runs Docker, start Ollama so it is listening on port 11434. The app container calls it through `host.docker.internal`, which works on Docker Desktop and on Linux Compose.

Use `qwen2.5-coder:7b` to plan the searches and `qwen2.5-coder:32b-instruct-q5_K_M` to write the answer. The 32b model is an instruct coder, so it stays with the retrieved Ruby and cites the passages. `gemma4:latest` is the small E4B model: quick on a laptop, and a lighter reader of a Rails application than either Qwen coder. `deepseek-r1:32b` spends the request on a reasoning trace; the retrieval has already found the passages, so that wait rarely improves the citation.

In `.env`:

```
LLM_PROVIDER=ollama
OLLAMA_RETRIEVAL_MODEL=qwen2.5-coder:7b
OLLAMA_MODEL=qwen2.5-coder:32b-instruct-q5_K_M
OLLAMA_NUM_CTX=8192
```

`OLLAMA_NUM_CTX` is the context the request actually uses. Ollama's own default is a few thousand tokens, which cuts off the retrieved code. `8192` fits a 32B model on a 32 GB Mac. Raise it to `16384` when the machine still has memory after the model loads. The first reply after a cold start can take several minutes; `OLLAMA_TIMEOUT` defaults to 600 seconds.

```powershell
docker compose run --rm -T rag query "where is the application submitted"
docker compose run --rm -T rag impact "OnlineApplication" --model qwen2.5-coder:7b
```

### Hosted model

```
LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_RETRIEVAL_MODEL=gpt-4o-mini
OPENAI_MODEL=gpt-4o
```

`OPENAI_BASE_URL` can point at any OpenAI-compatible chat API.

## How the index is built

Each source file is split on declarations (`class`, `module`, `def`, Java methods, RSpec groups, Cucumber scenarios). A chunk keeps the qualified name, for example `Application#submit` or `FeeService.calculate`. Files without declarations are split into overlapping windows.

Those chunks are embedded locally with `BAAI/bge-small-en-v1.5` (384 dimensions, so no model API is required) and stored in Postgres with [pgvector](https://github.com/pgvector/pgvector). This model is small enough to index a repository on a laptop-sized Docker virtual machine. Keyword search uses the Postgres `simple` dictionary, which does not stem tokens, after CamelCase and snake_case have been split. A question uses both searches together.

`impact` is identifier search plus that semantic search. A qualified name such as `ApplicationController#online_application` matches that method, not every file whose name is only the method's last word. It is not a compiler call graph: a name hidden behind metaprogramming, or a declaration the splitter misses, can be absent. The matching source is what the command shows.

To use the larger code model instead, set `EMBEDDING_MODEL=jinaai/jina-embeddings-v2-base-code` and `EMBEDDING_DIM=768`, then remove the database volume and ingest again. That model needs several extra gigabytes of memory during indexing.

Vendor directories, lockfiles, `node_modules`, and secret-shaped files (`.env`, private keys, `credentials.yml.enc`) are not indexed. `.env.example` files are.

## Data

Postgres listens on `localhost:5433` (user `rag`, password `rag`, database `rag`). That account is for this local index.

`docker compose down` stops the containers and keeps the index. `docker compose down -v` deletes it. Checkouts live in the `repos` volume, separate from the Postgres volume.

The embedding model is fixed when the database is first created. Changing `EMBEDDING_MODEL` means removing the volume and ingesting again.

## Tests

The unit tests cover chunking and file selection. They do not need Docker:

```powershell
python -m unittest discover -s tests -v
```

Run that from this directory.
