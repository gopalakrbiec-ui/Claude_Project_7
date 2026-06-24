FROM python:3.12-slim

WORKDIR /app

# System deps for asyncpg (libpq) and cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
# Install runtime deps only (no [dev] extras in the image)
RUN pip install --no-cache-dir -e .

COPY . .

EXPOSE 8000
