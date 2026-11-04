"""Helpers for the management-API tests: token setup and TestClients with a chosen peer address."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AppSetting

TOKEN = "t" * 40 + "abcd"


def set_api_token(token: str = TOKEN) -> None:
    add_instance(AppSetting(key="api_token", value_str=token), expunge_after_flush=True)


def set_allow_remote(on: bool = True) -> None:
    from ba2_trade_platform.api.auth import set_allow_remote as _set
    _set(on)


def auth_headers(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def client_for(app: FastAPI, *, host: str = "127.0.0.1", token: str | None = None) -> TestClient:
    """TestClient whose peer address is ``host`` (the auth rule reads the socket peer) and which sends
    ``Authorization: Bearer <token>`` when ``token`` is given."""
    return TestClient(app, client=(host, 50000), headers=auth_headers(token) if token else {})
