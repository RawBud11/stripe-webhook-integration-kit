# IntegrationKit

Reliable processing of Stripe-style webhooks: verify the signature, store the event once,
answer quickly, and (from milestone 2) sync each event to a spreadsheet in the background
with retries, a failure log and replay. The same flow will later be rebuilt in n8n and
the two compared.

> **All events are simulated.** This project has no Stripe account, uses no Stripe API
> keys and never contacts Stripe. Every event comes from the local simulator in
> `tools/simulate.py`, signed with Stripe's documented scheme and a fake secret.

Status: milestone 1 (verified intake) is built. See [docs/strategy.md](docs/strategy.md)
for scope, architecture and milestones, and [docs/decisions.md](docs/decisions.md) for
decisions and open questions.

## Quickstart

Needs Docker and, for the tools and tests, Python 3.13.

```sh
cp .env.example .env                   # fake values; adjust ports if they are taken
docker compose up -d --build --wait    # Postgres + API; the API applies migrations on start
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,test]"   # bin/ on Linux/macOS
.venv/Scripts/python tools/simulate.py --send http://127.0.0.1:8001/webhooks/stripe --expect 200
.venv/Scripts/python tools/smoke.py    # every simulator case + database checks
.venv/Scripts/python -m pytest         # database tests skip unless the db is up (REQUIRE_DB=1 to fail)
```

Default host ports: API `127.0.0.1:8001`, Postgres `127.0.0.1:5433` (`API_PORT`,
`POSTGRES_PORT` in `.env`).

### Pinned dependencies

Direct dependencies are pinned in `pyproject.toml`. Indirect ones (starlette, pydantic,
anyio, ...) are pinned by `constraints.txt`, which the Docker image installs with
`pip install -c constraints.txt .`. A constraint only applies to a package that actually
gets installed, so the dev and test entries in the file do not end up in the image.

The file is a `pip freeze` of the working venv. To regenerate it after changing a
dependency, install the change in the venv, run the tests, then:

```sh
.venv/Scripts/python -m pip freeze --exclude-editable > constraints.txt   # Git Bash, Linux, macOS
```

In Windows PowerShell 5.1, `>` writes UTF-16, which pip cannot read; use
`... | Out-File -Encoding ascii constraints.txt` instead. Rebuild the image afterwards.
The freeze comes from a Windows venv, so a Linux-only dependency would be missing from
it; compare `docker compose run --rm --no-deps api pip freeze` against the file after
rebuilding.

## The endpoint

`POST /webhooks/stripe`

| Situation | Response | Stored? |
| --- | --- | --- |
| Valid signature, well-formed event | `200 {"received": true}` | once, raw bytes kept |
| Same event id again | `200` | no (already stored) |
| Bad, missing or expired signature | `400` (generic) | no |
| Signed, but not JSON / no valid `id`, `type`, `livemode` | `400` (generic) | no |
| Signed, `livemode: true` (this service is test mode only) | `422` | no |
| Signed, payload PostgreSQL jsonb cannot hold (e.g. `\u0000`) | `200` | yes, with `payload_error` |
| Body over `MAX_BODY_BYTES` | `413` | no |
| Database unreachable | `503` | no (sender should retry) |
| Unexpected error | `500` | no |

Error bodies never say which check failed.

## The simulator

`tools/simulate.py` (standard library only) produces fake `checkout.session.completed`
events: valid, `--tamper-body`, `--wrong-secret`, `--expired`, `--duplicate`,
`--rolled-secret`, `--missing-header`, `--malformed-header`, plus `--with-v0` and
`--payment-status`. `--send` only targets localhost unless `--allow-remote` is given;
`--expect STATUS` sets the exit code.

Notes on realism:

- The payload is a documented subset of Stripe's Event and Checkout Session objects,
  not every field.
- Its JSON formatting is not Stripe's. That does not matter: verification runs over the
  exact raw bytes received, whatever their formatting.
- `evt_test_` and `pi_test_` are this project's own "fake" markers. Real Stripe event ids
  are `evt_...` in both modes. `cs_test_` is Stripe's real test-mode prefix.
- `api_version` is `2019-02-19`, copied from the example in Stripe's Event object docs.
  It is not a version this project was tested against, and no code depends on it.

## Signature verification

Follows Stripe's documented manual scheme (docs.stripe.com/webhooks, "Verify webhook
signatures manually"): HMAC-SHA256 of `<t>.<raw body>`, only `v1` signatures, any
matching `v1` accepted (rolled secrets), constant-time comparison on bytes, and a
timestamp tolerance that must be greater than 0.

It is cross-checked offline against Stripe's official Python library (`stripe==16.0.0`,
test-only) using 25 vectors in `tests/fixtures/signature_vectors.json`.

Timestamp boundaries (tolerance 300 s):

| `t - now` | This project | stripe 16.0.0 |
| --- | --- | --- |
| -301 | reject | reject |
| -300 | accept (inclusive) | accept (inclusive) |
| +300 | accept (inclusive) | accept |
| +301 | reject | accept (no future check) |

Where the library behaves differently (each has a test marked `library_behaviour`, so a
library upgrade that changes it is noticed):

| Case | This project | stripe 16.0.0 |
| --- | --- | --- |
| Timestamp more than 300 s in the future | reject | accept |
| Body that is not UTF-8 | verified over raw bytes | `UnicodeDecodeError` |
| Non-ASCII `v1` value | mismatch | `TypeError` |
| `t=1_790_000_000`, `t=+…`, `t= …`, non-ASCII digits | malformed | accept |
| Two `t` values | malformed | accept (uses the first) |
| Header item without `=` | malformed | accept |

None of the lenient cases lets a forged request through: the HMAC must still match.

## What this project proves and what it does not

See the section of that name in [docs/strategy.md](docs/strategy.md). In short: it shows
a correct, tested implementation of the documented scheme and of webhook intake
reliability; it does not show that it has handled real Stripe traffic, because it has
not.

## Limitations

- **Migrations run at container start.** The API container runs `alembic upgrade head`
  before it serves requests. That is fine for one instance, but not for several: they
  would all try to upgrade at the same time, and one could serve requests against a
  schema another is still changing. With more than one instance, migrations belong in a
  separate step that runs once before the new instances start.
