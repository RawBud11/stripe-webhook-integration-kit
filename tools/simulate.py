"""Simulator for clearly fake, Stripe-style `checkout.session.completed` webhooks.

Every event produced here is simulated. Nothing is sent to or received from Stripe.
Signing follows Stripe's documented scheme but is written independently of
`integrationkit.signature`, so a bug in one cannot hide in both. Standard library only.

Examples:
    python tools/simulate.py --print
    python tools/simulate.py --tamper-body --send http://127.0.0.1:8000/webhooks/stripe --expect 400
"""

import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Copied from the example in Stripe's Event object docs (docs.stripe.com/api/events/object).
# It is not an API version this project was tested against. Nothing may branch on it.
FAKE_API_VERSION = "2019-02-19"

EVENT_TYPE = "checkout.session.completed"
SIGNATURE_HEADER = "Stripe-Signature"
# Fake secrets for the broken variants. They must never be printed, like the real one.
WRONG_SECRET = "whsec_test_fake_wrong_secret_used_by_simulator"
PREVIOUS_SECRET = "whsec_test_fake_previous_secret_used_by_simulator"
V0_FAKE_KEY = b"fake_v0_key_used_by_simulator"
# Far beyond any sensible tolerance, so --expired fails whatever the server allows.
EXPIRED_AGE_SECONDS = 3600
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

EXIT_OK = 0
EXIT_UNEXPECTED_STATUS = 1
EXIT_USAGE = 2
EXIT_CONNECTION_ERROR = 3

VARIANTS = (
    "tamper_body",
    "wrong_secret",
    "expired",
    "duplicate",
    "rolled_secret",
    "missing_header",
    "malformed_header",
)


@dataclass(frozen=True)
class SimulatedRequest:
    label: str
    headers: dict[str, str]
    body: bytes


class SimulatorError(Exception):
    """A usage or configuration problem; the message never contains a secret."""


# --- event construction -------------------------------------------------------------


def _fake_id(rng: random.Random, prefix: str) -> str:
    return f"{prefix}{rng.getrandbits(96):024x}"


def build_event(rng: random.Random, created: int, payment_status: str = "paid") -> dict:
    """A documented subset of a real Event wrapping a Checkout Session. All values fake."""
    buyer = rng.randint(1, 9999)
    amount = rng.randint(500, 50_000)  # smallest currency unit, e.g. cents for usd
    session = {
        "id": _fake_id(rng, "cs_test_"),
        "object": "checkout.session",
        "amount_subtotal": amount,
        "amount_total": amount,
        "client_reference_id": f"order_test_{buyer}",
        "created": created,
        "currency": "usd",
        "customer": None,
        "customer_details": {
            "address": None,
            "email": f"buyer{buyer}@example.com",
            "name": f"Test Buyer {buyer}",
            "phone": None,
            "tax_exempt": "none",
            "tax_ids": [],
        },
        "customer_email": None,
        "expires_at": created + 86_400,
        "livemode": False,
        "metadata": {"simulated": "true"},
        "mode": "payment",
        "payment_intent": _fake_id(rng, "pi_test_"),
        "payment_method_types": ["card"],
        "payment_status": payment_status,
        "status": "complete",
        "success_url": "https://example.com/success",
        "url": None,
    }
    return {
        "id": _fake_id(rng, "evt_test_"),
        "object": "event",
        "api_version": FAKE_API_VERSION,
        "created": created,
        "data": {"object": session},
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "type": EVENT_TYPE,
    }


def serialize(event: dict) -> bytes:
    """Our own formatting, not Stripe's. Verification only cares about the exact bytes."""
    return json.dumps(event, indent=2).encode("utf-8")


# --- signing ------------------------------------------------------------------------


def sign(timestamp: int, body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of `<timestamp>.<body>` keyed with the secret."""
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_header(timestamp: int, v1: list[str], v0: str | None = None) -> str:
    parts = [f"t={timestamp}", *(f"v1={s}" for s in v1)]
    if v0 is not None:
        parts.append(f"v0={v0}")
    return ",".join(parts)


def _fake_v0(timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(V0_FAKE_KEY, message, hashlib.sha256).hexdigest()


def build_requests(
    *,
    secret: str,
    now: int,
    rng: random.Random,
    variant: str | None = None,
    payment_status: str = "paid",
    with_v0: bool = False,
) -> list[SimulatedRequest]:
    """Build the request(s) for one simulated delivery, valid or deliberately broken."""
    if variant is not None and variant not in VARIANTS:
        raise SimulatorError(f"unknown variant: {variant}")

    event = build_event(rng, created=now, payment_status=payment_status)
    body = serialize(event)

    def headers_for(t: int, v1: list[str], b: bytes) -> dict[str, str]:
        v0 = _fake_v0(t, b) if with_v0 else None
        return {
            "Content-Type": "application/json; charset=utf-8",
            SIGNATURE_HEADER: signature_header(t, v1, v0),
        }

    if variant is None:
        return [SimulatedRequest("valid", headers_for(now, [sign(now, body, secret)], body), body)]

    if variant == "tamper_body":
        headers = headers_for(now, [sign(now, body, secret)], body)
        event["data"]["object"]["amount_total"] += 100  # changed after signing
        return [SimulatedRequest("tamper_body", headers, serialize(event))]

    if variant == "wrong_secret":
        headers = headers_for(now, [sign(now, body, WRONG_SECRET)], body)
        return [SimulatedRequest("wrong_secret", headers, body)]

    if variant == "expired":
        t = now - EXPIRED_AGE_SECONDS
        return [SimulatedRequest("expired", headers_for(t, [sign(t, body, secret)], body), body)]

    if variant == "duplicate":
        # Same event, delivered twice; each delivery gets a fresh timestamp and signature,
        # as Stripe does for retries.
        first_t = now - 60
        return [
            SimulatedRequest(
                "duplicate_1", headers_for(first_t, [sign(first_t, body, secret)], body), body
            ),
            SimulatedRequest(
                "duplicate_2", headers_for(now, [sign(now, body, secret)], body), body
            ),
        ]

    if variant == "rolled_secret":
        # One v1 per active secret; the current one is last so a verifier must check all.
        v1 = [sign(now, body, PREVIOUS_SECRET), sign(now, body, secret)]
        return [SimulatedRequest("rolled_secret", headers_for(now, v1, body), body)]

    if variant == "missing_header":
        return [SimulatedRequest("missing_header", {"Content-Type": "application/json"}, body)]

    # malformed_header: a valid signature but an unusable timestamp.
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        SIGNATURE_HEADER: f"t=not-a-timestamp,v1={sign(now, body, secret)}",
    }
    return [SimulatedRequest("malformed_header", headers, body)]


# --- secret loading -----------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_secret(env_file: Path) -> str:
    """WEBHOOK_SECRET from the environment, else from the .env file. No CLI option."""
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret and env_file.is_file():
        secret = _read_env_file(env_file).get("WEBHOOK_SECRET")
    if not secret:
        raise SimulatorError(f"WEBHOOK_SECRET not set in the environment or in {env_file}")
    return secret


# --- sending ------------------------------------------------------------------------


def check_target(url: str, allow_remote: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SimulatorError("--send URL must use http or https")
    if not allow_remote and parts.hostname not in LOCAL_HOSTS:
        raise SimulatorError(
            f"refusing to send to non-local host {parts.hostname!r}; pass --allow-remote"
        )


def send(request: SimulatedRequest, url: str, timeout: float) -> int:
    """POST the request and return the HTTP status. Raises OSError on connection failure."""
    http_request = urllib.request.Request(
        url,
        data=request.body,
        headers={**request.headers, "User-Agent": "IntegrationKit-Simulator/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a status code
        return exc.code


def format_request(request: SimulatedRequest) -> str:
    lines = [f"### {request.label}", "POST /webhooks/stripe"]
    lines += [f"{name}: {value}" for name, value in request.headers.items()]
    lines += ["", request.body.decode("utf-8", errors="replace"), ""]
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Produce clearly fake, Stripe-style signed checkout.session.completed "
        "webhooks. All events are simulated."
    )
    variant = parser.add_mutually_exclusive_group()
    variant.add_argument("--tamper-body", action="store_true", help="change body after signing")
    variant.add_argument("--wrong-secret", action="store_true", help="sign with another secret")
    variant.add_argument(
        "--expired", action="store_true", help=f"timestamp {EXPIRED_AGE_SECONDS}s in the past"
    )
    variant.add_argument("--duplicate", action="store_true", help="same event delivered twice")
    variant.add_argument("--rolled-secret", action="store_true", help="two v1 signatures")
    variant.add_argument("--missing-header", action="store_true", help="no Stripe-Signature")
    variant.add_argument("--malformed-header", action="store_true", help="unusable header")

    parser.add_argument("--with-v0", action="store_true", help="add a fake v0 signature")
    parser.add_argument(
        "--payment-status", choices=["paid", "unpaid", "no_payment_required"], default="paid"
    )
    parser.add_argument("--seed", type=int, help="seed for deterministic ids and amounts")
    parser.add_argument("--now", type=int, help="unix time to use instead of the clock")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--print", action="store_true", help="print request(s) (default)")
    output.add_argument("--out", type=Path, help="write the exact body bytes to a file")
    output.add_argument("--send", metavar="URL", help="POST to URL (localhost only by default)")

    parser.add_argument("--allow-remote", action="store_true", help="allow non-local --send")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds, for --send")
    parser.add_argument("--expect", type=int, metavar="STATUS", help="exit 1 on other status")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    chosen = [name for name in VARIANTS if getattr(args, name)]
    try:
        if args.send:
            check_target(args.send, args.allow_remote)
        secret = load_secret(args.env_file)
        requests = build_requests(
            secret=secret,
            now=args.now if args.now is not None else int(time.time()),
            rng=random.Random(args.seed),
            variant=chosen[0] if chosen else None,
            payment_status=args.payment_status,
            with_v0=args.with_v0,
        )
    except SimulatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.out:
        args.out.write_bytes(requests[0].body)  # duplicate deliveries share one body
        print(f"wrote {len(requests[0].body)} bytes to {args.out}")
        return EXIT_OK

    if not args.send:
        for request in requests:
            print(format_request(request))
        return EXIT_OK

    exit_code = EXIT_OK
    for request in requests:
        try:
            status = send(request, args.send, args.timeout)
        except OSError as exc:  # connection refused, DNS failure, timeout
            print(f"{request.label}: connection error: {exc}", file=sys.stderr)
            return EXIT_CONNECTION_ERROR
        print(f"{request.label}: {status}")
        if args.expect is not None and status != args.expect:
            print(f"{request.label}: expected {args.expect}, got {status}", file=sys.stderr)
            exit_code = EXIT_UNEXPECTED_STATUS
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
