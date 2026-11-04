"""Pure ASGI middleware that applies the remote-access gate BEFORE routing.

A dependency only runs for a matched route, so a remote caller could still tell 404 (no such path), 405 (wrong
method) and 307 (trailing-slash redirect) apart. This middleware answers every remote request to the covered
paths with 403 ``remote_disabled`` while ``api_allow_remote`` is off. The token logic stays in
``auth.require_access``."""
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .auth import REMOTE_DISABLED_MESSAGE, get_allow_remote, is_remote_scope
from .errors import envelope


class RemoteGateMiddleware:
    def __init__(self, app, prefix: str | None = None):
        """``prefix`` limits the gate to paths equal to it or below it (``None`` = every path)."""
        self.app = app
        self.prefix = prefix

    def _covered(self, scope) -> bool:
        if self.prefix is None:
            return True
        path = scope["path"]
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self._covered(scope) and is_remote_scope(scope) \
                and not await run_in_threadpool(get_allow_remote):
            if scope["type"] == "http":
                await JSONResponse(status_code=403, content=envelope(
                    "remote_disabled", REMOTE_DISABLED_MESSAGE))(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)
