# Morning report: 2026-10-08

Milestone 1 (verified intake) is built and all seven of its "done when" checks pass. The
smoke test ran against the real container: twice on the existing stack and once on a
stack started from scratch. Nothing was pushed and no history was rewritten. M2 has not
been started.

Note: I could not see your description of this report from last night, so it follows
the five sections you listed today.

## 1. What was built and why

Overnight commits, oldest first:

| Commit | What |
| --- | --- |
| `5f89b48` | Open item 1: a `checkout.session.*` event with a missing or malformed `data.object.id` is stored (200, `session_id` NULL) and logs a warning with only the event id and type. |
| `ff9464e` | Open item 2: the httpx2 findings, written as an open question in `docs/decisions.md`. Nothing installed. |
| `ea73a54` | The step 7 plan, written to `docs/decisions.md` before building. |
| `152dada` | API container: `Dockerfile`, `.dockerignore`, Compose `api` service, uvicorn log config, `API_PORT`. |
| `0e434c6` | `tools/smoke.py` and its offline tests. |
| `cdb5485` | `README.md` stub. |

How M1 fits together, for the learning log:

- **Request path.** `POST /webhooks/stripe` reads at most `MAX_BODY_BYTES`, and stops
  even if the client lies about `Content-Length`. It then verifies the `Stripe-Signature`
  header over the exact raw bytes, parses and validates the event, refuses live-mode
  events, and stores the event with `INSERT ... ON CONFLICT (event_id) DO NOTHING`. It
  answers only after the row is committed, so an acknowledged event cannot be lost, and a
  repeated event is a harmless no-op.
- **Why the raw bytes are stored separately from the parsed JSON.** The signature covers
  the bytes exactly as sent. PostgreSQL's `jsonb` reorders keys, drops whitespace and
  duplicate keys, and cannot hold `\u0000`. So `raw_body` (`BYTEA`) is the source of
  truth, and `payload` (`JSONB`) is a convenience copy that may be NULL, with
  `payload_error` saying why.
- **Why the container runs migrations at startup.** `docker compose up` alone gives a
  working stack with the current schema. The migration step is idempotent, so a restart
  changes nothing.
- **Why the smoke test drives the simulator CLI.** It exercises the CLI exactly as a
  person would, including `--send` and `--expect`, as designed in step 3. It then
  rebuilds the same requests from the seed to check the database byte for byte.
- **Safety choices.** Both containers are bound to `127.0.0.1` only. The API runs as a
  non-root user. The image contains no `.env`, tests or test-only libraries. Logs carry
  event ids and types, never customer fields.

## 2. What I ran, with results

Every test run was wrapped in `timeout`. Ruff and pytest were run as separate commands.

| What | Result |
| --- | --- |
| `ruff check .` and `ruff format --check .` | clean |
| `REQUIRE_DB=1 pytest` (full suite, database up) | **304 passed**, 1 warning (the httpx deprecation), 28.6 s |
| `docker compose build api` | built on the first attempt |
| Image inspection | runs as uid 10001 (`app`); Python 3.13.16; fastapi 0.142.4, uvicorn 0.54.0, SQLAlchemy 2.1.4, alembic 1.20.0, psycopg 3.3.6, pydantic-settings 2.15.0; no stripe, pytest or httpx; no `.env` |
| `docker compose up -d --build --wait` | db and api healthy; the dev database migrated from `0001` to `0002` |
| `python tools/smoke.py`, run 1 | **SMOKE PASSED** (output below) |
| `python tools/smoke.py`, run 2 | **SMOKE PASSED** |
| Database after both runs | 10 rows, 10 distinct event ids, 0 `payload_error`, 0 NULL `session_id` |
| API container log | 10 "stored", 2 "duplicate", 10 "rejected", 22 `POST` lines; **0** matches for `@example.com`, `Test Buyer`, `cs_test_` or `whsec_` |
| Cold start: isolated project `ikcold`, fresh volume, ports 5434/8002 | both healthy; migrations at `0002`; tables `alembic_version` and `webhook_events`; databases `integrationkit` and `integrationkit_test` created; smoke (status codes only) **PASSED**, 5 rows stored; then `down -v` removed only that project |
| `.env.example` compared with every variable read by Settings, Compose and the tests | nothing missing |
| `git ls-files .env` | not tracked |
| `whsec_` values in tracked files | all `whsec_test_fake_...` |

Smoke run 1, verbatim (against `http://127.0.0.1:8001/webhooks/stripe`):

```
valid: 200
ok   valid: expected 200
valid: 200
ok   with_v0: expected 200
valid: 200
ok   payment_unpaid: expected 200
rolled_secret: 200
ok   rolled_secret: expected 200
duplicate_1: 200
duplicate_2: 200
ok   duplicate: expected 200
tamper_body: 400
ok   tamper_body: expected 400
wrong_secret: 400
ok   wrong_secret: expected 400
expired: 400
ok   expired: expected 400
missing_header: 400
ok   missing_header: expected 400
malformed_header: 400
ok   malformed_header: expected 400
database: checked 10 event ids (5 expected stored once)
SMOKE PASSED
```

## 3. M1 "done when" checks

| # | Check (from `docs/strategy.md`) | Status | Evidence |
| --- | --- | --- | --- |
| 1 | `docker compose up` starts Postgres and the API; `alembic upgrade head` creates the tables | **Pass** | Existing stack and cold start both healthy; `alembic_version` = `0002`; `webhook_events` exists. The CLI `alembic upgrade head` was also run by hand in step 5. |
| 2 | `verify_signature` is pure; tests cover valid, tampered, wrong secret, expired, future, rolled, `v0` only, missing, malformed, non-integer `t`, tolerance 0 | **Pass** | `tests/test_signature.py`, plus the boundary vectors |
| 3 | Simulator variants, `--with-v0`, `--payment-status unpaid`, print or POST, localhost-only `--send`, timeout, `--expect`, no secret in `--print` | **Pass** | `tests/test_simulate.py`; the smoke test used `--send`/`--expect` for real |
| 4 | Each simulator variant against the local API gives the expected status, shown in a recorded run | **Pass** | The smoke output above (the real container, not the test client) |
| 5 | A valid event is stored with `raw_body` byte-identical to what was sent | **Pass** | Endpoint tests, and the smoke database check on every accepted event |
| 6 | Contract vectors checked in, verified by the official `stripe` library offline | **Pass** | 25 vectors (9 documented differences); `tests/test_stripe_crosscheck.py` |
| 7 | `.env.example` lists every setting; no secret committed | **Pass** | The comparison and scans in section 2 |

I left the checkboxes in `docs/strategy.md` unticked so you can tick them yourself after
reviewing.

## 4. What needs your review

1. **httpx vs httpx2** (open question in `docs/decisions.md`): switch the test client
   dependency now, later or never.
2. **Decisions I made overnight in step 7:**
   - Default API host port **8001**. Your `answerdesk` stack already holds 8000 and 5432;
     I left those containers alone.
   - Image pinned to `python:3.13.16-slim-bookworm`. The local venv is 3.13.1, so the
     minor version matches but the patch version does not.
   - Migrations run at container start (`alembic upgrade head && exec uvicorn ...`).
   - Health check is a TCP connect from inside the container; no `/healthz` endpoint
     was added.
   - The smoke test writes fake events into the **dev** database (`integrationkit`). It
     now holds 10 rows from the two runs.
   - Only direct dependencies are pinned. Transitive packages in the image (starlette,
     pydantic, anyio, ...) resolve to the latest compatible versions at build time, so a
     later rebuild can differ. A lock file would fix that; it is not added.
   - The README's quickstart uses Windows paths (`.venv/Scripts/...`) and mentions
     `bin/` for Linux and macOS.
3. **Local files changed outside git:** `.env` got `API_PORT=8001` appended (fake values
   only).
4. **The 2026-10-07 hang:** cause still unknown (see `docs/decisions.md`). Nothing hung
   tonight.
5. **Alembic is silent at container start:** it has no logging config, so "applied
   migration 0002" does not appear in `docker compose logs`. I checked the result via
   `alembic_version` instead. It is easy to add if you want it visible.

## 5. What failed

Nothing failed tonight, and no step needed the three-attempt fallback. Two small things
were changed before their first real run:

- The log-config test was made independent of the working directory before it was run.
- The image kept a `build/` directory from `pip install`; the Dockerfile now removes it.
  This was found by inspecting the image, not by a failure.

Earlier in step 6 (already reported): two test bugs (the `1e400` case and a ~200 KB test
id) and two schema bugs caught by tests (JSON `null` vs SQL NULL, and a doubled
constraint name). All were fixed before their commits.

Stack state now: `integrationkit-db-1` and `integrationkit-api-1` are running and
healthy. Stop them with `docker compose stop`.
