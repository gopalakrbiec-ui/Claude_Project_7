FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

# Arq workers log to stdout; no PORT needed (not an HTTP server)
ENV PYTHONUNBUFFERED=1

CMD ["arq", "app.workers.main.WorkerSettings"]
