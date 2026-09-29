"""The classic risk manager's REAL sizing loop, with ``allow_fractional_shares``.

The helper-level tests live beside the grid in ``packages/common``. These drive the whole
``_calculate_order_quantities`` pass -- the code that actually decides an order's quantity --
because the grid only matters if every branch of that loop honours it: the early
affordability skip, the notional path's two roundings, the min-one-share bump, the
risk-based path, and the round-lot rule.
"""
import logging

import pytest

from ba2_common.core import TradeRiskManagement as trm
from ba2_common.core.types import OrderDirection


class _Rec:
    def __init__(self, profit=10.0, confidence=80.0):
        self.expected_profit_percent = profit
        self.confidence = confidence


class _Order:
    def __init__(self, symbol, data=None):
        self.id = None
        self.symbol = symbol
        self.side = OrderDirection.BUY
        self.quantity = None
        self.stop_price = None
        self.data = data or {}


class _Expert:
    def __init__(self, settings=None, instruments=None, equity=100_000.0):
        self._settings = dict(settings or {})
        self._instruments = dict(instruments or {})
        self._equity = equity

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._settings.get(name)

    def _get_enabled_instruments_config(self):
        return self._instruments

    def get_virtual_balance(self):
        return self._equity


class _Account:
    def __init__(self, prices, fractionable=None):
        self._prices = dict(prices)
        self._fractionable = dict(fractionable or {})
        self.fractionable_calls = []

    def get_instrument_current_price(self, symbols):
        return {s: self._prices[s] for s in symbols if s in self._prices}

    def get_fractionable(self, symbols):
        self.fractionable_calls.append(list(symbols))
        return {s: self._fractionable.get(s) for s in symbols}


def _size(symbols, *, prices, balance, cap, settings=None, fractionable=None, data=None,
          instruments=None):
    mgr = object.__new__(trm.TradeRiskManagement)
    mgr.logger = logging.getLogger("test")
    mgr.indicator_provider = None
    mgr.as_of = None
    account = _Account(prices, fractionable)
    pairs = [(_Order(s, data=(data or {}).get(s)), _Rec()) for s in symbols]
    context = {}
    mgr._calculate_order_quantities(
        pairs, balance, cap, {}, account,
        _Expert(settings=settings, instruments=instruments),
        traces={}, context=context)
    return {o.symbol: o.quantity for o, _ in pairs}, account, context


ON = {"allow_fractional_shares": True}


# ---------------------------------------------------------------------------
# Off (the default): exactly as before
# ---------------------------------------------------------------------------

def test_not_opted_in_sizes_whole_shares_and_asks_the_broker_nothing():
    qty, account, context = _size(["AAA"], prices={"AAA": 333.0}, balance=100_000.0,
                                  cap=1_000.0, fractionable={"AAA": True})

    assert qty["AAA"] == 3
    assert isinstance(qty["AAA"], int)
    assert account.fractionable_calls == []
    assert context["allow_fractional_shares"] is False


# ---------------------------------------------------------------------------
# On: IGNORED, because this risk manager always arms a protective stop
# ---------------------------------------------------------------------------
#
# A broker cannot carry a fractional protective order (no fractional OCO on Alpaca, and a
# fractional stop is DAY-only), so an opted-in expert is warned and sized exactly as if it
# had not opted in. See share_grid "NO FRACTIONS UNDER PROTECTIVE ORDERS".

def test_opted_in_is_ignored_with_a_warning_and_sizes_whole_shares(caplog):
    with caplog.at_level(logging.WARNING, logger="test"):
        qty, account, context = _size(["AAA"], prices={"AAA": 333.0}, balance=100_000.0,
                                      cap=1_000.0, settings=ON, fractionable={"AAA": True})

    assert qty["AAA"] == 3
    assert isinstance(qty["AAA"], int)
    assert account.fractionable_calls == []
    assert context["allow_fractional_shares"] is False
    assert context["allow_fractional_shares_ignored"] is True
    assert any("allow_fractional_shares is ignored" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("price,cap,expected", [
    (2_000.0, 1_000.0, 0),      # dearer than the cap: skipped, not bought fractionally
    (100.0, 1_000.0, 10),
])
def test_opted_in_matches_not_opted_in_quantity_for_quantity(price, cap, expected):
    on, _, _ = _size(["AAA"], prices={"AAA": price}, balance=100_000.0, cap=cap,
                     settings=ON, fractionable={"AAA": True})
    off, _, _ = _size(["AAA"], prices={"AAA": price}, balance=100_000.0, cap=cap)

    assert on == off == {"AAA": expected}


def test_a_round_lot_is_unchanged():
    qty, _, _ = _size(["AAA"], prices={"AAA": 10.0}, balance=100_000.0, cap=5_000.0,
                      settings=ON, fractionable={"AAA": True},
                      data={"AAA": {"lot_size": 100}})

    assert qty["AAA"] == 500
