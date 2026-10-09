"""Parsing and validation of a verified webhook body. Pure functions, no I/O.

Rules (docs/plan.md):
- Any event with a well-formed `id` and `type` is accepted, whatever its type.
- `session_id` is set only for `checkout.session.*` events whose `data.object.id` looks
  like a Checkout Session id; otherwise it is None.
- If PostgreSQL's jsonb cannot hold the parsed payload, `payload` is None and
  `payload_error` says why. The raw body is always kept separately.

Exception messages are fixed strings and never contain payload content.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any

EVENT_ID_RE = re.compile(r"evt_[A-Za-z0-9_]{1,250}")
EVENT_TYPE_RE = re.compile(r"[a-z0-9_]{1,100}(\.[a-z0-9_]{1,100}){1,10}")
SESSION_ID_RE = re.compile(r"cs_[A-Za-z0-9_]{1,250}")
CHECKOUT_SESSION_PREFIX = "checkout.session."

NUL_ERROR = "payload contains U+0000, which PostgreSQL jsonb cannot store"
SURROGATE_ERROR = "payload contains an unpaired UTF-16 surrogate, which jsonb cannot store"
NUMBER_ERROR = "payload contains a number too large to parse as a float"


class InvalidEventError(Exception):
    """The verified body is not an event we can identify. Maps to 400."""


@dataclass(frozen=True)
class ParsedEvent:
    event_id: str
    event_type: str
    livemode: bool
    session_id: str | None
    payload: dict[str, Any] | None
    payload_error: str | None

    @property
    def missing_session_id(self) -> bool:
        """A checkout.session.* event whose data.object.id was missing or malformed."""
        return self.event_type.startswith(CHECKOUT_SESSION_PREFIX) and self.session_id is None


def _reject_constant(name: str) -> None:
    raise ValueError("NaN and Infinity are not valid JSON")


def parse_event(raw_body: bytes) -> ParsedEvent:
    try:
        data = json.loads(raw_body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise InvalidEventError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidEventError("body is not a JSON object")

    event_id = data.get("id")
    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
        raise InvalidEventError("missing or malformed event id")
    event_type = data.get("type")
    if not isinstance(event_type, str) or not EVENT_TYPE_RE.fullmatch(event_type):
        raise InvalidEventError("missing or malformed event type")
    livemode = data.get("livemode")
    if not isinstance(livemode, bool):
        raise InvalidEventError("missing or malformed livemode")

    problem = jsonb_problem(data)
    return ParsedEvent(
        event_id=event_id,
        event_type=event_type,
        livemode=livemode,
        session_id=_session_id(data, event_type),
        payload=None if problem else data,
        payload_error=problem,
    )


def _session_id(data: dict[str, Any], event_type: str) -> str | None:
    if not event_type.startswith(CHECKOUT_SESSION_PREFIX):
        return None
    obj = data.get("data")
    obj = obj.get("object") if isinstance(obj, dict) else None
    candidate = obj.get("id") if isinstance(obj, dict) else None
    if isinstance(candidate, str) and SESSION_ID_RE.fullmatch(candidate):
        return candidate
    return None


def _has_surrogate(text: str) -> bool:
    # json.loads joins valid surrogate pairs, so any surrogate left is unpaired.
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def jsonb_problem(value: Any) -> str | None:
    """Why PostgreSQL jsonb would reject this parsed JSON value, or None if it would not."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if "\x00" in item:
                return NUL_ERROR
            if _has_surrogate(item):
                return SURROGATE_ERROR
        elif isinstance(item, float) and not math.isfinite(item):
            return NUMBER_ERROR
    return None
