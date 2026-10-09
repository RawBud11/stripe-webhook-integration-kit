# Status: end of session 2026-10-08

- Moved the `~/integrationkit` history (28 commits) into this repo as a merge on top of
  `LICENSE`, so the commit hashes are unchanged. Secret scan of the full history: clean.
- Milestone 1 (verified intake) is reviewed and closed. Tagged `v0.1.0` (`828ad77`).
- Checks before the tag: ruff clean, `REQUIRE_DB=1 pytest` 305 passed, smoke test passed.
- Removed `docs/strategy.md` (identical to `docs/plan.md`); added Python `.gitignore` entries.
- Duplicates: in M1 a duplicate is a no-op that returns 200. `duplicate_count` and the
  `session_id`+`type` check come in M2, as `docs/plan.md` says.
- Next: Milestone 2 (reliable delivery). Decide early: create sync jobs only for
  `checkout.session.completed`, or for every stored event (plan §2 step 4)?
- Open: the API container exits with 137 on stop (`docs/backlog.md`); httpx vs httpx2
  (`docs/decisions.md`).
- Local: both Compose stacks are stopped; this repo has its own `.venv` and fake `.env`.
