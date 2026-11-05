"""Local embedding model. Loaded on first use so `repos` stays quick."""

from __future__ import annotations

from rag.config import EMBEDDING_DIM, EMBEDDING_MODEL, FASTEMBED_CACHE_PATH


class Embedder:
    def __init__(self) -> None:
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
            produced = model.embed(texts, batch_size=32)
        return [vector.tolist() for vector in produced]

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            print(f"Loading embedding model {EMBEDDING_MODEL}", flush=True)
            self._model = TextEmbedding(
                model_name=EMBEDDING_MODEL,
                cache_dir=FASTEMBED_CACHE_PATH,
                threads=4,
                enable_cpu_mem_arena=False,
            )
        return self._model

    @staticmethod
    def _check_dim(vector: list[float]) -> None:
        if len(vector) != EMBEDDING_DIM:
            raise SystemExit(
                f"Embedding model {EMBEDDING_MODEL} produced {len(vector)} dimensions, "
                f"but this database expects {EMBEDDING_DIM}. "
                "Keep EMBEDDING_MODEL and EMBEDDING_DIM together, and re-ingest into a fresh volume "
                "if you change them (docker compose down -v)."
            )
