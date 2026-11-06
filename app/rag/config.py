import os
from pathlib import Path

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://rag:rag@localhost:5433/rag"
)
REPOS_DIR = Path(os.environ.get("REPOS_DIR", "/data/repos"))
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
FASTEMBED_CACHE_PATH = os.environ.get("FASTEMBED_CACHE_PATH", "/opt/fastembed")
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

MAX_LINES = 80
LINE_OVERLAP = 15
MAX_CHUNK_CHARS = 5000
MAX_FILE_BYTES = 400_000
EMBED_TEXT_CHARS = 8000
