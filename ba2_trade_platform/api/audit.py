"""Audit trail for management-API writes (design section 1, "Audit")."""
import json
from typing import Callable

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from fastapi.routing import APIRoute

from ..core.db import log_activity
from ..core.types import ActivityLogSeverity, ActivityLogType
from ..logger import logger
from ..services._scrub import REDACTED, scrub, scrub_string
from . import idempotency
from .auth import SAFE_METHODS, require_access

_MAX_ARGUMENT_CHARS = 500
#: Largest request body an audited (write) route will read. Anything bigger is refused with 413.
MAX_BODY_BYTES = 64 * 1024


def _arguments(body: bytes, secret_keys=()) -> str:
    """The request body as logged: parsed JSON with every secret redacted (``services._scrub.scrub``: by key name
    and by pattern), plus the top-level keys a route flagged as secret through its own knowledge of the target
    (``request.state.audit_secret_keys``: a setting whose NAME looks harmless but whose definition says secret)."""
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        return scrub_string(text)[:_MAX_ARGUMENT_CHARS]
    if parsed is None:
        return ""
    if isinstance(parsed, dict) and secret_keys:
        parsed = {k: (REDACTED if k in secret_keys else v) for k, v in parsed.items()}
    return json.dumps(scrub(parsed), sort_keys=True)[:_MAX_ARGUMENT_CHARS]


def audit_write(request: Request, body: bytes, status: int) -> None:
    client = request.client.host if request.client is not None else "unknown"
    tail = request.state.token_tail if hasattr(request.state, "token_tail") else None
    where = f"{request.method} {request.url.path}"
    if tail is None:     # refused by auth (or the dependency never ran): never an authenticated write
        logger.warning(f"API write refused: {where} from {client} -> {status}")
        return
    secret_keys = request.state.audit_secret_keys if hasattr(request.state, "audit_secret_keys") else ()
    arguments = _arguments(body, secret_keys)
    query = request.url.query
    logger.info(f"API write: {where} from {client} token=...{tail} args={arguments} query={query} -> {status}")
    log_activity(
        ActivityLogSeverity.SUCCESS if status < 400 else ActivityLogSeverity.WARNING,
        ActivityLogType.APPLICATION_STATUS_CHANGE,
        f"API {where} -> {status}",
        data={"source": "api", "method": request.method, "path": request.url.path, "status": status,
              "client": client, "token_tail": tail, "arguments": arguments, "query": query},
    )


async def _read_capped_body(request: Request) -> bytes:
    """Read the body, refusing (413) as soon as it exceeds MAX_BODY_BYTES. The bytes are cached on the request so
    FastAPI's own body parsing sees them (the stream can only be consumed once)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body larger than {MAX_BODY_BYTES} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail=f"Request body larger than {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body


def _status_of(exc: Exception) -> int:
    if isinstance(exc, RequestValidationError):
        return 422
    return exc.status_code if hasattr(exc, "status_code") else 500


def is_dry_run(request: Request) -> bool:
    return request.query_params.get("dry_run", "").lower() in ("1", "true", "yes", "on")


def _begin_idempotent(request: Request, body: bytes):
    """None when the request carries no usable Idempotency-Key (or is a dry run); a ``Response`` to return as the
    replay; otherwise the ``(scope, key)`` claimed in the store."""
    key = request.headers.get("idempotency-key")
    if key is None or is_dry_run(request):
        return None
    key = key.strip()
    if not key or len(key) > idempotency.MAX_KEY_CHARS:
        raise HTTPException(status_code=422, detail=f"Idempotency-Key must be 1-{idempotency.MAX_KEY_CHARS} characters")
    tail = request.state.token_tail if hasattr(request.state, "token_tail") else None
    scope = f"{tail}:{request.method} {request.url.path}"
    outcome, stored = idempotency.STORE.begin(scope, key, idempotency.fingerprint(body, request.url.query))
    if outcome == "run":
        return scope, key
    if outcome == "replay":
        return Response(content=stored.body, status_code=stored.status_code, media_type=stored.media_type,
                        headers={"Idempotent-Replay": "true"})
    if outcome == "busy":
        raise HTTPException(status_code=409, detail="A request with this Idempotency-Key is still running")
    raise HTTPException(status_code=422, detail="This Idempotency-Key was already used with a different request")


class AuditedRoute(APIRoute):
    """Route class for routers that hold writes: every non-GET/HEAD request is audited, whatever its outcome.

    Auth runs FIRST (``require_access`` is invoked here before anything is read, because FastAPI parses the body
    before it solves dependencies), so an unauthenticated caller can make us read no body at all; an
    authenticated one is capped at MAX_BODY_BYTES."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.method in SAFE_METHODS:
                return await original(request)
            body = b""
            claimed = None
            finished = False
            try:
                try:
                    await run_in_threadpool(require_access, request)
                    body = await _read_capped_body(request)
                    replay = _begin_idempotent(request, body)
                    if isinstance(replay, Response):
                        audit_write(request, body, replay.status_code)
                        return replay
                    claimed = replay
                    response = await original(request)
                except Exception as exc:
                    audit_write(request, body, _status_of(exc))
                    raise
                if claimed is not None and 200 <= response.status_code < 300:
                    idempotency.STORE.finish(*claimed, idempotency.Stored(
                        response.status_code, bytes(response.body), response.media_type))
                    finished = True
                audit_write(request, body, response.status_code)
                return response
            finally:
                # Whatever happened -- a failure, a non-2xx answer, or a cancellation (BaseException) -- a claim that
                # was not completed is dropped, so the key is never left "busy".
                if claimed is not None and not finished:
                    idempotency.STORE.abort(*claimed)

        return handler
