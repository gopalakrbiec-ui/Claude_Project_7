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

# Railway injects $PORT at runtime; default to 8000 for local docker compose
ENV PORT=8000
PYTHONUNBUFFERED=1
EXPOSE $PORT

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port $PORT"]
