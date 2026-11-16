import os
from pathlib import Path

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://rag:rag@localhost:5433/rag"
)
REPOS_DIR = Path(os.environ.get("REPOS_DIR", "/data/repos"))
# The text model is the one a query uses unless the caller asks for code or both.
# EMBEDDING_MODEL remains the environment name so an existing deployment still
# points at the text model.
TEXT_EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
TEXT_EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
CODE_EMBEDDING_MODEL = os.environ.get(
    "CODE_EMBEDDING_MODEL", "jinaai/jina-embeddings-v2-base-code"
)
CODE_EMBEDDING_DIM = int(os.environ.get("CODE_EMBEDDING_DIM", "768"))
EMBEDDING_MODEL = TEXT_EMBEDDING_MODEL
EMBEDDING_DIM = TEXT_EMBEDDING_DIM
FASTEMBED_CACHE_PATH = os.environ.get("FASTEMBED_CACHE_PATH", "/opt/fastembed")

INDEX_CHOICES = ("text", "code", "both")
FUSION_CHOICES = ("rrf", "score")
# text keeps the previous single-index search. score keeps the previous sum of
# cosine similarity and ts_rank_cd, so a query that sets neither flag ranks as
# it did before the second index existed. rrf is there when the two lists
# should have an equal vote instead.
DEFAULT_INDEX = os.environ.get("RAG_INDEX", "text").strip().lower() or "text"
DEFAULT_FUSION = os.environ.get("RAG_FUSION", "score").strip().lower() or "score"
RRF_K = int(os.environ.get("RRF_K", "60"))
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().lower() or "openai"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.environ.get(
    "OPENAI_BASE_URL", "https://api.openai.com/v1"
).strip().rstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_RETRIEVAL_MODEL = os.environ.get("OPENAI_RETRIEVAL_MODEL", "gpt-4o-mini").strip()
OLLAMA_BASE_URL = os.environ.get(
    "OLLAMA_BASE_URL", "http://host.docker.internal:11434"
).strip().rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b").strip()
OLLAMA_RETRIEVAL_MODEL = os.environ.get(
    "OLLAMA_RETRIEVAL_MODEL", "qwen2.5-coder:7b"
).strip()
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

def resolve_index(value: str | None) -> str:
    """Return text, code, or both. An empty value uses the configured default."""
    chosen = _choice(value, DEFAULT_INDEX)
    if chosen not in INDEX_CHOICES:
        raise ValueError("Index must be text, code, or both")
    return chosen


def resolve_fusion(value: str | None) -> str:
    """Return rrf or score. An empty value uses the configured default."""
    chosen = _choice(value, DEFAULT_FUSION)
    if chosen not in FUSION_CHOICES:
        raise ValueError("Fusion must be rrf or score")
    return chosen


def _choice(value: str | None, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        return value  # type: ignore[return-value]
    stripped = value.strip().lower()
    return stripped or default


def resolve_rrf_k(value: int | None = None) -> int:
    """Return the reciprocal-rank constant. The usual value is 60."""
    chosen = RRF_K if value is None else value
    if isinstance(chosen, bool) or not isinstance(chosen, int) or chosen < 0:
        raise ValueError("RRF k must be zero or greater")
    return chosen


MAX_LINES = 80
LINE_OVERLAP = 15
MAX_CHUNK_CHARS = 5000
MAX_FILE_BYTES = 400_000
EMBED_TEXT_CHARS = 8000
