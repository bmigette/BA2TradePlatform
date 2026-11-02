"""AlpacaAccount.get_closing_fill: the broker FILL activities that closed a position."""
from datetime import datetime, timezone

import pytest

from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

SINCE = datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc)


def _fill(i, symbol, side, qty, price, t):
    return {"id": f"a{i}", "activity_type": "FILL", "symbol": symbol, "side": side,
            "qty": str(qty), "price": str(price), "transaction_time": t}


class _Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.pages.pop(0) if self.pages else []


def _account(pages, page_size=100):
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = _Client(pages)
    return acct


def test_partial_fills_are_averaged_and_the_last_fill_time_kept():
    acct = _account([[
        _fill(1, "AMPX", "buy", 2, 12.11, "2026-06-26T13:30:00Z"),
        _fill(2, "OTHER", "sell", 5, 1.0, "2026-06-29T18:15:00Z"),
        _fill(3, "AMPX", "sell", 1, 13.60, "2026-06-29T18:16:01Z"),
        _fill(4, "AMPX", "sell", 1, 13.70, "2026-06-29T18:16:02Z"),
    ]])
    price, when = acct.get_closing_fill("AMPX", OrderDirection.SELL, 2.0, SINCE)
    assert price == pytest.approx(13.65)
    assert when == datetime(2026, 6, 29, 18, 16, 2, tzinfo=timezone.utc)
    path, params = acct.client.calls[0]
    assert path == "/account/activities/FILL"
    assert params["after"] == SINCE.isoformat() and params["direction"] == "asc"


def test_a_short_is_closed_by_buys():
    acct = _account([[_fill(1, "XYZ", "sell_short", 3, 10.0, "2026-06-26T14:00:00Z"),
                      _fill(2, "XYZ", "buy", 3, 9.0, "2026-06-27T14:00:00Z")]])
    assert acct.get_closing_fill("XYZ", OrderDirection.BUY, 3.0, SINCE)[0] == pytest.approx(9.0)


def test_fills_that_never_cover_the_quantity_are_not_a_price():
    acct = _account([[_fill(1, "AMPX", "sell", 1, 13.6, "2026-06-29T18:16:01Z")]])
    assert acct.get_closing_fill("AMPX", OrderDirection.SELL, 2.0, SINCE) is None


def test_the_search_follows_page_tokens():
    first = [_fill(i, "OTHER", "buy", 1, 1.0, "2026-06-27T14:00:00Z") for i in range(100)]
    acct = _account([first, [_fill(200, "AMPX", "sell", 2, 13.0, "2026-06-29T18:16:01Z")]])
    assert acct.get_closing_fill("AMPX", OrderDirection.SELL, 2.0, SINCE)[0] == pytest.approx(13.0)
    assert acct.client.calls[1][1]["page_token"] == "a99"


def test_an_unreadable_history_is_none_not_a_guess():
    acct = _account([])

    def boom(*a, **k):
        raise RuntimeError("API down")
    acct.client.get = boom
    assert acct.get_closing_fill("AMPX", OrderDirection.SELL, 2.0, SINCE) is None
