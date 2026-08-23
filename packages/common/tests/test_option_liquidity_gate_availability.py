"""A liquidity gate whose field the data source never publishes must be a LOUD config
error, not a silent zero-result (2026-08-23).

WHY: ``passes_liquidity`` fails CLOSED on ``None`` — correct when the field is published
and this one contract lacks it, catastrophic when NO contract publishes it. Measured
against the real 10 GB options cache: ``option_chain.open_interest`` is NULL for all
6,757,055 rows, so ``min_open_interest=100`` — the LIVE UI DEFAULT, present on all 14 live
option entry actions — rejected 16/16 structures on 16/16 symbol-date-capital combinations
and reported "No liquid <structure>", indistinguishable from a genuinely illiquid chain.

The fix is tri-state: the gate stays fail-closed per contract (an illiquid contract must
never slip through), but a gate the CHAIN cannot answer at all raises
``OptionLiquidityDataUnavailable`` naming the field.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.option_selector import (
    OptionLiquidityDataUnavailable,
    OptionSelectionConfigError,
    check_liquidity_data_available,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import ExpertActionType, OptionRight

TODAY = date(2024, 6, 3)
EXP = date(2024, 7, 19)


def _c(strike, *, oi=None, volume=None, bid=1.20, ask=1.30, right=OptionRight.CALL):
    return OptionContract(
        symbol=f"XYZ240719{right.value[0].upper()}{int(strike * 1000):08d}",
        underlying="XYZ", option_type=right, strike=float(strike), expiry=EXP,
        bid=bid, ask=ask, last=1.25, open_interest=oi, volume=volume)


# --------------------------------------------------------------------------- #
# the probe itself
# --------------------------------------------------------------------------- #
def test_open_interest_gate_on_a_chain_that_never_publishes_it_raises():
    chain = [_c(95.0), _c(100.0), _c(105.0)]          # open_interest NULL everywhere
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, min_open_interest=100, underlying="XYZ")
    assert ei.value.field == "open_interest"
    assert "open_interest" in str(ei.value) and "XYZ" in str(ei.value)


def test_volume_gate_on_a_chain_that_never_publishes_it_raises():
    chain = [_c(95.0, oi=500), _c(100.0, oi=500)]
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, min_volume=25, underlying="XYZ")
    assert ei.value.field == "volume"


def test_spread_gate_on_a_chain_with_no_two_sided_quotes_raises():
    chain = [_c(95.0, bid=None, ask=None), _c(100.0, bid=None, ask=None)]
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")
    assert ei.value.field == "spread"


def test_gate_is_evaluable_when_even_ONE_contract_publishes_the_field():
    """Partial publication is a real liquidity signal, not missing data: the contracts that
    do NOT publish stay fail-closed (rejected), exactly as before."""
    chain = [_c(95.0), _c(100.0, oi=800), _c(105.0)]
    check_liquidity_data_available(chain, min_open_interest=100, underlying="XYZ")  # no raise


def test_gates_that_are_off_are_never_probed():
    chain = [_c(95.0), _c(100.0)]                     # nothing published at all
    check_liquidity_data_available(chain, min_open_interest=None, min_volume=None,
                                   max_spread_pct=None, underlying="XYZ")


def test_empty_chain_is_not_a_gate_error():
    """An empty chain is its own (already-reported) condition — do not mislabel it."""
    check_liquidity_data_available([], min_open_interest=100, min_volume=25,
                                   max_spread_pct=15.0, underlying="XYZ")


def test_the_error_is_a_selection_config_error():
    assert issubclass(OptionLiquidityDataUnavailable, OptionSelectionConfigError)
    assert issubclass(OptionSelectionConfigError, ValueError)


# --------------------------------------------------------------------------- #
# end to end through a real option entry action
# --------------------------------------------------------------------------- #
from ba2_common.core.TradeActions import create_action                    # noqa: E402
from ba2_common.core.interfaces.OptionsAccountInterface import (          # noqa: E402
    OptionsAccountInterface,
)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "liqgate.sqlite"))
    db.init_db()
    yield


class _Acct(OptionsAccountInterface):
    """Chain shaped like the real historical cache: quotes present, OI and volume NULL."""

    def __init__(self, *, oi=None, volume=None):
        self.id = 1
        self._oi = oi
        self._vol = volume
        self.submitted = []

    def _as_of_date(self):
        return date(2024, 6, 1)

    def get_balance(self):
        return 100_000.0

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_current_price(self, symbol=None):
        return 100.0

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            otm = abs(float(s) - 100.0)
            bid = max(0.2, 5.0 - 0.08 * otm)
            out.append(OptionContract(
                symbol=f"{underlying}{s}{option_type.value[0].upper()}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=self._oi, volume=self._vol))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        order = SimpleNamespace(id=len(self.submitted) + 1, data={})
        self.submitted.append(option_strategy)
        return order

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 100_000.0


_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


def _run(action_type, acct, **kw):
    act = create_action(ExpertActionType(action_type), "AAPL", acct, SimpleNamespace(),
                        None, _REC, **kw)
    act.submit_to_broker = True
    return act.execute()


def test_live_default_min_oi_on_an_oi_less_chain_reports_the_real_cause():
    """The live UI default (min_open_interest=100) against a cache-shaped chain used to say
    'No liquid call contract' — which reads as 'the market is thin'. It must now name the
    missing FIELD, and must not silently look like a normal no-selection."""
    res = _run("buy_call", _Acct(oi=None), strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is False
    assert "open_interest" in res["message"]
    assert "No liquid" not in res["message"]


def test_the_same_rule_trades_normally_once_the_chain_publishes_open_interest():
    acct = _Acct(oi=1000)
    res = _run("buy_call", acct, strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is True, res["message"]
    assert acct.submitted == ["long_call"]


def test_a_published_but_low_open_interest_is_still_rejected_by_the_gate():
    """The gate must NOT become fail-open: a contract that publishes OI below the floor is
    still refused."""
    res = _run("buy_call", _Acct(oi=5), strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is False
    assert "No liquid" in res["message"]


# --------------------------------------------------------------------------- #
# DTE window
# --------------------------------------------------------------------------- #
def test_dte_max_none_is_a_config_error_not_an_empty_chain():
    """dte_min=30 with dte_max unset built the INVERTED fetch window [today+30, today] and
    reported 'Empty option chain' — a data problem, when it is a config problem."""
    res = _run("buy_call", _Acct(oi=1000), strike_method="percent_otm", strike_param=2.0,
               dte_min=30, dte_max=None, sizing=20.0)
    assert res["success"] is False
    assert "dte_max" in res["message"]
    assert "Empty option chain" not in res["message"]


def test_dte_min_greater_than_dte_max_is_a_config_error():
    res = _run("buy_call", _Acct(oi=1000), strike_method="percent_otm", strike_param=2.0,
               dte_min=45, dte_max=20, sizing=20.0)
    assert res["success"] is False
    assert "dte_min" in res["message"] and "dte_max" in res["message"]
