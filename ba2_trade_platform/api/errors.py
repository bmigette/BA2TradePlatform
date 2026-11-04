"""JSON error envelope of /api/v1: ``{"error": code, "message": ..., "detail": {...}}``.

Installed on the v1 sub-app only; the NiceGUI app and the legacy ``/api/*`` aliases keep FastAPI's default
``{"detail": ...}`` bodies."""
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..logger import logger
from ..services.errors import ServiceError

_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 422: "invalid"}


def envelope(error: str, message: str, detail: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"error": error, "message": message, "detail": {} if detail is None else detail}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code,
                            content=envelope(exc.code, exc.message, exc.detail))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:   # an HTTPException that names its own code
            content = envelope(exc.detail["error"], exc.detail["message"])
        else:
            code = _HTTP_CODES[exc.status_code] if exc.status_code in _HTTP_CODES else "http_error"
            content = envelope(code, str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=envelope(
            "invalid", "Request validation failed", {"errors": jsonable_encoder(exc.errors())}))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.error(f"API {request.method} {request.url.path} crashed: {exc}", exc_info=True)
        return JSONResponse(status_code=500,
                            content=envelope("internal", "Internal error (see the platform log)"))
