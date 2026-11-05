FROM postgres:16-bookworm

# The Postgres apt repository is already configured in the official image.
# pgvector supplies the extension used for code embeddings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
