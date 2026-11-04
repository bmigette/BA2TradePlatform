"""Paging query parameters shared by every list endpoint."""
from fastapi import Depends, FastAPI

from ba2_trade_platform.api.common import Paging, paging
from ba2_trade_platform.api.errors import install_error_handlers
from tests.api_helpers import FakeAccount, client_for, v1_client
import pytest


pytestmark = pytest.mark.api_db


def _app():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/l")
    def l(p: Paging = Depends(paging)):
        return {"limit": p.limit, "offset": p.offset}

    return app


def test_paging_defaults_and_bounds():
    c = client_for(_app())
    assert c.get("/l").json() == {"limit": 50, "offset": 0}
    assert c.get("/l?limit=500&offset=10").json() == {"limit": 500, "offset": 10}
    assert c.get("/l?limit=501").status_code == 422
    assert c.get("/l?limit=0").status_code == 422
    assert c.get("/l?offset=-1").status_code == 422


def test_phase2_fixtures_work():
    assert FakeAccount(3).get_account_snapshot().equity == 10000.0
    assert v1_client().get("/ping").status_code == 200
