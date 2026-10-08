"""HTTP API: the webhook endpoint.

Run with: uvicorn integrationkit.app:create_app --factory
"""

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from integrationkit.db import is_database_unavailable, make_engine
from integrationkit.events import InvalidEventError, parse_event
from integrationkit.settings import Settings
from integrationkit.signature import SignatureError, verify_signature
from integrationkit.store import store_event

log = logging.getLogger("integrationkit.webhooks")

# Response bodies are deliberately generic: they never say which check failed.
MESSAGES = {
    400: "invalid request",
    413: "payload too large",
    422: "unprocessable event",
    500: "internal error",
    503: "temporarily unavailable",
}


def error(status: int) -> JSONResponse:
    return JSONResponse({"error": MESSAGES[status]}, status_code=status)


async def read_body_limited(request: Request, limit: int) -> bytes | None:
    """The raw body, or None if it exceeds `limit` bytes. Does not trust Content-Length."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    settings = settings or Settings()
    engine = engine or make_engine(settings.database_url)
    secret = settings.webhook_secret.get_secret_value()

    app = FastAPI(title="IntegrationKit", docs_url=None, redoc_url=None, openapi_url=None)

    async def handle(request: Request) -> JSONResponse:
        raw_body = await read_body_limited(request, settings.max_body_bytes)
        if raw_body is None:
            log.info("rejected webhook: body over %d bytes", settings.max_body_bytes)
            return error(413)

        try:
            signature_ts = verify_signature(
                raw_body,
                request.headers.get("stripe-signature"),
                secret,
                now=int(clock()),
                tolerance=settings.signature_tolerance_seconds,
            )
        except SignatureError as exc:
            log.info("rejected webhook: %s", type(exc).__name__)
            return error(400)

        try:
            event = parse_event(raw_body)
        except InvalidEventError as exc:
            log.info("rejected verified webhook: %s", exc)
            return error(400)

        if event.livemode:
            # Only the id and type: never the payload or any customer field.
            log.warning(
                "refused livemode event %s of type %s: this service is test mode only",
                event.event_id,
                event.event_type,
            )
            return error(422)

        try:
            inserted = await run_in_threadpool(store_event, engine, event, raw_body, signature_ts)
        except SQLAlchemyError as exc:
            if not is_database_unavailable(exc):
                raise
            log.warning("database unavailable while storing event %s", event.event_id)
            return error(503)

        if event.payload_error:
            log.warning("stored event %s without payload: %s", event.event_id, event.payload_error)
        log.info(
            "%s event %s of type %s",
            "stored" if inserted else "duplicate",
            event.event_id,
            event.event_type,
        )
        return JSONResponse({"received": True})

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> JSONResponse:
        try:
            return await handle(request)
        except Exception:
            log.exception("unexpected error while handling a webhook")
            return error(500)

    return app
