FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_TERMINAL_PROMPT=0 \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    HF_HOME=/opt/hf \
    EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 \
    EMBEDDING_DIM=384 \
    REPOS_DIR=/data/repos

# Bake the embedding model into the image so a machine can query after the
# image has been built, without downloading it again on first ingest.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')"

COPY rag ./rag

ENTRYPOINT ["python", "-m", "rag"]
CMD ["--help"]
