"""Factory of the /api/v1 sub-app. Phase 2 routers are registered in ``_ROUTER_MODULES`` as they land."""
from importlib import import_module

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html

from ..version import APP_VERSION
from .auth import require_access
from .errors import install_error_handlers
from .gate import RemoteGateMiddleware

#: Router modules (relative to this package), each exposing ``router``.
_ROUTER_MODULES = ["system", "maintenance", "accounts", "transactions", "orders", "experts", "analysis", "risk_manager", "activity"]


def create_v1_app() -> FastAPI:
    # One app-wide dependency: GET/HEAD = read rule, other methods = write rule. A router that forgets to
    # declare auth is still guarded. /docs and /openapi.json are declared by hand (FastAPI's built-in
    # doc routes cannot carry dependencies) so they sit under the same read rule.
    app = FastAPI(title="BA2 Trade Platform management API", version=APP_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(require_access)])
    install_error_handlers(app)
    app.add_middleware(RemoteGateMiddleware)

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json():
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    def docs(request: Request):
        return get_swagger_ui_html(openapi_url=request.scope["root_path"] + "/openapi.json",
                                   title=f"{app.title} - docs")

    for name in _ROUTER_MODULES:
        app.include_router(import_module(f"{__package__}.{name}").router)
    return app


def register_management_api(host_app: FastAPI) -> None:
    """Attach the management API to the NiceGUI/FastAPI host app: the legacy ``/api/*`` aliases (routes) and
    the ``/api/v1`` sub-app (mount). Called once from ``ui/main.py``."""
    from ..ui import api_routes
    # Before routing, for /api and everything below it (the v1 mount and the legacy aliases): remote callers
    # get 403 remote_disabled for 404/405/307 paths too. Must be added at import time, before the app starts.
    host_app.add_middleware(RemoteGateMiddleware, prefix="/api")
    host_app.include_router(api_routes.router)
    host_app.mount("/api/v1", create_v1_app())
