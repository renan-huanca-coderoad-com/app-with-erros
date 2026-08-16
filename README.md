# NorthStar Supplies — MTTR Triage Agent Target App

A deliberately imperfect wholesale office-supplies backend, built as the
**target system** for the *Incident MTTR Triage Agent* hackathon project.
It looks and behaves like a normal business API, but a handful of
human-plausible bugs (business logic and database mistakes — no security
issues) are hidden in the endpoints. A simulated user exercises the API
like a real customer/back-office mix and unknowingly triggers them.

Every unhandled exception is captured as a structured JSON line with the
full stack trace — that log is the feed for the triage agent.

## Quickstart

```bash
uv sync

# terminal 1 — the app (Swagger UI at http://127.0.0.1:8000/docs)
uv run uvicorn shopflow.app:app --port 8000

# terminal 2 — the simulated user
uv run python simulator/shopper.py --ops 120 --seed 2026
```

After a run, `logs/errors.log` contains the captured incidents. A fresh
database (`shopflow.db`, recreated and seeded automatically if missing)
plus ~120 ops reliably produces examples of **all** planted failure
modes. Delete `shopflow.db` to reset the world.

## What's in the box

| Piece | Path | Notes |
|---|---|---|
| API | `src/shopflow/` | FastAPI + SQLAlchemy + SQLite. Catalog, customers, loyalty tiers, coupons, orders, refunds, sales reports. |
| Error capture | `src/shopflow/app.py` | Middleware appends each unhandled exception to `logs/errors.log` (override with `SHOPFLOW_ERROR_LOG`) and returns a 500 with an `error_id`. |
| Simulated user | `simulator/shopper.py` | Weighted-random realistic operations; `--base-url`, `--ops`, `--seed`, `--delay`. Knows nothing about the bugs. |
| Tests | `tests/test_smoke.py` | Happy paths only (`uv run python -m unittest discover -s tests`). The planted bugs are features, not regressions. |
| Answer key | `docs/ANSWER_KEY.md` | **Spoilers.** The planted bugs with root causes and fix hints — for scoring the triage agent. Never feed it to the agent. |

## Error log format

One JSON object per line:

```json
{
  "timestamp": "2026-07-31T14:59:01.123456+00:00",
  "request_id": "0198c4a1-7f3e-7a2b-9c11-4de2f0a71b93",
  "event_id": "0198c4a1-7f40-7c8d-b0a4-91ee3c5d7f22",
  "fingerprint": "3f9a1c4e7b02d85a",
  "method": "POST",
  "path": "/orders",
  "query": "",
  "body": "{\"customer_id\": 1, \"items\": [...]}",
  "exception_type": "IntegrityError",
  "exception_message": "...CHECK constraint failed: stock_non_negative...",
  "traceback": "Traceback (most recent call last): ..."
}
```

### The three identifiers

They answer three different questions, so they are kept separate:

| Field | Identifies | Notes |
|---|---|---|
| `request_id` | one HTTP request | Minted for **every** request, not just failures. Taken from the caller's `X-Request-ID` when it looks safe, otherwise generated. Returned on every response as `X-Request-ID`. |
| `event_id` | one occurrence of an error | Unique per failure; also in the 500 response body. |
| `fingerprint` | the *class* of error | Deterministic — `sha256(exception type + normalized message + deepest in-app frame)`. Repeat occurrences of one bug share it, so 300 incidents group into 7 issues. |

Both generated IDs are UUIDv7, so they sort chronologically. The
fingerprint's message normalization strips SQLAlchemy's `[SQL: ...]`,
`[parameters: ...]` and docs-link suffixes plus any digits, which is what
lets the same bug group across occurrences with different bound values.

The 500 response body carries `request_id` and `event_id` (never the
exception detail), so client-side observations can be correlated with
server-side traces.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SHOPFLOW_DB` | `shopflow.db` | SQLite database path |
| `SHOPFLOW_ERROR_LOG` | `logs/errors.log` | Incident feed location |
