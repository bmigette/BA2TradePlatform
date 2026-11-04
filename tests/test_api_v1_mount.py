"""ui/main.py wires the API through register_management_api(app); test the wiring on a bare FastAPI."""
from fastapi import FastAPI

from ba2_trade_platform.api.app import register_management_api
from tests.api_helpers import client_for


def test_register_management_api_mounts_v1_and_legacy_aliases():
    app = FastAPI()
    register_management_api(app)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/reload" in paths and "/api/v1" in paths
    assert client_for(app).get("/api/v1/ping").status_code == 200
