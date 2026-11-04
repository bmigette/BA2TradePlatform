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
from .auth import SAFE_METHODS, require_access

_MAX_ARGUMENT_CHARS = 500
#: Largest request body an audited (write) route will read. Anything bigger is refused with 413.
MAX_BODY_BYTES = 64 * 1024


def _arguments(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        text = json.dumps(json.loads(text), sort_keys=True) if text.strip() else ""
    except ValueError:
        pass
    return text[:_MAX_ARGUMENT_CHARS]


def audit_write(request: Request, body: bytes, status: int) -> None:
    client = request.client.host if request.client is not None else "unknown"
    tail = request.state.token_tail if hasattr(request.state, "token_tail") else None
    where = f"{request.method} {request.url.path}"
    if tail is None:     # refused by auth (or the dependency never ran): never an authenticated write
        logger.warning(f"API write refused: {where} from {client} -> {status}")
        return
    arguments = _arguments(body)
    logger.info(f"API write: {where} from {client} token=...{tail} args={arguments} -> {status}")
    log_activity(
        ActivityLogSeverity.SUCCESS if status < 400 else ActivityLogSeverity.WARNING,
        ActivityLogType.APPLICATION_STATUS_CHANGE,
        f"API {where} -> {status}",
        data={"source": "api", "method": request.method, "path": request.url.path, "status": status,
              "client": client, "token_tail": tail, "arguments": arguments},
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
            try:
                await run_in_threadpool(require_access, request)
                body = await _read_capped_body(request)
                response = await original(request)
            except Exception as exc:
                audit_write(request, body, _status_of(exc))
                raise
            audit_write(request, body, response.status_code)
            return response

        return handler
