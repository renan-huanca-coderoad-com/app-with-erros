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

For the richer exercise — a corpus spanning a bad deploy — see
[The deploy-incident corpus](#the-deploy-incident-corpus) below. That is
one command and needs no terminal juggling.

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
| Logging | `src/shopflow/observability.py` | JSON formatter, correlation IDs, error fingerprinting, lifecycle records. |
| Request capture | `src/shopflow/app.py` | Middleware logs an access line for every request and an extra error line, with traceback, for unhandled exceptions. |
| Corpus generator | `tools/logcorpus/` | Builds the deploy-incident log: two commits, one database, one timeline. |
| Simulated user | `simulator/shopper.py` | Weighted-random realistic operations; `--base-url`, `--ops`, `--seed`, `--delay`. Knows nothing about the bugs. |
| Tests | `tests/test_smoke.py` | Happy paths only (`uv run python -m unittest discover -s tests`). The planted bugs are features, not regressions. |
| Answer key | `docs/ANSWER_KEY.md` | **Spoilers.** The planted bugs with root causes and fix hints — for scoring the triage agent. Never feed it to the agent. |

## The deploy-incident corpus

The default corpus asks the agent one question: *what is broken?* This one
asks the harder one real incidents start with: **what changed?**

```bash
uv run python -m tools.logcorpus generate      # writes logs/app.log
uv run python -m tools.logcorpus verify        # checks it tells the story
```

It runs the app twice — once at the previous commit, once at the current one —
against one database, appending to one log, then moves the result onto a
realistic timeline. About 1,700 lines spanning **three days of baseline and an
overnight incident**:

| | Before the deploy | After |
|---|---|---|
| build | `1.4.2`, commit `02780cc` | `1.5.0`, commit `5528d5b` |
| `GET /products/{id}` | **0 failures in ~200 requests** | fails on most requests |
| the 7 long-standing bugs | firing steadily | still firing, same rates |

A commit broke a previously-healthy endpoint. Everything needed to prove it is
in the log: the endpoint's spotless record beforehand, an exception fingerprint
that appears nowhere in the baseline, lifecycle records bracketing the version
change, and a `commit` field on every line that `git show` takes directly.

The seven pre-existing bugs keep firing at the same rate throughout, on
purpose. Listing every error in the file is easy; separating "this has always
been broken" from "this broke at 14:40" is the actual skill.

Useful flags: `--baseline-days`, `--incident-hours`, `--baseline-ops`,
`--deploy-at`, `--seed`, `--dry-run`. The same `--seed` reproduces the traffic
*shape* — same operation sequence, same bug mix — but not byte-for-byte:
identifiers use `secrets` and durations are measured wall time.

Between traffic bursts the generator grooms the simulated world — restocking
seeded SKUs, reinstating discontinued ones, retiring unordered seasonal lines,
working down the untiered-customer backlog. Each is something a real operations
team does on a schedule, and together they keep the ambient bug rates flat.
Without them the baseline drains its own stock and its own supply of untiered
customers, and the long-standing bugs *trend* — which reads as a second, larger
regression and buries the real one.

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
| `SHOPFLOW_COMMIT` | `unknown` | `commit` field; the revision this process is running |
| `SHOPFLOW_HOST` | system hostname | `host` field on every record |
