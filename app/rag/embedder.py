"""Local embedding models. Each model is loaded on first use so `repos` stays quick."""

from __future__ import annotations

from rag.config import (
    CODE_EMBED_BATCH,
    CODE_EMBED_TOKENS,
    CODE_EMBEDDING_DIM,
    CODE_EMBEDDING_MODEL,
    FASTEMBED_CACHE_PATH,
    TEXT_EMBED_BATCH,
    TEXT_EMBEDDING_DIM,
    TEXT_EMBEDDING_MODEL,
)


class Embedder:
    """One FastEmbed model, shared across calls that name the same model."""

    _shared: dict[str, object] = {}

    def __init__(self, model_name: str | None = None, dim: int | None = None) -> None:
        if model_name is None and dim is None:
            model_name = TEXT_EMBEDDING_MODEL
            dim = TEXT_EMBEDDING_DIM
        elif model_name is None or dim is None:
            raise ValueError("Pass the embedding model and its dimension together")
        self.model_name = model_name
        self.dim = dim
        self.batch_size = CODE_EMBED_BATCH if model_name == CODE_EMBEDDING_MODEL else TEXT_EMBED_BATCH
        self._model = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._embed(texts, query=False)
        self._check_dim(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vector = self._embed([text], query=True)[0]
        self._check_dim(vector)
        return vector

    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        model = self._load()
        if query:
            produced = model.query_embed(texts)
        else:
            # Stay in-process. A worker pool deadlocks under Docker, and the
            # ONNX runtime already uses its own threads.
            produced = model.embed(texts, batch_size=self.batch_size)
        return [vector.tolist() for vector in produced]

    def _load(self):
        cached = Embedder._shared.get(self.model_name)
        if cached is None:
            from fastembed import TextEmbedding

            print(f"Loading embedding model {self.model_name}", flush=True)
            cached = TextEmbedding(
                model_name=self.model_name,
                cache_dir=FASTEMBED_CACHE_PATH,
                threads=4,
                enable_cpu_mem_arena=False,
            )
            if self.model_name == CODE_EMBEDDING_MODEL:
                cached.model.tokenizer.enable_truncation(max_length=CODE_EMBED_TOKENS)
            Embedder._shared[self.model_name] = cached
        return cached

    def _check_dim(self, vector: list[float]) -> None:
        if len(vector) != self.dim:
            raise SystemExit(
                f"Embedding model {self.model_name} produced {len(vector)} dimensions, "
                f"but this database expects {self.dim}. "
                "Keep the model and its dimension together. "
                "A change of model means removing the volume and ingesting again "
                "(docker compose down -v)."
            )


def text_embedder() -> Embedder:
    return Embedder(TEXT_EMBEDDING_MODEL, TEXT_EMBEDDING_DIM)


def code_embedder() -> Embedder:
    return Embedder(CODE_EMBEDDING_MODEL, CODE_EMBEDDING_DIM)
