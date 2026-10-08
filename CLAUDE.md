# CLAUDE.md

IntegrationKit is a portfolio project: reliable webhook processing for a Stripe-style
payment event (verify signature, store once, return 2xx fast, sync to a spreadsheet sink
in the background with retries, a failure log and replay), then the same flow rebuilt in
n8n and compared. See `docs/strategy.md` for scope, architecture and milestones.

**Hard constraint:** the project, its tests and its smoke test must never require a Stripe
account, Stripe API keys, or network access to Stripe; every event they use comes from
the local simulator. The one exception is an optional manual check, described in
`docs/real-stripe-check.md`, that the author runs by hand with their own sandbox
(test-mode) keys. It is test mode only, never live mode or live keys. Claude never runs
it or handles its keys or secrets, and nothing in the project may depend on it or on its
result.

## Working rules

- Before writing code, state the plan in 3-6 lines and wait for approval.
- Small steps, one logical change per commit, clear commit messages.
- Write tests for core logic alongside the implementation, in the same change.
- Ask before adding any dependency; explain what it does and why it is needed.
- Secrets live only in `.env`; keep `.env.example` current. Everything uses fake data
  (ids like `evt_test_...`, fake `whsec_...` secrets, fake customers).
- Verify Stripe, n8n and Google details against current official docs, and say what was
  checked (page and what it confirmed).
- Never claim something works unless it was run and tested. Never describe simulated
  events as real Stripe traffic.
- After each milestone, explain what was built so the author can write
  `docs/learning-log.md` themselves. Do not write the learning log entries.
