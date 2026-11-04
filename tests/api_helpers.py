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
    # base_url localhost: the token-free local read also requires a loopback Host header (DNS-rebinding guard),
    # and TestClient's default Host is "testserver".
    return TestClient(app, base_url="http://localhost", client=(host, 50000),
                      headers=auth_headers(token) if token else {})


from ba2_trade_platform.core.account_types import AccountSnapshot


class FakeAccount:
    """Stands in for a broker account in service/route tests: canned snapshot, capital, positions, orders.
    Every figure is a plain attribute so a test can blank one (None) to exercise the Unmeasurable path."""

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.snapshot = AccountSnapshot(cash=2500.0, equity=10000.0, net_liquidation=10000.0,
                                        buying_power=20000.0, option_buying_power=5000.0,
                                        margin_multiplier=2.0, is_margin_account=True,
                                        long_market_value=7500.0, short_market_value=0.0)
        self.capital = {"account_id": account_id, "balance": 10000.0, "margin_enabled": True,
                        "margin_factor": 1.5, "broker_multiplier": 2.0, "effective_factor": 1.5,
                        "tradable_balance": 15000.0, "broker_buying_power": 20000.0,
                        "gross_exposure": 7500.0, "pending_entries": 500.0, "headroom": 7000.0}
        self.positions = [{"symbol": "AAPL", "qty": 10.0}]
        self.orders = [{"id": "b-1", "symbol": "AAPL", "status": "new"}]
        self.capital_error = None

    def get_account_snapshot(self):
        return self.snapshot

    def describe_capital(self):
        if self.capital_error is not None:
            raise ValueError(self.capital_error)
        return self.capital

    def get_positions(self):
        return self.positions

    def get_orders(self, status=None):
        return self.orders

    def _pending_stock_entry_notional(self, exclude_order_id=None):
        return 500.0


def v1_client(**kwargs) -> TestClient:
    from ba2_trade_platform.api.app import create_v1_app
    return client_for(create_v1_app(), **kwargs)
