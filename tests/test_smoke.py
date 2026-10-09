"""The smoke script's case table, checked offline (no server, no database)."""

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest
import simulate
import smoke

from integrationkit.events import parse_event
from integrationkit.signature import SignatureError, verify_signature

ROOT = Path(__file__).resolve().parents[1]
SECRET = "whsec_test_fake_smoke_secret"
NOW = 1_790_000_000


def offline_status(request) -> int:
    """What our verifier and parser decide, mapped to the endpoint's status code."""
    try:
        verify_signature(
            request.body,
            request.headers.get("Stripe-Signature"),
            SECRET,
            now=NOW,
            tolerance=300,
        )
    except SignatureError:
        return 400
    parse_event(request.body)
    return 200


@pytest.mark.parametrize("case", smoke.CASES, ids=lambda c: c.label)
def test_expected_status_agrees_with_verifier(case):
    for request in smoke.rebuild(case, SECRET, seed=5, now=NOW):
        assert offline_status(request) == case.expected


@pytest.mark.parametrize("case", smoke.CASES, ids=lambda c: c.label)
def test_cli_flags_and_rebuild_produce_identical_requests(case, monkeypatch, capsys):
    """The CLI (as smoke calls it) and smoke.rebuild must build byte-identical requests."""
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    args = [*case.flags, "--print", "--seed", "5", "--now", str(NOW)]
    assert simulate.main(args) == 0
    printed = capsys.readouterr().out
    for request in smoke.rebuild(case, SECRET, seed=5, now=NOW):
        assert request.body.decode() in printed
        if "Stripe-Signature" in request.headers:
            assert request.headers["Stripe-Signature"] in printed


def test_every_simulator_variant_is_covered():
    covered = {case.variant for case in smoke.CASES if case.variant}
    assert covered == set(simulate.VARIANTS)


def test_case_labels_unique_and_event_ids_distinct_per_seed():
    labels = [case.label for case in smoke.CASES]
    assert len(labels) == len(set(labels))
    ids = {
        json.loads(r.body)["id"]
        for case in smoke.CASES
        for r in smoke.rebuild(case, SECRET, seed=random.randrange(2**32), now=NOW)
    }
    assert len(ids) >= len(smoke.CASES) - 1  # duplicate shares one id across 2 requests


def test_uvicorn_log_config_routes_integrationkit_logs_to_stdout(tmp_path):
    """Load docker/uvicorn-logging.json in a subprocess and log one line through it."""
    script = (
        "import json, logging, logging.config\n"
        "logging.config.dictConfig(json.load(open('docker/uvicorn-logging.json')))\n"
        "logging.getLogger('integrationkit.webhooks').info('smoke-log-check')\n"
        "logging.getLogger('integrationkit.webhooks').debug('hidden-debug')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    assert "integrationkit.webhooks: smoke-log-check" in result.stdout
    assert "hidden-debug" not in result.stdout
