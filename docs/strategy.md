# IntegrationKit: Strategy

Status: approved 2026-10-07. Nothing here has been built or run yet.

## 1. Scope and non-goals

### In scope

- An HTTP endpoint that receives a `checkout.session.completed`-style event, verifies the
  `Stripe-Signature` header using Stripe's documented manual scheme, stores the raw event
  once, and returns `2xx` quickly.
- A background worker that syncs one row per event to a spreadsheet through a `SheetSink`
  interface, with retries (exponential backoff with jitter), a failure log, a dead-letter
  state and a replay command.
- An event simulator CLI (`tools/`) that produces clearly fake, Stripe-style signed events
  plus deliberately broken variants.
- A cross-check of the simulator and verifier against the official `stripe` Python
  library's offline verification (test-only, pending approval).
- A chaos scenario runner, an n8n rebuild of the same flow, and a written comparison.

### Non-goals

- No Stripe account, API keys, Stripe CLI login, or network calls to Stripe. Ever.
- No claim of having received real Stripe traffic. All events are simulated.
- No calls back to the Stripe API to fetch objects (a real integration often would).
- Thin events (`v2` event notifications). Stripe's docs now recommend thin events for new
  integrations; this project uses the snapshot `checkout.session.completed` shape because
  it carries the data needed for a spreadsheet row without an API call.
- `checkout.session.async_payment_succeeded` / `_failed`. A completed session can still
  have `payment_status: "unpaid"` when a delayed payment method is used; this project
  records the status as received and does nothing further. A real system would listen for
  those follow-up events and update the order (and its sheet row) when the money actually
  arrives or the payment fails.
- Exactly-once delivery. The design is at-least-once with idempotent sinks.
- Production deployment, HTTPS termination, IP allowlisting, horizontal scaling, load tests
  beyond a local smoke level.
- Multi-tenant or multi-endpoint support. One endpoint, one secret (plus rolled secrets).
- A real Google Sheets integration in M1/M2. It is an optional extra after M2.

## 2. Architecture

```
 tools/simulate.py                      FastAPI app (api)
 (fake signed event)                    ─────────────────────────────────────────────
        │  POST /webhooks/stripe         1. read raw body bytes (never re-serialise)
        │  Stripe-Signature: t=..,v1=..  2. verify signature + timestamp tolerance
        └──────────────────────────────▶    └─ fail → 400, nothing stored
                                         3. parse JSON, extract event id/type
                                         4. ONE transaction:
                                              INSERT webhook_events (raw body)
                                                ON CONFLICT (event_id) DO NOTHING
                                              INSERT sync_jobs (status=pending)
                                         5. return 200 (also 200 for a duplicate)
                                                     │
                                         PostgreSQL  ▼
                              ┌──────────────────────────────────────┐
                              │ webhook_events │ sync_jobs │ job_attempts │
                              └──────────────────────────────────────┘
                                                     │ poll: SELECT ... FOR UPDATE SKIP LOCKED
                                                     ▼
                                          worker process (same codebase)
                                          ├─ build row from stored raw event
                                          ├─ SheetSink.upsert(row)  (CsvSink by default)
                                          ├─ success → job succeeded
                                          ├─ retryable error → log attempt,
                                          │     next_run_at = now + backoff with jitter
                                          └─ permanent error or attempts exhausted
                                                → status = dead (dead-letter)
                                                     │
                                          replay CLI ◀── operator
                                          (re-queue dead jobs by job id / event id / all)
```

Key decisions:

- **Store before acknowledging.** The `2xx` is returned only after the event and its job
  are committed in one transaction, so an acknowledged event is never lost. Nothing slow
  (sink I/O) happens in the request.
- **Transactional outbox.** The job row is written in the same transaction as the event
  row, so there is no window where an event exists without a job, or the reverse.
- **Idempotency on `event.id`.** Stripe's docs say endpoints may receive the same event
  more than once and recommend tracking event IDs, not `created`. A unique constraint on
  `event_id` makes duplicates a no-op; the endpoint still returns `200` so the sender
  stops retrying.
- **Idempotent sink writes.** Because delivery to the sink is at-least-once (a worker can
  crash after writing but before marking the job done), each sink upserts keyed by
  `event_id`, so a replay or retry does not create a duplicate row.

## 3. Stack proposal

Each dependency below needs explicit approval before it is added (CLAUDE.md rule).

| Choice | Reason |
| --- | --- |
| Python 3.13 | Installed locally and well supported by every library below; the Docker image uses the same version. |
| FastAPI + Uvicorn | Gives direct access to the raw request body (`await request.body()`), which signature verification requires. Small, typed, fast to test with its `TestClient`. |
| SQLAlchemy 2 + Alembic | Typed ORM/Core with explicit transactions; Alembic gives versioned, reviewable migrations. |
| psycopg 3 | PostgreSQL driver for SQLAlchemy. |
| PostgreSQL 16 in Docker Compose | Real unique constraints, `jsonb`, and `FOR UPDATE SKIP LOCKED` for a safe job queue. Compose keeps it to one command and no account. |
| pydantic-settings | Loads and validates settings from `.env` (e.g. refuses a tolerance of 0). Comes with FastAPI's pydantic. |
| pytest (+ httpx for `TestClient`) | Standard test runner. |
| stripe (test-only) | Official SDK, used only in tests for offline cross-checking. See section 7. |
| ruff (dev) | Lint and format. Optional. |
| Simulator | Standard library only (`argparse`, `hmac`, `hashlib`, `urllib.request`), so the signing code shares nothing with the SDK and the cross-check is meaningful. |

### Retry mechanism (proposal)

A **PostgreSQL-backed job table polled by a worker process**, not Celery/RQ/Redis.

- Claim: `SELECT ... WHERE status IN ('pending','retrying') AND next_run_at <= now()
  ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT n`, then set `status='running'`,
  `locked_until = now() + lease`. Several workers can run without double-processing.
- Claiming in one line: the worker opens a transaction, selects due jobs with
  `FOR UPDATE SKIP LOCKED` (rows locked by another worker are skipped, not waited on),
  marks them `running` with `locked_until = now() + 120 s` and `locked_by = <worker id>`,
  and commits; the sink call then runs outside that transaction with its own 30 s
  timeout, so the lease (120 s) always outlasts a single attempt.
- Crash recovery: a `running` job whose `locked_until` has passed is claimable again.
  The claim query therefore also matches `status = 'running' AND locked_until < now()`.
- Backoff: **exponential with full jitter**,
  `delay = random.uniform(0, min(cap, base * 2 ** (attempt - 1)))`,
  defaults `base = 2 s`, `cap = 300 s`, `max_attempts = 8`. Random source and clock are
  injected so tests are deterministic.
- Error classes: `SinkRetryableError` (timeouts, rate limits, 5xx) retries;
  `SinkPermanentError` (bad data, auth) goes straight to `dead`. Unknown exceptions are
  treated as retryable until attempts run out.
- Every attempt, success or failure, is written to `job_attempts` (the failure log).

Why: no extra infrastructure, the queue lives in the same transaction as the event
(outbox), and every piece of the retry logic is visible code that the project is meant to
demonstrate. Trade-off: polling adds latency (about 1 s) and does not scale like a broker;
both are acceptable here and will be stated in the README.

## 4. Database schema

```sql
-- One row per unique Stripe event id. Raw body kept byte-for-byte.
CREATE TABLE webhook_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        TEXT        NOT NULL UNIQUE,      -- evt_test_...
    event_type      TEXT        NOT NULL,             -- checkout.session.completed
    session_id      TEXT,                             -- data.object.id (cs_test_...)
    livemode        BOOLEAN     NOT NULL,             -- always false here
    raw_body        BYTEA       NOT NULL,             -- exact bytes that were verified
    payload         JSONB,                            -- parsed copy; NULL if jsonb can't hold it
    payload_error   TEXT,                             -- why payload is NULL (e.g. \u0000)
    signature_ts    TIMESTAMPTZ NOT NULL,             -- t= from the header, UTC
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(), -- set by the database
    duplicate_count INTEGER     NOT NULL DEFAULT 0    -- added in M2: times seen again
);
-- CHECK ((payload IS NULL) <> (payload_error IS NULL))  -- exactly one of the two
-- Indexed but not unique: Stripe can send two distinct events for the same object.
CREATE INDEX ix_webhook_events_session_id ON webhook_events (session_id);

CREATE TYPE job_status AS ENUM ('pending', 'running', 'retrying', 'succeeded', 'dead');

CREATE TABLE sync_jobs (
    id            BIGSERIAL PRIMARY KEY,
    event_id      TEXT        NOT NULL REFERENCES webhook_events(event_id),
    sink          TEXT        NOT NULL,               -- 'csv', 'google_sheets', ...
    status        job_status  NOT NULL DEFAULT 'pending',
    attempts      INTEGER     NOT NULL DEFAULT 0,
    max_attempts  INTEGER     NOT NULL DEFAULT 8,
    next_run_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until  TIMESTAMPTZ,
    locked_by     TEXT,
    last_error    TEXT,
    replay_count  INTEGER     NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, sink)
);
CREATE INDEX sync_jobs_claim_idx ON sync_jobs (status, next_run_at);

-- Failure log: every attempt, kept forever (replay does not delete history).
CREATE TABLE job_attempts (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT      NOT NULL REFERENCES sync_jobs(id),
    attempt_no    INTEGER     NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ NOT NULL,
    outcome       TEXT        NOT NULL,   -- 'succeeded' | 'retryable_error' | 'permanent_error'
    error_type    TEXT,
    error_message TEXT                    -- truncated, never contains secrets
);
```

Dead-letter = `sync_jobs` rows with `status = 'dead'`, joined to their `job_attempts`.
Replay sets `status='pending'`, `attempts=0`, `next_run_at=now()`, increments
`replay_count`, and keeps all previous attempts.

Rows the sink writes (CSV columns): `event_id, event_created, session_id, customer_email,
amount_total, currency, payment_status, synced_at`.

`amount_total` is written as the integer Stripe sends, in the currency's smallest unit
(`2198` with `usd` means 21.98 USD; `500` with `jpy` means 500 JPY), always next to
`currency`. Nothing divides by 100, because that is wrong for zero-decimal currencies
(confirmed in Stripe's currencies docs).

### How a sink finds "the row" for an event

`event_id` is always the first column and is the only key. Sinks never store a row
number, because row numbers in a shared spreadsheet are not stable.

- **CsvSink:** reads the file, replaces the line whose `event_id` matches or appends a new
  one, writes to a temp file and atomically renames it over the original. Single writer
  per file is assumed (one worker writes CSV; documented).
- **GoogleSheetsSink (optional):** reads the `event_id` column, finds the matching row by
  value, updates that row's range if found, otherwise appends. Exact API calls will be
  checked against Google's Sheets API docs when this sink is built.
- **Someone reorders rows:** harmless. The lookup is by value at write time.
- **Someone deletes a row:** nothing recreates it automatically, because the job is
  already `succeeded`. The database is the source of truth and the sheet is a projection;
  `replay --event-id` (or a later `resync` command) writes the row again.
- **Someone duplicates a row by hand:** the sink updates the first match and logs a
  warning naming the event id; it does not delete rows it did not create.
- Edits to a row's other cells are overwritten the next time that event is synced.

## 5. Milestones

### M1: Verified intake (about 20 h)

Scaffold (pyproject, Compose with Postgres, settings, Alembic), `POST /webhooks/stripe`,
signature verification module, raw-event storage, simulator CLI, contract tests.

Done when:
- [ ] `docker compose up` starts Postgres and the API; `alembic upgrade head` creates the tables.
- [ ] `verify_signature(raw_body, header, secret, now, tolerance)` is a pure function and
      tests cover: valid, tampered body, wrong secret, expired timestamp, future
      timestamp, rolled secret (two `v1`, one matches), only `v0` present, missing header,
      malformed header, non-integer `t`, tolerance of 0 refused.
- [ ] Simulator produces each variant: valid, `--tamper-body`, `--wrong-secret`,
      `--expired`, `--duplicate`, `--rolled-secret`, `--missing-header`,
      `--malformed-header`, plus `--with-v0` and `--payment-status unpaid`, and can print
      or POST the request. `--send` targets only localhost unless `--allow-remote` is
      given, uses a timeout, and `--expect STATUS` sets the exit code. `--print` never
      shows a secret.
- [ ] Running each simulator variant against the local API gives the expected status
      (200 or 400), shown in a recorded test run.
- [ ] A valid event is stored with `raw_body` byte-identical to what was sent.
- [ ] Contract test vectors (fixed secret, timestamp, body, expected signature) are
      checked in, and, if approved, verified by the official `stripe` library offline.
- [ ] `.env.example` lists every setting; no secret is committed.

### M2: Reliable delivery (about 22 h)

Idempotency, job table, worker, backoff with jitter, dead-letter failure log, replay.

Done when:
- [ ] Sending the same event id twice stores one event, one job, returns 200 both times,
      and increments `duplicate_count`.
- [ ] Event and job are written in one transaction (test: forced failure on job insert
      leaves no event row).
- [ ] Worker syncs pending jobs to `CsvSink` (`output/events.csv`); re-running a job does
      not duplicate the row.
- [ ] With `FlakySink` failing N times, the job succeeds on attempt N+1 and `job_attempts`
      shows N failures; backoff delays fall within the expected jitter bounds (seeded).
- [ ] With a permanent error, or after `max_attempts`, the job is `dead`.
- [ ] `replay` CLI re-queues by `--job-id`, `--event-id` or `--all-dead`, and the job then
      succeeds once the sink is healthy; history is kept.
- [ ] Two worker processes running at once never process the same job twice (test).
- [ ] A worker killed mid-job leaves a job that is reclaimed after the lease expires.

### M3: Chaos and comparison (about 24 h)

Chaos scenario runner, n8n rebuild, comparison README.

Done when:
- [ ] A scenario runner replays a scripted mix (bursts, duplicates, bad signatures,
      sink outages, worker restarts) and prints a summary: received, rejected, stored,
      synced, dead, duplicate rows in sink (expected 0).
- [ ] The same scenarios run against an n8n workflow (self-hosted in Docker, no account),
      using the Webhook node with Raw Body, the Crypto node (HMAC SHA256, hex) and a
      local CSV or spreadsheet output. Exported workflow JSON is committed with no secrets.
- [ ] A comparison table, backed by the scenario runner's output for both, covers:
      signature verification, raw body handling, timestamp tolerance, idempotency, ack
      timing, retries/backoff, dead-letter, replay, observability, testability, effort.
- [ ] README states clearly that all traffic was simulated.

Optional after M2: `GoogleSheetsSink` (service account key in `secrets/`, never committed;
details verified against Google's docs at the time).

## 6. Signature verification spec (confirmed against Stripe docs)

Checked on 2026-10-07: <https://docs.stripe.com/webhooks> ("Verify webhook signatures
manually", "Preventing replay attacks", "Roll endpoint signing secrets") and
<https://docs.stripe.com/events/manage-webhook-endpoints> (signature errors, duplicates).

1. Header `Stripe-Signature`, single line, comma-separated `prefix=value` items.
   `t` is the timestamp; `v1` items are signatures (there may be several). Ignore every
   other scheme, including the `v0` Stripe adds to test events (downgrade protection).
2. `signed_payload = <t as string> + "." + <raw request body>`.
3. Expected = hex HMAC-SHA256 of `signed_payload`, key = the endpoint secret (the whole
   `whsec_...` string, UTF-8, as the official library does).
4. Compare against each `v1` with a constant-time comparison (`hmac.compare_digest`);
   any match passes (rolled secrets: Stripe sends one signature per active secret for up
   to 24 h).
5. Check the timestamp against a tolerance; libraries default to 5 minutes. A tolerance
   of 0 disables the check in Stripe's libraries, so this project refuses 0 or negative
   values at configuration time.
6. Use the raw, unmodified body; any re-serialisation breaks verification.

Differences from the official Python library (stripe 16.0.0) are listed, with reasons, in
`tests/fixtures/signature_vectors.json` and checked by tests marked `library_behaviour`.

Timestamp boundaries (tolerance 300 s; `t - now` shown; this table moves to the README):

| `t - now` | Ours (`abs(now - t) <= tol`) | Library (`t < now - tol` rejects) |
| --- | --- | --- |
| -301 | reject | reject |
| -300 | accept (inclusive) | accept (inclusive) |
| +5 | accept | accept |
| +300 | accept (inclusive) | accept |
| +301 | reject | accept (no future check) |

## 7. Testing strategy

- **Unit (pure, fast):** header parsing, signature computation, verification decisions,
  backoff calculation (seeded random, injected clock), row mapping, CSV escaping.
- **Contract vectors:** a checked-in JSON file of cases (secret, timestamp, body, header,
  now, expected result). Run against our verifier and, if approved, against
  `stripe.WebhookSignature.verify_header`. Confirmed from the stripe-python source
  (`stripe/_webhook.py`): verification is pure local HMAC with no network call and needs
  no API key. The SDK's own `generate_signature_header` is not used to create vectors, so
  the simulator's signing code is checked independently.
- **Integration (real Postgres from Compose):** endpoint to DB, duplicate handling,
  transactional outbox, worker with `FlakySink`, dead-letter, replay, concurrent workers,
  lease expiry. Each test uses its own schema or truncates tables.
- **End-to-end smoke:** simulator against the running API and worker, checking the CSV.
- **Chaos (M3):** scenario runner, same scripts for Python and n8n.
- Rule: a feature is only described as working after its tests ran and passed.

## 8. Security notes

- Signature check runs before JSON parsing; unsigned or failing requests store nothing and
  get a generic `400` (no detail about which check failed in the response; details go to
  the log).
- Constant-time comparison; tolerance must be > 0; future timestamps rejected.
- Request body size limit (for example 256 KB) before reading into memory.
- Secrets only in `.env` (ignored by git); `.env.example` holds placeholders. Logs never
  include the secret or the full signature header.
- All data is fake: `evt_test_...`, `cs_test_...`, `example.com` emails, a fake
  `whsec_test_fake_...` secret.
- CSV formula injection: values starting with `=`, `+`, `-`, `@` are escaped before
  writing, since the output is meant to be opened in a spreadsheet.
- Payload is untrusted even after verification: fields are validated before use.
- Not covered (stated in README): HTTPS, Stripe IP allowlisting, secret rotation tooling,
  authentication for the replay CLI beyond local access.

## 9. README outline

1. What it is, in one paragraph, plus "all events are simulated; no Stripe account used".
2. Architecture diagram and the five guarantees (verified, stored once, fast ack, retried,
   replayable).
3. Quickstart: `cp .env.example .env`, `docker compose up`, run simulator, open the CSV.
4. Simulator usage and the broken-event variants. Notes: the payload is a documented
   subset of the real objects; its JSON formatting is not Stripe's, which does not matter
   because verification runs over raw bytes; `evt_test_` and `pi_test_` are this
   project's fake markers, while `cs_test_` is Stripe's real test-mode prefix;
   `api_version` is copied from Stripe's docs example and was not tested against.
5. Signature verification: how it follows Stripe's documented scheme, and the SDK
   cross-check.
6. Reliability: idempotency, outbox, worker, backoff with jitter, dead-letter, replay.
7. Testing: how to run, what is covered, sample output.
8. Chaos results and the Python vs n8n comparison.
9. What this project proves and what it does not.
10. Limitations and possible next steps.

## 10. What this project proves and what it does not

### Proves (once the milestones' checks pass)

- A correct implementation of Stripe's documented webhook signature scheme, including
  rolled secrets, scheme filtering, constant-time comparison and replay tolerance,
  cross-checked against the official library's offline verifier.
- Correct handling of the core webhook reliability problems: fast acknowledgement,
  durable storage before ack, idempotency on event id, at-least-once background delivery
  with backoff and jitter, a visible failure log, dead-lettering and operator replay.
- That these behaviours are tested, including concurrency and crash recovery, and
  reproducible on any machine with Docker and no accounts.
- A reasoned comparison of hand-written code against a low-code tool for the same job.

### Does not prove

- That it has received real Stripe traffic. It has not; every event is simulated.
- Compatibility with every real `checkout.session.completed` payload variant or API
  version, or with thin events.
- Production readiness: no HTTPS, deployment, monitoring, alerting, or IP allowlisting.
- Performance or behaviour at real scale.
- Exactly-once delivery (the design deliberately provides at-least-once plus idempotent
  writes).
- Anything about the Google Sheets API unless the optional sink is built and tested.

## 11. Open questions

- **Second duplicate check (decide in M2).** Stripe's docs say two separate Event objects
  are sometimes generated for the same change, and suggest identifying them by
  `data.object.id` plus `event.type`. M1 stores `session_id` (indexed, not unique) so
  this is possible; M2 decides whether to treat a second event with the same
  `session_id` and `type` as a duplicate, and what to do with it.
- **Events with `livemode: true` (decide in step 6).** The project is test-mode only, so
  a correctly signed event claiming `livemode: true` should not be stored. Step 6
  proposes the response code, with reasons (it affects whether a real sender would
  retry), before implementing it.
