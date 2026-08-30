# WeddingApp Backend

AI-generated wedding & life-event content for India.
Async Python backend — FastAPI + Arq + PostgreSQL + Redis + MinIO.

---

## Prerequisites

- Docker + Docker Compose v2
- Python 3.12 (for running tests locally without Docker)
- `make`

---

## First-time setup

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd weddingapp-backend

# 2. Create your local .env (safe defaults for local dev are pre-filled)
cp .env.example .env
# Edit .env if you need different ports or real API keys

# 3. Install Python dev dependencies (for local test / lint runs)
pip install -e ".[dev]"

# 4. Start all services (postgres, redis, minio, api, worker)
make up

# 5. Run database migrations
make migrate

# 6. Verify the API is healthy
curl http://localhost:8000/health
# → {"status":"ok","env":"development"}

# 7. Open the interactive API docs
open http://localhost:8000/docs

# 8. MinIO web console (browse uploaded files)
open http://localhost:9001
# login: minioadmin / minioadmin
```

---

## Daily workflow

| Command | What it does |
|---|---|
| `make up` | Build images and start all services in the background |
| `make down` | Stop and remove containers (data volumes are preserved) |
| `make logs` | Tail logs for api and worker |
| `make test` | Run the pytest suite locally |
| `make lint` | Run ruff checks |
| `make format` | Auto-fix formatting and lint issues |
| `make migrate` | Apply pending Alembic migrations |
| `make makemigrations msg="..."` | Generate a new migration from model changes |
| `make shell` | Open a bash shell inside the api container |

---

## Project layout

```
app/
  api/            HTTP routers — request validation, auth, response shaping only
  services/       Business logic — orchestrates repos and adapters
  repositories/   SQLAlchemy queries — one file per aggregate root
  adapters/       External provider clients — one module per provider
  workers/        Arq async job functions
  models/         SQLAlchemy ORM models
  schemas/        Pydantic request / response schemas
  core/
    config.py     pydantic-settings — all config from env vars
    database.py   SQLAlchemy engine + session dependency

alembic/          DB migration scripts
tests/            pytest tests (mirrors app/ structure)
docker-compose.yml
Dockerfile
pyproject.toml
Makefile
```

See [CLAUDE.md](./CLAUDE.md) for the full architectural brief and non-negotiable rules.

---

## Architecture rules (summary)

1. **Money is integer paise** — never float.
2. **Payments and credit mutations are idempotent** — safe to retry.
3. **Wallet is an append-only ledger** — balance = `SUM(entries)`.
4. **HTTP never calls a model** — requests enqueue jobs; workers do the AI calls.
5. **Safety gate** — every job checks for celebrity / NSFW before spending credits.
6. **All external providers behind adapter interfaces** — swap without touching services.

---

## Running tests

```bash
# Local (no Docker required — uses in-memory / mock settings)
pip install -e ".[dev]"
make test

# With coverage report
pytest --cov=app --cov-report=term-missing
```

---

## Environment variables

See `.env.example` for the full list with descriptions.
All secrets must come from env vars — **never hardcode credentials**.
