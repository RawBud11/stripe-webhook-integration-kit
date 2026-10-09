import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import simulate

from integrationkit.signature import (
    MalformedHeaderError,
    MissingHeaderError,
    SignatureMismatchError,
    TimestampOutsideToleranceError,
    parse_header,
    verify_signature,
)

SECRET = "whsec_test_fake_simulator_test_secret"
NOW = 1_790_000_000
TOLERANCE = 300
ALL_SECRETS = [SECRET, simulate.WRONG_SECRET, simulate.PREVIOUS_SECRET]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Use only the test secret; never read a developer's real .env."""
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.chdir(tmp_path)


def build(variant=None, **kwargs):
    return simulate.build_requests(
        secret=SECRET, now=NOW, rng=random.Random(7), variant=variant, **kwargs
    )


def verify(request):
    return verify_signature(
        request.body,
        request.headers.get("Stripe-Signature"),
        SECRET,
        now=NOW,
        tolerance=TOLERANCE,
    )


# --- event shape --------------------------------------------------------------------


def test_event_shape_and_fake_markers():
    (request,) = build()
    event = json.loads(request.body)
    session = event["data"]["object"]

    assert event["object"] == "event"
    assert event["type"] == "checkout.session.completed"
    assert event["api_version"] == simulate.FAKE_API_VERSION == "2019-02-19"
    assert event["livemode"] is False and session["livemode"] is False
    assert event["id"].startswith("evt_test_") and len(event["id"]) == len("evt_test_") + 24
    assert session["id"].startswith("cs_test_")
    assert session["payment_intent"].startswith("pi_test_")
    assert session["object"] == "checkout.session"
    assert session["status"] == "complete"
    assert session["payment_status"] == "paid"
    assert isinstance(session["amount_total"], int)
    assert session["currency"] == "usd"
    assert session["customer_details"]["email"].endswith("@example.com")
    assert session["expires_at"] == session["created"] + 86_400
    assert session["url"] is None
    assert session["metadata"] == {"simulated": "true"}


def test_payment_status_option():
    (request,) = build(payment_status="unpaid")
    assert json.loads(request.body)["data"]["object"]["payment_status"] == "unpaid"


def test_same_seed_gives_identical_bytes():
    assert build() == build()


def test_different_seeds_give_different_events():
    other = simulate.build_requests(secret=SECRET, now=NOW, rng=random.Random(8))
    assert json.loads(build()[0].body)["id"] != json.loads(other[0].body)["id"]


# --- variants against our verifier --------------------------------------------------


def test_valid_request_verifies():
    (request,) = build()
    assert verify(request) == NOW


def test_with_v0_still_verifies_and_adds_v0():
    (request,) = build(with_v0=True)
    assert ",v0=" in request.headers["Stripe-Signature"]
    assert verify(request) == NOW


@pytest.mark.parametrize(
    "variant, error",
    [
        ("tamper_body", SignatureMismatchError),
        ("wrong_secret", SignatureMismatchError),
        ("expired", TimestampOutsideToleranceError),
        ("missing_header", MissingHeaderError),
        ("malformed_header", MalformedHeaderError),
    ],
)
def test_broken_variants_fail_with_expected_error(variant, error):
    (request,) = build(variant)
    with pytest.raises(error):
        verify(request)


def test_tampered_body_differs_but_header_is_for_original():
    (valid,) = build()
    (tampered,) = build("tamper_body")
    assert tampered.body != valid.body
    assert tampered.headers == valid.headers


def test_rolled_secret_has_two_v1_and_verifies():
    (request,) = build("rolled_secret")
    assert len(parse_header(request.headers["Stripe-Signature"]).v1_signatures) == 2
    assert verify(request) == NOW


def test_duplicate_is_same_event_with_fresh_signatures():
    first, second = build("duplicate")
    assert first.body == second.body
    assert first.headers["Stripe-Signature"] != second.headers["Stripe-Signature"]
    assert verify(first) == NOW - 60
    assert verify(second) == NOW


def test_unknown_variant_rejected():
    with pytest.raises(simulate.SimulatorError):
        build("not_a_variant")


# --- secret handling ----------------------------------------------------------------

VARIANT_FLAGS = [
    [],
    ["--tamper-body"],
    ["--wrong-secret"],
    ["--expired"],
    ["--duplicate"],
    ["--rolled-secret"],
    ["--missing-header"],
    ["--malformed-header"],
    ["--with-v0"],
]


@pytest.mark.parametrize("flags", VARIANT_FLAGS)
def test_print_never_shows_any_secret(capsys, flags):
    assert simulate.main(["--print", "--seed", "1", "--now", str(NOW), *flags]) == 0
    captured = capsys.readouterr()
    assert "### " in captured.out
    for secret in ALL_SECRETS:
        assert secret not in captured.out
        assert secret not in captured.err


def test_secret_read_from_env_file(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("WEBHOOK_SECRET")
    env_file = tmp_path / "custom.env"
    env_file.write_text(f'# comment\nWEBHOOK_SECRET="{SECRET}"\n', encoding="utf-8")
    assert simulate.load_secret(env_file) == SECRET


def test_missing_secret_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("WEBHOOK_SECRET")
    assert simulate.main(["--print", "--env-file", "does-not-exist.env"]) == 2
    assert "WEBHOOK_SECRET not set" in capsys.readouterr().err


def test_out_writes_exact_body_bytes(tmp_path):
    out = tmp_path / "body.json"
    assert simulate.main(["--out", str(out), "--seed", "7", "--now", str(NOW)]) == 0
    assert out.read_bytes() == build()[0].body


# --- send target safety -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/webhooks/stripe",
        "http://127.0.0.1:8000/webhooks/stripe",
        "http://[::1]:8000/webhooks/stripe",
    ],
)
def test_local_targets_allowed(url):
    simulate.check_target(url, allow_remote=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/webhooks/stripe",
        "http://10.0.0.5:8000/webhooks/stripe",
        "http://localhost.example.com/webhooks/stripe",
        "http://127.0.0.2:8000/webhooks/stripe",
    ],
)
def test_remote_targets_refused_without_flag(url, capsys):
    with pytest.raises(simulate.SimulatorError):
        simulate.check_target(url, allow_remote=False)
    assert simulate.main(["--send", url]) == 2
    assert "--allow-remote" in capsys.readouterr().err


def test_remote_target_allowed_with_flag():
    simulate.check_target("https://example.com/webhooks/stripe", allow_remote=True)


@pytest.mark.parametrize("url", ["ftp://localhost/x", "file:///etc/passwd"])
def test_non_http_schemes_refused(url):
    with pytest.raises(simulate.SimulatorError):
        simulate.check_target(url, allow_remote=True)


# --- sending to a local test server -------------------------------------------------


class RecordingServer:
    """Tiny local HTTP server that records requests and replies with a fixed status."""

    def __init__(self, status=200, delay=0.0):
        self.received = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                outer.received.append((dict(self.headers), self.rfile.read(length)))
                time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/webhooks/stripe"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_send_posts_exact_bytes_and_header(capsys):
    with RecordingServer(status=200) as server:
        code = simulate.main(
            ["--send", server.url, "--seed", "7", "--now", str(NOW), "--expect", "200"]
        )
    assert code == 0
    (headers, body) = server.received[0]
    (expected,) = build()
    assert body == expected.body
    assert headers["Stripe-Signature"] == expected.headers["Stripe-Signature"]
    assert "valid: 200" in capsys.readouterr().out


def test_send_duplicate_sends_two_requests():
    with RecordingServer(status=200) as server:
        assert simulate.main(["--send", server.url, "--duplicate", "--expect", "200"]) == 0
    assert len(server.received) == 2
    assert server.received[0][1] == server.received[1][1]


def test_expect_mismatch_exits_1(capsys):
    with RecordingServer(status=400) as server:
        assert simulate.main(["--send", server.url, "--expect", "200"]) == 1
    captured = capsys.readouterr()
    assert "valid: 400" in captured.out
    assert "expected 200, got 400" in captured.err


def test_without_expect_any_status_exits_0():
    with RecordingServer(status=400) as server:
        assert simulate.main(["--send", server.url]) == 0


def test_connection_refused_exits_3(capsys):
    with RecordingServer() as server:
        url = server.url
    # Server is closed now, so the port refuses connections.
    assert simulate.main(["--send", url, "--timeout", "2"]) == 3
    assert "connection error" in capsys.readouterr().err


def test_timeout_exits_3(capsys):
    with RecordingServer(delay=1.0) as server:
        assert simulate.main(["--send", server.url, "--timeout", "0.2"]) == 3
    assert "connection error" in capsys.readouterr().err
