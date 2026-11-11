# Hospital Bulk Processing API

A FastAPI service that takes a CSV of hospitals, creates each one in the
[Hospital Directory API](https://hospital-directory.onrender.com/docs) under a
single batch, and activates the batch once every hospital exists. It ships with
a small web UI for uploading files and watching batches progress.

- **Live app:** _add your Render URL here_
- **Interactive API docs:** `/docs` on the running service

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
# open http://localhost:8000 (UI) or http://localhost:8000/docs (API)
```

With Docker:

```bash
docker compose up --build
```

Try it from the command line:

```bash
curl -F file=@samples/hospitals_20.csv http://localhost:8000/hospitals/bulk
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/hospitals/bulk` | Upload a CSV and process it. Waits for completion (`200`) by default; `?wait=false` returns `202` immediately with a `Location` header to poll. |
| `POST` | `/hospitals/bulk/validate` | Validate a CSV and preview its rows without creating anything. |
| `GET` | `/hospitals/bulk` | List jobs, newest first. |
| `GET` | `/hospitals/bulk/{batch_id}` | Progress and per-row results for one job (the polling endpoint). |
| `POST` | `/hospitals/bulk/{batch_id}/resume` | Retry the failed rows, or a failed activation, in the same batch. |
| `DELETE` | `/hospitals/bulk/{batch_id}` | Roll back: delete the batch's hospitals from the directory. |
| `GET` | `/health` | Liveness. |
| `GET` | `/health/upstream` | Whether the Hospital Directory is reachable, and its latency. |

CSV format: header `name,address,phone` (phone optional, header case and
spacing ignored), up to 20 rows, UTF-8 (a byte-order mark is fine).

Response (superset of the format in the brief):

```json
{
  "batch_id": "0d86a402-3227-41ac-9d7f-7edefe94dac7",
  "status": "completed",
  "total_hospitals": 20,
  "processed_hospitals": 20,
  "failed_hospitals": 0,
  "pending_hospitals": 0,
  "processing_time_seconds": 5.602,
  "batch_activated": true,
  "resumable": false,
  "error": null,
  "warnings": [],
  "created_at": "2026-10-09T15:56:11.037491Z",
  "hospitals": [
    {"row": 1, "hospital_id": 101, "name": "General Hospital",
     "status": "created_and_activated", "attempts": 1, "error": null}
  ]
}
```

Job statuses: `pending → processing → activating → completed`, or
`partially_failed` / `activation_failed` (both resumable), or `rolled_back`.
Row statuses: `pending`, `created`, `created_and_activated`, `failed`,
`rolled_back`.

## Design

```
            ┌──────────────── Bulk Processing API ────────────────┐
 CSV ──────▶│ routes ─▶ csv_parser (validate whole file first)    │
            │    │                                                 │
            │    ▼                                                 │
            │ BulkProcessor ──▶ JobStore (in-memory, bounded)      │
            │    │                                                 │
            │    ▼                                                 │
            │ DirectoryClient (global request limit, retries) ─────┼──▶ Hospital Directory API
            └──────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `app/csv_parser.py` | Decode, parse and validate the upload; report every problem at once. |
| `app/directory_client.py` | Talk to the Hospital Directory: error mapping, retries for idempotent calls. |
| `app/processor.py` | Run a job: concurrent creates, duplicate-safe retries, activation, resume, rollback. |
| `app/jobs.py` | Job and row state, progress counters, bounded in-memory store. |
| `app/routes.py`, `app/schemas.py` | HTTP layer and the public response contract. |
| `app/static/index.html` | Web UI (no build step; uses the same public API). |

### Decisions worth calling out

**Validate everything before creating anything.** The whole file is checked
before the first upstream call, and all errors come back in one response. A
malformed file can never leave a half-created batch behind.

**Concurrent creates, bounded globally.** I measured the upstream before
designing: a single `POST /hospitals/` takes about 5 to 6 s, but ten in parallel
also finish in about 6 s, so the delay does not add up. All rows are created
concurrently through one pooled `httpx.AsyncClient`. The limit on in-flight
requests (`MAX_CONCURRENCY`) is enforced inside `DirectoryClient`, the single
path every upstream call takes, so it covers creates, duplicate checks,
activation and rollback for *all* jobs at once. Retries wait out their backoff
outside the limit, so a waiting retry never holds a slot.

**Retries that cannot create duplicates.** `GET`, `PATCH` and `DELETE` are
idempotent and are retried with capped exponential backoff and jitter on
timeouts, connection errors, 5xx and 429. `POST` is not idempotent: a timeout
does not say whether the hospital was stored. Before retrying a failed create,
the processor lists the batch upstream and adopts a matching hospital that no
other row has claimed. Resume does the same reconciliation before re-creating
anything. 4xx responses are never retried.

**Activate only when the batch is complete.** If any row fails, the batch is
left inactive, so nothing half-imported is visible in the directory. The job
becomes `partially_failed` and can be resumed (same batch ID, only the failed
rows are retried) or rolled back via the upstream batch delete.

**Sync by default, async when asked.** `POST /hospitals/bulk` returns the full
result, as the brief specifies. `?wait=false` returns `202` immediately for
clients that want to poll for progress (the UI does this). A synchronous client
that disconnects does not cancel its job; it keeps running and stays
pollable.

**Cold starts.** The upstream runs on Render's free tier and can take 20–50 s
to wake up. The read timeout is 60 s, and the service pings the upstream on
startup to wake it early. `/health/upstream` shows its current state.

### Performance

Measured against the live Hospital Directory API with `samples/hospitals_20.csv`:

| Scenario | Time for 20 hospitals |
|---|---|
| Sequential creates (estimated, about 5.8 s each) | about 115 s |
| This service, upstream warm | 5.6 s, 7.6 s (two runs) |
| This service, upstream cold | 27 s (mostly the upstream waking up) |

### Scaling

Job state is kept in process memory, as the brief allows. That means one
worker process (the Dockerfile runs a single uvicorn worker), and jobs do not
survive a restart. To scale out, move `JobStore` to Redis or Postgres and run
processing in a task queue; `BulkProcessor` only depends on the store's small
interface, so this is a contained change. The store keeps at most `MAX_JOBS`
jobs and evicts the oldest finished ones first.

### Known limitations

- If a CSV contains two identical rows *and* a create attempt for one of them
  times out after the upstream stored it, the adoption step cannot tell the two
  rows apart. The parser warns about duplicate rows for this reason.
- Jobs are lost on restart (in-memory store, see Scaling). Hospitals already
  created upstream are not; their batch can still be activated or deleted
  directly through the Hospital Directory API.

## Configuration

All settings are environment variables (or a `.env` file).

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://hospital-directory.onrender.com` | Hospital Directory API |
| `MAX_CSV_ROWS` | `20` | Maximum hospitals per file |
| `MAX_UPLOAD_BYTES` | `1048576` | Maximum upload size |
| `MAX_CONCURRENCY` | `20` | Maximum in-flight upstream requests across all jobs |
| `MAX_ATTEMPTS` | `3` | Attempts per upstream call |
| `RETRY_BASE_DELAY` / `RETRY_MAX_DELAY` | `0.5` / `8.0` | Backoff bounds in seconds |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | `10` / `60` | Upstream timeouts in seconds |
| `MAX_JOBS` | `500` | Jobs kept in memory |
| `WARM_UPSTREAM_ON_STARTUP` | `true` | Ping the upstream at startup |
| `LOG_LEVEL` | `INFO` | Logging level |

## Tests

```bash
.venv/bin/pytest                                   # unit, API, scenario and browser tests
.venv/bin/pytest --cov=app --cov-branch            # with coverage (99% of app/)
RUN_LIVE_TESTS=1 .venv/bin/pytest -m live          # against the real Hospital Directory
```

| Layer | Files | What it covers |
|---|---|---|
| Unit | `test_csv_parser.py`, `test_directory_client.py`, `test_jobs.py` | CSV edge cases (encoding, BOM, quoting, limits), error mapping, retry and backoff, job state and timing, store eviction. |
| Processor | `test_processor.py` | Concurrency and the global request limit across jobs, duplicate-safe retries, partial failure, activation failure, resume, rollback, shutdown, crash safety. |
| API | `test_api.py` | Every endpoint and status code (200, 202, 404, 409, 413, 422, 502), sync and async modes, OpenAPI docs, static assets. |
| Scenarios | `test_scenarios.py` | Every file in `samples/scenarios/` produces its documented outcome. |
| Browser (e2e) | `tests/e2e/` | Drives the web UI in Chrome via Playwright: review, create, retry, rollback, deep links, icons and fonts loading, dark mode, no horizontal scroll at 375 and 768 px, no console errors. Skips if Chrome is missing. |
| Live (opt-in) | `test_live_directory.py` | The real upstream's contract, plus a 5-row upload end to end. Cleans up after itself. |

Everything except the live tests runs against `tests/fake_directory.py`, a
stateful fake Hospital Directory plugged into httpx's transport layer, so the
real client code runs end to end. Failures are scripted per hospital: HTTP 500,
422, timeouts, and a timeout that happens *after* the upstream stored the
hospital. CI (`.github/workflows/ci.yml`) runs the suite on Python 3.9 and 3.12
and checks that the Docker image builds and passes its health check.

## Demo mode and sample files

`samples/scenarios/` holds one CSV per scenario: files `01` to `12` are valid
(two with warnings), `13` to `23` are rejected by validation, and `D1` to `D6`
show failure handling. The real Directory accepts every row that passes
validation, so failures can only be shown against a simulation:

```bash
.venv/bin/python scripts/demo_server.py   # http://localhost:8001
```

In demo mode a tag at the end of a hospital name decides what happens:
`[server-error]` (fails until you retry), `[rejected]` (HTTP 422 once),
`[flaky]` (fails once, recovers automatically), `[slow-save]` (stored but the
response times out; no duplicate is created) and `[activation-fails]` (the
batch can't be activated until you retry).

## Deploying to Render

The repo includes a `render.yaml` blueprint. In Render choose
**New → Blueprint**, select this repository, and apply. The service builds from
the Dockerfile and uses `/health` as its health check.
