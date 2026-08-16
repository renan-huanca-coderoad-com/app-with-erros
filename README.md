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

After a run, `logs/app.log` holds the traffic — successes and failures
together, roughly 190 lines for 120 ops. A fresh database (`shopflow.db`,
recreated and seeded automatically if missing) plus ~120 ops reliably
produces examples of **all** planted failure modes. Delete `shopflow.db`
to reset the world.

Finding the incidents in that stream is the triage agent's job, so the
log is not pre-filtered for it. To look yourself:

```bash
jq -c 'select(.level == "ERROR" and .logger == "shopflow.error")' logs/app.log
jq -r '.fingerprint' logs/app.log | sort | uniq -c | sort -rn   # group by bug
```

## What's in the box

| Piece | Path | Notes |
|---|---|---|
| API | `src/shopflow/` | FastAPI + SQLAlchemy + SQLite. Catalog, customers, loyalty tiers, coupons, orders, refunds, sales reports. |
| Logging | `src/shopflow/observability.py` | JSON formatter, correlation IDs, error fingerprinting. Attached to the root logger, so library output joins the same stream. |
| Request capture | `src/shopflow/app.py` | Middleware logs an access line for every request and an extra error line, with traceback, for unhandled exceptions. |
| Simulated user | `simulator/shopper.py` | Weighted-random realistic operations; `--base-url`, `--ops`, `--seed`, `--delay`. Knows nothing about the bugs. |
| Tests | `tests/test_smoke.py` | Happy paths only (`uv run python -m unittest discover -s tests`). The planted bugs are features, not regressions. |
| Answer key | `docs/ANSWER_KEY.md` | **Spoilers.** The planted bugs with root causes and fix hints — for scoring the triage agent. Never feed it to the agent. |

## Log format

One JSON object per line, one stream for everything. Every record
carries `timestamp`, `level`, `logger`, `message`, `request_id` and the
deployment context (`service`, `env`, `version`, `host`, `pid`).

**Every request produces an access line** on `shopflow.access`, at `INFO`
for 2xx/3xx, `WARNING` for 4xx and `ERROR` for 5xx:

```json
{
  "timestamp": "2026-07-31T14:59:01.129864+00:00",
  "level": "INFO", "logger": "shopflow.access",
  "message": "POST /orders 201",
  "service": "shopflow", "env": "prod", "version": "0.1.0",
  "host": "web-7d9c-x4k2", "pid": 17,
  "request_id": "0198c4a1-7f3e-7a2b-9c11-4de2f0a71b93",
  "http.method": "POST", "http.path": "/orders", "http.query": "",
  "http.status_code": 201, "http.duration_ms": 3.86,
  "http.client_ip": "10.4.2.19", "http.user_agent": "python-httpx/0.27"
}
```

**A failure adds a second line** on `shopflow.error`, with the same
`request_id`, carrying the traceback and the request body:

```json
{
  "timestamp": "2026-07-31T14:59:01.127810+00:00",
  "level": "ERROR", "logger": "shopflow.error",
  "message": "unhandled exception in POST /orders",
  "request_id": "0198c4a1-7f3e-7a2b-9c11-4de2f0a71b93",
  "event_id": "0198c4a1-7f40-7c8d-b0a4-91ee3c5d7f22",
  "fingerprint": "3f9a1c4e7b02d85a",
  "http.method": "POST", "http.path": "/orders",
  "http.body": "{\"customer_id\": 1, \"items\": [...]}",
  "exception_type": "IntegrityError",
  "exception_message": "...CHECK constraint failed: stock_non_negative...",
  "traceback": "Traceback (most recent call last): ..."
}
```

The request body is kept only on the error line — an access log carrying
every body would be both enormous and a privacy problem. It is currently
stored **verbatim, unredacted**, which a real service handling customer
data would not do.

Because the handler is attached to the root logger, anything a library
logs lands in the same stream. Chatty clients (`httpx`, `urllib3`, …) are
dialed to `WARNING` the way a real logging config does.

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
| `SHOPFLOW_LOG` | `logs/app.log` | Log stream location |
| `SHOPFLOW_SERVICE` | `shopflow` | `service` field on every record |
| `SHOPFLOW_ENV` | `prod` | `env` field on every record |
| `SHOPFLOW_VERSION` | installed package version | `version` field; real deploys inject a tag or git SHA |
| `SHOPFLOW_HOST` | system hostname | `host` field on every record |
