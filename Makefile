.PHONY: up down logs test lint migrate makemigrations shell

# ── Docker ──────────────────────────────────────────────────────────────────

up:
	@cp -n .env.example .env 2>/dev/null || true
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

# ── Quality ─────────────────────────────────────────────────────────────────

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

test:
	pytest

# ── Database ─────────────────────────────────────────────────────────────────

# Run pending migrations against the running postgres container
migrate:
	docker compose exec api alembic upgrade head

# Generate a new migration: make makemigrations msg="add users table"
makemigrations:
	docker compose exec api alembic revision --autogenerate -m "$(msg)"

# ── Misc ─────────────────────────────────────────────────────────────────────

shell:
	docker compose exec api bash
