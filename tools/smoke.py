"""Smoke test against the running stack (docker compose up).

Sends every simulator case through the simulator CLI with --expect, then checks the
database: each accepted event stored exactly once with the exact bytes sent, and no
rejected event stored. All events are simulated; nothing touches Stripe.

Run: python tools/smoke.py [--url http://127.0.0.1:8001/webhooks/stripe]
Exit code 0 only if every check passes.
"""

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import simulate
from sqlalchemy import select

from integrationkit.db import make_engine
from integrationkit.models import WebhookEvent
from integrationkit.settings import Settings


@dataclass(frozen=True)
class Case:
    label: str
    flags: tuple[str, ...]
    expected: int
    variant: str | None = None
    build_kwargs: dict = field(default_factory=dict)


CASES = (
    Case("valid", (), 200),
    Case("with_v0", ("--with-v0",), 200, build_kwargs={"with_v0": True}),
    Case(
        "payment_unpaid",
        ("--payment-status", "unpaid"),
        200,
        build_kwargs={"payment_status": "unpaid"},
    ),
    Case("rolled_secret", ("--rolled-secret",), 200, "rolled_secret"),
    Case("duplicate", ("--duplicate",), 200, "duplicate"),
    Case("tamper_body", ("--tamper-body",), 400, "tamper_body"),
    Case("wrong_secret", ("--wrong-secret",), 400, "wrong_secret"),
    Case("expired", ("--expired",), 400, "expired"),
    Case("missing_header", ("--missing-header",), 400, "missing_header"),
    Case("malformed_header", ("--malformed-header",), 400, "malformed_header"),
)


def rebuild(case: Case, secret: str, seed: int, now: int) -> list[simulate.SimulatedRequest]:
    """The exact requests the CLI sent for this seed and time."""
    return simulate.build_requests(
        secret=secret,
        now=now,
        rng=random.Random(seed),
        variant=case.variant,
        **case.build_kwargs,
    )


def check_database(engine, sent: dict[str, tuple[Case, bytes]]) -> list[str]:
    """Problems found in the database for the events sent, as readable lines."""
    problems = []
    with engine.connect() as connection:
        for event_id, (case, body) in sent.items():
            rows = connection.execute(
                select(WebhookEvent.raw_body).where(WebhookEvent.event_id == event_id)
            ).all()
            if case.expected == 200:
                if len(rows) != 1:
                    problems.append(f"{case.label}: {event_id} stored {len(rows)} times")
                elif rows[0].raw_body != body:
                    problems.append(f"{case.label}: {event_id} raw_body differs from sent")
            elif rows:
                problems.append(f"{case.label}: rejected event {event_id} was stored")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8001/webhooks/stripe")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--skip-db", action="store_true", help="only check status codes")
    args = parser.parse_args(argv)

    try:
        secret = simulate.load_secret(args.env_file)
    except simulate.SimulatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    now = int(time.time())
    seeds = random.SystemRandom()
    sent: dict[str, tuple[Case, bytes]] = {}
    failures = 0

    for case in CASES:
        seed = seeds.randrange(2**32)
        cli_args = [
            *case.flags,
            "--send", args.url,
            "--expect", str(case.expected),
            "--seed", str(seed),
            "--now", str(now),
            "--env-file", str(args.env_file),
        ]  # fmt: skip
        code = simulate.main(cli_args)
        if code == simulate.EXIT_CONNECTION_ERROR:
            print(f"FAIL {case.label}: cannot reach {args.url}", file=sys.stderr)
            return 3
        status = "ok" if code == simulate.EXIT_OK else "FAIL"
        failures += code != simulate.EXIT_OK
        print(f"{status:4} {case.label}: expected {case.expected}")
        for request in rebuild(case, secret, seed, now):
            sent[json.loads(request.body)["id"]] = (case, request.body)

    if not args.skip_db:
        settings = Settings(_env_file=args.env_file)
        engine = make_engine(settings.database_url)
        try:
            problems = check_database(engine, sent)
        finally:
            engine.dispose()
        for problem in problems:
            print(f"FAIL database: {problem}")
        failures += len(problems)
        accepted = sum(case.expected == 200 for case, _ in sent.values())
        print(f"database: checked {len(sent)} event ids ({accepted} expected stored once)")

    print("SMOKE PASSED" if failures == 0 else f"SMOKE FAILED ({failures} problems)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
