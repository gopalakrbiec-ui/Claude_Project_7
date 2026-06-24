# CLAUDE.md — Project Brief

## PROJECT

Backend for a mobile app that creates AI-edited wedding & life-event content
(posters, invite videos, photo edits) for rural India. Cheap Android clients,
patchy data, pay-per-creation via UPI, sold both direct-to-consumer and through
commissioned local agents. The client is thin; the backend does all heavy work
asynchronously.

## TECH STACK

Use exactly this unless you flag a strong reason with a trade-off explanation.

| Layer | Choice |
|---|---|
| Language / framework | Python 3.12, FastAPI, Pydantic v2 |
| ORM / migrations | SQLAlchemy 2.0 (typed) + Alembic |
| Transactional DB | PostgreSQL |
| Cache + task queue | Redis |
| Async workers | Arq (preferred) |
| Object storage | S3-compatible client — Cloudflare R2 (prod), MinIO (local) |
| Config / secrets | pydantic-settings; all secrets from env vars, never hardcoded |
| Testing | pytest |
| Lint | ruff |
| Local dev | docker-compose |

## NON-NEGOTIABLE RULES

1. **Money in paise.** Credits and amounts are stored as integer paise. Never use
   float for money anywhere in the stack.

2. **Idempotent payments and credit mutations.** Payment confirmation and any ledger
   write must be safe to retry — use idempotency keys enforced at the DB layer.

3. **Append-only ledger.** The wallet is a ledger of credit entries. The spendable
   balance is always computed as `SUM(entries)`. Never store or overwrite a mutable
   balance column as the source of truth.

4. **HTTP never calls a model.** Generation is expensive and async. An API request
   enqueues a job and returns immediately. Workers pull from the queue and do the
   actual model calls.

5. **Safety gate before spend.** Every generation job runs a moderation check
   *before* charging credits or calling any paid provider. The gate blocks:
   - Depictions of real public figures / celebrities
   - NSFW content

   A blocked job is rejected and the reserved credits are refunded.

6. **Adapter interfaces for all external providers.** Image gen, video gen, payment
   gateways, and Claude are accessed only through adapter interfaces defined in the
   `adapters/` layer. No provider SDK calls appear in business logic or services.

## ARCHITECTURE LAYERS

```
api/          Routers only — request validation, auth, response shaping
  └─ services/    Business logic — orchestrates repos and adapters
       └─ repositories/   DB queries — SQLAlchemy models, typed queries
            └─ adapters/  External APIs — one module per provider, behind an interface
workers/      Arq async pipeline — pulls jobs, calls services, never touches HTTP
```

Each layer depends only on the layer below it. Services never import routers;
workers never import routers; adapters never import services.

## CODING STYLE

- Fully typed — use `from __future__ import annotations` and type every function.
- Small, single-purpose functions.
- Dependency injection throughout (FastAPI `Depends`, constructor injection in
  services and workers).
- No global mutable state.
- Write a pytest test alongside every service function.
- Explain trade-offs in comments only where non-obvious — avoid noise comments.
