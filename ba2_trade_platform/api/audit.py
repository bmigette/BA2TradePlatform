"""Audit trail for management-API writes (design section 1, "Audit")."""
import json
from typing import Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from ..core.db import log_activity
from ..core.types import ActivityLogSeverity, ActivityLogType
from ..logger import logger
from .auth import SAFE_METHODS

_MAX_ARGUMENT_CHARS = 500


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


class AuditedRoute(APIRoute):
    """Route class for routers that hold writes: every non-GET/HEAD request is audited, whatever its outcome."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.method in SAFE_METHODS:
                return await original(request)
            body = await request.body()
            try:
                response = await original(request)
            except Exception as exc:
                status = exc.status_code if hasattr(exc, "status_code") else 500
                audit_write(request, body, status)
                raise
            audit_write(request, body, response.status_code)
            return response

        return handler
