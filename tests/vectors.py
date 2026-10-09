"""Shared loading of tests/fixtures/signature_vectors.json."""

import base64
import json
from pathlib import Path

from integrationkit.signature import SignatureError, verify_signature

VECTORS_PATH = Path(__file__).parent / "fixtures" / "signature_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_bytes())
SECRET = VECTORS["secret"]
CASES = VECTORS["cases"]


def body_of(case: dict) -> bytes:
    return base64.b64decode(case["body_b64"])


def our_outcome(case: dict) -> str:
    try:
        verify_signature(
            body_of(case),
            case["header"],
            SECRET,
            now=case["now"],
            tolerance=case["tolerance"],
        )
    except SignatureError as exc:
        return type(exc).__name__
    return "valid"
