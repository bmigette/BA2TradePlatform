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
# On
# ---------------------------------------------------------------------------

def test_opted_in_and_broker_true_buys_the_fraction():
    qty, _, _ = _size(["AAA"], prices={"AAA": 333.0}, balance=100_000.0, cap=1_000.0,
                      settings=ON, fractionable={"AAA": True})

    assert qty["AAA"] == pytest.approx(3.003)


def test_the_whole_batch_is_one_lookup():
    _, account, _ = _size(["AAA", "BBB", "CCC"],
                          prices={"AAA": 10.0, "BBB": 20.0, "CCC": 30.0},
                          balance=100_000.0, cap=1_000.0, settings=ON,
                          fractionable={"AAA": True, "BBB": True, "CCC": True})

    assert len(account.fractionable_calls) == 1


@pytest.mark.parametrize("flag", [False, None])
def test_broker_false_or_unknown_stays_whole(flag):
    qty, _, _ = _size(["AAA"], prices={"AAA": 333.0}, balance=100_000.0, cap=1_000.0,
                      settings=ON, fractionable={"AAA": flag})

    assert qty["AAA"] == 3


def test_a_share_dearer_than_the_cap_is_bought_fractionally_instead_of_skipped():
    """The early affordability skip used to mean "cannot afford ONE SHARE". On a fractional
    grid it means one grid step: a $1,000 cap buys ~0.5 of a $2,000 share rather than
    nothing."""
    qty, _, _ = _size(["AAA"], prices={"AAA": 2_000.0}, balance=100_000.0, cap=1_000.0,
                      settings=ON, fractionable={"AAA": True})

    assert qty["AAA"] == pytest.approx(0.5)


def test_the_same_symbol_is_still_skipped_on_the_whole_grid():
    qty, _, _ = _size(["AAA"], prices={"AAA": 2_000.0}, balance=100_000.0, cap=1_000.0)

    assert qty["AAA"] == 0


def test_no_min_one_share_bump_on_the_fractional_grid():
    """The whole grid bumps a sub-share result to 1 when funds allow. On the fractional grid
    the floor already bought the affordable fraction; bumping would spend money the weight
    did not grant."""
    qty, _, _ = _size(["AAA"], prices={"AAA": 100.0}, balance=100_000.0, cap=1_000.0,
                      settings=ON, fractionable={"AAA": True},
                      instruments={"AAA": {"weight": 5.0}})

    # 10 shares by the cap, x 5% weight = 0.5 -- NOT bumped to 1.
    assert qty["AAA"] == pytest.approx(0.5)


def test_a_round_lot_still_forces_whole_lots():
    qty, _, _ = _size(["AAA"], prices={"AAA": 10.0}, balance=100_000.0, cap=5_000.0,
                      settings=ON, fractionable={"AAA": True},
                      data={"AAA": {"lot_size": 100}})

    assert qty["AAA"] == 500


def test_the_budget_is_charged_the_fractional_cost():
    """Two symbols sharing a balance: the first's fractional cost is what the second sees
    remaining, not a whole-share approximation of it."""
    qty, _, _ = _size(["AAA", "BBB"], prices={"AAA": 333.0, "BBB": 333.0},
                      balance=1_500.0, cap=1_000.0, settings=ON,
                      fractionable={"AAA": True, "BBB": True})

    spent = qty["AAA"] * 333.0 + qty["BBB"] * 333.0
    assert spent <= 1_500.0 + 1e-9
    assert qty["AAA"] == pytest.approx(3.003)
