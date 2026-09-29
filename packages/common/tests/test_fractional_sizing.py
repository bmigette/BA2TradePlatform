"""Fractional shares for experts: the account method, the risk sizer, the classic RM.

Opt-in (``allow_fractional_shares``, default off). What must hold:

* an expert that did NOT opt in sizes exactly as before -- same numbers, same ``int`` type,
  and no new broker call;
* only a broker ``True`` selects the fractional grid; unknown is whole shares;
* the account caches the broker's answer for a day, per account instance, and a failed
  lookup degrades to unknown rather than raising into sizing.
"""
import time
from types import SimpleNamespace

import pytest

from ba2_common.core.account_types import MarginInfo
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.position_sizing import compute_risk_based_quantity


# ---------------------------------------------------------------------------
# The account method and its 24-hour cache
# ---------------------------------------------------------------------------

class _Broker(ReadOnlyAccountInterface):
    """Just enough account to exercise the concrete, cached ``get_fractionable``."""

    def __init__(self, answers):
        self.id = 1
        self.answers = answers
        self.calls = []

    def get_symbol_margin_info(self, symbols):
        self.calls.append(list(symbols))
        return {s: MarginInfo(symbol=s, bp_factor=1.0, fractionable=self.answers[s])
                for s in symbols if s in self.answers}


# Only the concrete, cached fractionability methods are under test; every other abstract
# broker method is irrelevant here, so the ABC's instantiation guard is lifted for this stub.
_Broker.__abstractmethods__ = frozenset()


def _bare(cls, *args):
    return cls(*args)


def test_the_answer_is_read_off_margin_info():
    broker = _bare(_Broker, {"AAPL": True, "BRK.A": False})

    assert broker.get_fractionable(["AAPL", "BRK.A"]) == {"AAPL": True, "BRK.A": False}


def test_a_symbol_the_broker_cannot_describe_is_unknown_not_false():
    broker = _bare(_Broker, {"AAPL": True})

    assert broker.get_fractionable(["ZZZZ"]) == {"ZZZZ": None}


def test_a_measured_answer_is_cached_for_a_day(monkeypatch):
    broker = _bare(_Broker, {"AAPL": True})
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    broker.get_fractionable(["AAPL"])
    clock["t"] += 23 * 3600
    broker.get_fractionable(["AAPL"])
    assert len(broker.calls) == 1, "re-asked the broker inside the 24h window"

    clock["t"] += 2 * 3600
    broker.get_fractionable(["AAPL"])
    assert len(broker.calls) == 2, "did not refresh after 24h"


def test_an_unknown_is_retried_much_sooner(monkeypatch):
    """Usually a transient failure; pinning it for a day would size a fractionable symbol
    in whole shares until tomorrow."""
    broker = _bare(_Broker, {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    broker.get_fractionable(["AAPL"])
    clock["t"] += 10 * 60
    broker.get_fractionable(["AAPL"])

    assert len(broker.calls) == 2


def test_a_cold_basket_is_one_broker_call():
    broker = _bare(_Broker, {"A": True, "B": True, "C": False})

    broker.get_fractionable(["A", "B", "C"])

    assert broker.calls == [["A", "B", "C"]]


def test_the_cache_is_per_account_instance():
    """Fractionability is an answer a specific broker gives a specific account. A CLASS-level
    dict would serve Alpaca's answer to TastyTrade."""
    alpaca = _bare(_Broker, {"AAPL": True})
    other = _bare(_Broker, {"AAPL": False})

    assert alpaca.get_fractionable(["AAPL"]) == {"AAPL": True}
    assert other.get_fractionable(["AAPL"]) == {"AAPL": False}


def test_a_failed_lookup_degrades_to_unknown():
    broker = _bare(_Broker, {})

    def _boom(symbols):
        raise RuntimeError("broker down")

    broker.get_symbol_margin_info = _boom

    assert broker.get_fractionable(["AAPL"]) == {"AAPL": None}


def test_a_non_bool_answer_is_recorded_as_unknown():
    broker = _bare(_Broker, {})
    broker._fetch_fractionable = lambda symbols: {"AAPL": "yes"}

    assert broker.get_fractionable(["AAPL"]) == {"AAPL": None}


def test_the_single_symbol_form_shares_the_cache():
    broker = _bare(_Broker, {"AAPL": True})

    assert broker.is_fractionable(" aapl ") is True
    assert broker.is_fractionable("AAPL") is True
    assert len(broker.calls) == 1
    assert broker.is_fractionable("") is None


# ---------------------------------------------------------------------------
# compute_risk_based_quantity on the grid
# ---------------------------------------------------------------------------

_BASE = dict(equity=10_000.0, current_price=333.0, risk_per_trade_pct=1.0, stop_price=300.0)


def test_the_default_is_whole_shares_and_still_an_int():
    """Every caller before fractional support: byte-identical number AND type."""
    out = compute_risk_based_quantity(**_BASE)

    assert out["quantity"] == 3
    assert isinstance(out["quantity"], int)


def test_a_fractional_unit_buys_the_fraction():
    out = compute_risk_based_quantity(**_BASE, quantity_unit=0.0001)

    assert out["quantity"] == pytest.approx(3.0303)
    assert isinstance(out["quantity"], float)


def test_both_clamps_land_on_the_same_grid():
    """A whole-share cash clamp over a fractional risk count would cut 2.5 to 2 for no
    reason the budget can explain."""
    out = compute_risk_based_quantity(**_BASE, available_balance=333.0 * 1.5,
                                      quantity_unit=0.0001)

    assert out["quantity"] == pytest.approx(1.5)
    assert out["capped_by"] == "balance"


def test_a_round_lot_forces_whole_shares_whatever_the_unit():
    out = compute_risk_based_quantity(equity=100_000.0, current_price=50.0,
                                      risk_per_trade_pct=1.0, stop_price=45.0,
                                      lot_size=100, quantity_unit=0.0001)

    assert out["quantity"] == 200
    assert isinstance(out["quantity"], int)


def test_a_budget_below_one_share_now_buys_a_fraction():
    out = compute_risk_based_quantity(equity=1_000.0, current_price=500.0,
                                      risk_per_trade_pct=1.0, stop_price=450.0,
                                      quantity_unit=0.0001)

    assert out["quantity"] == pytest.approx(0.2)


def test_whole_grid_keeps_floor_division_semantics():
    """The whole-share path keeps ``a // b`` rather than ``floor(a / b)`` -- the two differ on
    float edges -- so every existing backtest reproduces exactly. Pinned against the SAME
    expression on the same operands the sizer sees (a stop of 9.9 under 10.0 is a distance of
    0.0999...96, not 0.1)."""
    out = compute_risk_based_quantity(equity=100.0, current_price=10.0,
                                      risk_per_trade_pct=1.0, stop_price=9.9)

    assert out["qty_by_risk"] == int((100.0 * 0.01) // (10.0 - 9.9))


# ---------------------------------------------------------------------------
# The classic risk manager's helpers
# ---------------------------------------------------------------------------

def test_the_rm_asks_nothing_when_the_expert_did_not_opt_in():
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    rm = object.__new__(TradeRiskManagement)
    account = SimpleNamespace(get_fractionable=lambda s: pytest.fail("asked the broker"))

    assert rm._fractionable_by_symbol(account, ["AAPL"], allow_fractional=False) == {}


def test_the_rm_asks_once_for_the_whole_batch_when_opted_in():
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    rm = object.__new__(TradeRiskManagement)
    calls = []
    account = SimpleNamespace(get_fractionable=lambda s: calls.append(list(s)) or
                              {x: True for x in s})

    out = rm._fractionable_by_symbol(account, ["AAPL", "MSFT"], allow_fractional=True)

    assert out == {"AAPL": True, "MSFT": True}
    assert len(calls) == 1


def test_a_broker_outage_never_reaches_the_rm():
    """The ACCOUNT absorbs a failed broker lookup and reports unknown, so the RM sizes that
    symbol in whole shares and carries on. (An exception that does reach the RM's own guard
    is a programming error, and ``absorb_if_benign`` is right to surface it in strict mode.)
    """
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    broker = _bare(_Broker, {})

    def _boom(symbols):
        raise RuntimeError("broker down")

    broker.get_symbol_margin_info = _boom
    rm = object.__new__(TradeRiskManagement)

    assert rm._fractionable_by_symbol(broker, ["AAPL"], allow_fractional=True) == {"AAPL": None}


@pytest.mark.parametrize("fractionable,allow,lot,expected", [
    (True, True, None, 0.0001),
    (True, False, None, 1.0),
    (None, True, None, 1.0),
    (False, True, None, 1.0),
    (True, True, 100, 1.0),     # a round lot forces whole shares
])
def test_the_rm_grid_per_order(fractionable, allow, lot, expected):
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    order = SimpleNamespace(data={"lot_size": lot} if lot else {})

    assert TradeRiskManagement._quantity_unit(order, fractionable, allow) == expected


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def test_the_setting_exists_and_defaults_off():
    """Off by default so every existing expert keeps its whole-share sizing."""
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    definitions = MarketExpertInterface.get_merged_settings_definitions()
    setting = definitions["allow_fractional_shares"]

    assert setting["type"] == "bool"
    assert setting["default"] is False
