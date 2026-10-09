# Backlog

Tasks found along the way that are not yet in a milestone's "done when" list.

## M2

- [ ] **API container exits with 137 on `docker compose stop`.** Seen 2026-10-08:
      Docker killed `api` after the 10 s grace period. The signal does arrive. The
      Dockerfile `exec`s uvicorn as PID 1, and the log shows `Shutting down`, then
      `Application shutdown complete` and `Finished server process [1]` within 0.3 s.
      The Python process still did not exit afterwards. A likely cause is a non-daemon
      thread (threadpool, DB pool) or something holding the interpreter open. That is
      not yet checked. No data is at risk, because events are committed before the 200.
      Done when: `docker compose stop` exits `api` with 0 in under 10 s, checked
      by a repeatable command or test.
