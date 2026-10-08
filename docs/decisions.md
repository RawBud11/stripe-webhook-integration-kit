# Decisions log

Decisions made while working unattended, and commands that needed a permission that was
not available. Each entry says what was decided, why, and where it is in the code.
Anything here can be revisited.

## Permissions needed but not available

None so far.

## Step 6: webhook endpoint (2026-10-08)

- **Event id format:** `evt_` followed by 1-250 ASCII letters, digits or underscores.
  **Event type format:** 2-11 dot-separated parts of lowercase ASCII letters, digits or
  underscores (e.g. `checkout.session.completed`,
  `v1.billing.meter.error_report_triggered`). Anything else is a generic 400.
  (`src/integrationkit/events.py`)
- **Session id format:** `cs_` followed by 1-250 ASCII letters, digits or underscores.
- **`livemode` missing or not a boolean:** generic 400. Stripe always sends it, so an
  event without it cannot be classified as test mode.
- **Order of checks:** body size, signature, JSON and ids, livemode, then storage. So an
  unsigned body over the limit gets 413, not 400.
- **Payloads jsonb cannot hold:** besides U+0000 (approved), unpaired UTF-16 surrogates
  (PostgreSQL rejects them) and numbers beyond float range (Python would turn `1e400` into
  `inf`, which jsonb rejects) also go to `payload_error`, so a validly signed event never
  causes a 500. `NaN` and `Infinity` literals are not valid JSON and get a 400.
- **Parsed payload precision:** `payload` is parsed with Python floats, so a float with
  more digits than a double holds loses precision in the JSONB copy. `raw_body` keeps the
  exact bytes and is the source of truth.
- **Deep nesting:** JSON nested deeper than Python's parser allows is a generic 400.
- **503 vs 500:** 503 for connection failures (no SQLSTATE), SQLSTATE class 08,
  `57P01`/`57P02`/`57P03` (shutdown, starting up), `53300` (too many connections),
  invalidated connections and pool timeouts. Everything else, including a missing table,
  is 500. (`src/integrationkit/db.py`)
- **Response bodies:** `{"received": true}` for 200, including duplicates;
  `{"error": "..."}` with one fixed message per status code otherwise.
- **Docs and OpenAPI routes disabled** on the API: the service has a single machine-facing
  endpoint.
- **Logging:** logger `integrationkit.webhooks`. Rejections log only the error class or a
  fixed message; stored events log id and type. Application logging is not yet configured
  for the running server (step 7).
- **`checkout.session.*` with a missing or malformed `data.object.id`** (approved
  2026-10-08): stored, 200, `session_id` NULL, plus a warning log with event id and type
  only. (`ParsedEvent.missing_session_id`, tested in `test_webhook_endpoint.py`)

## Open question for you: httpx vs httpx2 in the test client (2026-10-08)

Not installed; tests keep using `httpx==0.28.1`. What I found:

- **The warning** comes from the installed Starlette 1.7.0
  (`starlette/testclient.py`): it imports `httpx2` if present, otherwise falls back to
  `httpx` and emits `StarletteDeprecationWarning: Using httpx with starlette.testclient is
  deprecated; install httpx2 instead.` With neither installed, the test client fails.
- **Starlette docs** (`docs/testclient.md` in the official repo, github.com/Kludex/starlette;
  www.starlette.io did not resolve from here): "The `TestClient` is built on `httpx2`.
  Plain `httpx` is still supported, but deprecated - install `httpx2` (included in
  `starlette[full]`) instead."
- **Starlette release notes** (`docs/release-notes.md`, same repo): 1.2.0 (2026-05-28)
  "Support httpx2 in the test client"; 1.2.1 (2026-05-31) uses httpx2 for type checking;
  1.3.0 (2026-06-11) adds httpx2 to the `full` extra. No entry announces a removal date
  for plain `httpx`, and I found no release-note line that names the deprecation itself;
  only the docs page and the code state it.
- **httpx2 on PyPI:** latest 2.13.1 (2026-09-23), 18 releases, first upload 0.0.0 on
  2026-05-11. Author: Tom Christie (the original httpx author); maintainer: Pydantic
  Services Inc.; source and homepage github.com/pydantic/httpx2. Requires Python >= 3.10
  and pulls in `httpcore2`, `anyio`, `idna`, `truststore`. No license field in the PyPI
  metadata (I did not check the repository's licence file).
- **Impact today:** none. One warning per test run, all tests pass. If a future Starlette
  drops `httpx`, the test client stops importing and every endpoint test fails loudly.

Question: switch the dev extra from `httpx` to `httpx2` now, later (for example when
Starlette announces removal), or never?

## Session note: the 2026-10-07 hang

The command that stalled ran `ruff format` (it completed: the files were rewritten at
23:28:46) and then pytest. The cause was not found: each part, and the same combination,
later finished in under 15 s. One test had a ~200 KB test ID (the deep-nesting body),
which produced two pytest errors but no hang when reproduced in isolation. Since then
every test run is wrapped in `timeout`, and ruff and pytest run as separate commands.
