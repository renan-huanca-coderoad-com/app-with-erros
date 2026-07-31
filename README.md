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
  "error_id": "540c1bc90111",
  "timestamp": "2026-07-31T14:59:01.123456+00:00",
  "method": "POST",
  "path": "/orders",
  "query": "",
  "body": "{\"customer_id\": 1, \"items\": [...]}",
  "exception_type": "IntegrityError",
  "exception_message": "...CHECK constraint failed: stock_non_negative...",
  "traceback": "Traceback (most recent call last): ..."
}
```

The `error_id` is also returned in the 500 response body, so client-side
observations can be correlated with server-side traces.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SHOPFLOW_DB` | `shopflow.db` | SQLite database path |
| `SHOPFLOW_ERROR_LOG` | `logs/errors.log` | Incident feed location |
