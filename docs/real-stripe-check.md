# Optional: one real Stripe sandbox delivery

> **Optional and manual.** Nothing in this project, its tests or its smoke test needs a
> Stripe account or API keys. This page describes a single check the author runs by
> hand: one real `checkout.session.completed` event from a Stripe **sandbox** (test
> mode), forwarded by the Stripe CLI to the local endpoint. Never use live mode or live
> keys (`sk_live_`, `rk_live_`) for it.
>
> Every value below is a placeholder. Never commit a real key or signing secret.

Status: not yet run. Until it is, everything this project has received is simulated.

Stripe's docs now call test mode a *sandbox*; sandbox secret keys start with `sk_test_`.

## Placeholders

| Placeholder | What it is | Where it comes from |
| --- | --- | --- |
| `<SANDBOX_SECRET_KEY>` | `sk_test_...` key of a Stripe sandbox | Dashboard, API keys page, sandbox selected |
| `<CLI_SIGNING_SECRET>` | `whsec_...` secret the CLI signs forwarded events with | `stripe listen --print-secret` |
| `<EVENT_ID>` | id of the forwarded event (`evt_...`) | `stripe listen` output or the API log |

## Before you start

- The stack is running: `docker compose up -d --wait`. The API listens on
  `127.0.0.1:8001` (or your `API_PORT`).
- The Stripe CLI is installed (`stripe --version`). Installation options:
  <https://docs.stripe.com/stripe-cli> and <https://github.com/stripe/stripe-cli#installation>.
- Your computer's clock is accurate: the endpoint rejects timestamps more than 300 s away
  from its own clock.

## 1. Log the CLI in to the sandbox

```sh
stripe login --interactive
```

Paste `<SANDBOX_SECRET_KEY>` when prompted. A `sk_test_` key only reaches sandbox data,
so nothing in this check can touch live mode. Using the prompt keeps the key out of your
shell history.

Alternatively, `stripe login` uses a browser pairing flow; then check with
`stripe login list` that a sandbox is active, and never use `stripe switch --live` or any
`--live` flag. For CLI versions newer than v1.50.0, an Administrator may first need to
enable CLI access under Settings, Team and security, MCP and CLI access.

## 2. Give the API the CLI's signing secret

The CLI signs forwarded events with its own secret, not with the fake one in `.env`.

```sh
stripe listen --print-secret
```

Copy the `whsec_...` value it prints (`<CLI_SIGNING_SECRET>`; Stripe's docs say it does
not change between `listen` restarts). In `.env`, change only this line:

```
WEBHOOK_SECRET=<CLI_SIGNING_SECRET>
```

Then recreate the API container so it picks up the new value (a plain `restart` would
keep the old environment):

```sh
docker compose up -d --wait api
```

While this secret is set, `tools/smoke.py` fails, because it signs with the secret in
`.env` and expects the fake one. Don't run it until step 6.

## 3. Start forwarding (terminal A)

```sh
stripe listen --events checkout.session.completed --forward-to http://127.0.0.1:8001/webhooks/stripe
```

- `--events checkout.session.completed`: the trigger in step 4 creates other objects and
  their events too. The endpoint stores every correctly signed test-mode event, so
  without the filter those would be stored as well.
- `127.0.0.1`, not `localhost`: the API port is bound to IPv4 loopback only, and
  `localhost` can resolve to `::1` first on Windows.

Wait for the `Ready!` line and leave it running.

## 4. Trigger the event (terminal B)

```sh
stripe trigger checkout.session.completed
```

This makes real API requests to your sandbox and creates the objects the event needs
(Stripe's CLI reference: "triggering events causes side effects: all necessary API
objects will be created in the process"). They stay in the sandbox; no money moves.

## 5. Check the result

**Terminal A** shows the event going out (`--> checkout.session.completed [<EVENT_ID>]`)
and a line with the status our endpoint returned. Expected: `200`.

**API log:**

```sh
docker compose logs api | grep <EVENT_ID>
```

Expected: `stored event <EVENT_ID> of type checkout.session.completed`.

**Database.** Simulated events all start with `evt_test_`; real Stripe ids do not:

```sh
docker compose exec db psql -U integrationkit -d integrationkit -c "SELECT event_id, event_type, session_id, livemode, octet_length(raw_body) AS bytes, payload_error, received_at FROM webhook_events WHERE event_id NOT LIKE 'evt\_test\_%' ORDER BY received_at"
```

Expected: one row with your `<EVENT_ID>`, type `checkout.session.completed`, a
`session_id` starting with `cs_test_`, `livemode` false and `payload_error` empty.

If the status is not `200`:

| Status | Likely cause |
| --- | --- |
| `400` | The API still has the old secret (step 2 not applied or container not recreated), or the clock is off by more than 300 s. The API log says which check failed. |
| `422` | The event claimed `livemode: true`. Stop: the CLI is not on a sandbox. |
| `503` | The database is down. |

Write down the date, `stripe --version`, the status and the row you saw: that is the
evidence for closing the open item in `docs/plan.md`, section 11.

## 6. Clean up

1. Stop `stripe listen` in terminal A with Ctrl+C.
2. Put the fake secret back in `.env`. Unless you changed it, the value is the one in
   `.env.example`:

   ```
   WEBHOOK_SECRET=whsec_test_fake_replace_me_with_any_random_string
   ```

   Then recreate the API and confirm the simulator works again:

   ```sh
   docker compose up -d --wait api
   .venv/Scripts/python tools/smoke.py      # expect SMOKE PASSED
   ```

3. Remove the stored real event from the dev database. Check what will be removed first,
   then delete by id:

   ```sh
   docker compose exec db psql -U integrationkit -d integrationkit -c "SELECT event_id, event_type FROM webhook_events WHERE event_id NOT LIKE 'evt\_test\_%'"
   docker compose exec db psql -U integrationkit -d integrationkit -c "DELETE FROM webhook_events WHERE event_id = '<EVENT_ID>'"
   ```

   Expected: `DELETE 1`. This is correct as of milestone 1, where `webhook_events` is the
   only table. Once milestone 2 adds `sync_jobs` and `job_attempts`, their rows for the
   event must be deleted first (the foreign keys refuse otherwise), and this page will be
   updated.
4. Log the CLI out, which revokes the session and clears its stored credentials:

   ```sh
   stripe logout
   ```

5. The objects `stripe trigger` created stay in your sandbox. They are test data and can
   be left there.

## What this check shows, and what it does not

It shows that one event created by Stripe, signed by Stripe's tooling and delivered over
HTTP by the Stripe CLI, passes this endpoint's signature check and is stored byte for
byte. It does not show delivery to a registered HTTPS endpoint, Stripe's own retry
schedule, thin events, other payload variants or API versions, or anything about live
mode.

## Sources

Checked on 2026-10-08:

- <https://docs.stripe.com/webhooks>, "Test locally without a registered URL":
  `stripe login`, `stripe listen --forward-to`, the `whsec_` secret printed by `listen`,
  `stripe trigger`.
- <https://docs.stripe.com/stripe-cli/use-cli> (CLI reference): `listen` flags `--events`,
  `--forward-to`, `--print-secret`; "The webhook signing secret provided will not change
  between restarts to the `listen` command"; `trigger` side effects; `login
  --interactive`; the v1.50.0 CLI-access requirement; `switch --live`; `logout`.
- <https://docs.stripe.com/keys>: sandbox keys start with `sk_test_`, live keys with
  `sk_live_`; each mode has its own keys and objects are not shared between modes.
