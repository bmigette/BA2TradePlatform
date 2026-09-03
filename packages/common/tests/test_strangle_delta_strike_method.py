"""``OpenStrangleAction`` selects by DELTA when the method says so (plan Task 14b item 5).

WHAT THIS CLOSES. ``open_straddle``/``open_strangle`` were two of the eight builders that
hard-coded ``method="percent_otm"`` at every selection site (OPT-S2), so grid 2's O_ERN row
could not express design section 2's "strangle width delta 0.25-0.45": a delta band handed to
a percent-OTM selector targets 0.25 PERCENT out of the money -- effectively at the money -- on
both legs, which is a straddle wearing a strangle's name. The row therefore searched the unit
the builder actually read (percent), and recorded that converting it was a change to
``OpenStrangleAction``, not to the table. This is that change.

THE STRANGLE now passes ``method=self.strike_method`` on BOTH legs, so a delta target selects
symmetrically: the selector compares ABSOLUTE delta (``option_selector._pick_by``), and a call
and a put at the same |delta| sit on opposite sides of spot by construction -- which is exactly
what a symmetric-delta strangle is. It joins ``types.get_strike_method_action_values()``.

THE STRADDLE does NOT, and that is deliberate rather than an omission. Its strike is not a
parameter: both legs are the SAME strike nearest spot, and "the ATM strike" is the same
contract whether you name it as 0% OTM or as ~0.50 delta. A builder that took a method it
cannot act on would be back in the OPT-S2 trap from the other side -- the editor would offer a
knob whose value never changes a strike. It stays out of the registry, and this file pins that
its selection is unmoved by the method.

SHARED CODE, ONE IMPLEMENTATION: ``TradeActions`` is packages/common, used by the live platform
and the backtester through the same ``create_action``, so this test is the parity pin too.
"""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.TradeActions as TA
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import ExpertActionType, OptionRight, honours_strike_method

_SPOT = 100.0


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "strangle_delta.sqlite"))
    db.init_db()
    yield


class _Acct(OptionsAccountInterface):
    """A chain whose DELTA and whose %-OTM distance disagree about which strike to pick.

    Strikes 70..130 step 5. Delta is a smooth, monotone function of moneyness (calls positive,
    puts negative), pitched so a 0.30-delta call is 10 points ABOVE spot while a 5%-OTM call is
    5 points above it -- so a percent-configured run and a delta-configured run land on
    DIFFERENT contracts, which is what makes the assertions below able to fail."""

    def __init__(self):
        self.id = 1
        self.submitted = []
        self.legs_submitted = []

    def _as_of_date(self):
        return date(2024, 6, 1)

    def get_balance(self):
        return 1_000_000.0

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=1_000_000.0, equity=1_000_000.0,
                              net_liquidation=1_000_000.0)

    def get_positions(self):
        return []

    def get_instrument_current_price(self, symbol, price_type=None):
        return _SPOT

    def get_current_price(self, symbol=None):
        return _SPOT

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(70, 131, 5):
            strike = float(s)
            if option_type == OptionRight.CALL:
                otm = max(strike - _SPOT, 0.0)
                intrinsic = max(_SPOT - strike, 0.0)
                # 0.50 ATM, falling 0.02 per point above spot -> 0.30 at strike 110.
                delta = max(0.02, min(0.98, 0.5 - 0.02 * (strike - _SPOT)))
            else:
                otm = max(_SPOT - strike, 0.0)
                intrinsic = max(strike - _SPOT, 0.0)
                delta = -max(0.02, min(0.98, 0.5 - 0.02 * (_SPOT - strike)))
            bid = max(0.2, 5.0 - 0.08 * otm) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{option_type.value[0].upper()}",
                underlying=underlying, option_type=option_type, strike=strike,
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, volume=500,
                delta=round(delta, 4)))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(option_strategy)
        self.legs_submitted.append(list(legs))
        return SimpleNamespace(id=len(self.submitted), data={})

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 1_000_000.0


_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


def _legs(action_type, *, strike_method, strike_param):
    acct = _Acct()
    act = TA.create_action(
        ExpertActionType(action_type), "AAPL", acct, SimpleNamespace(), None, _REC,
        strike_method=strike_method, strike_param=strike_param,
        dte_min=10, dte_max=40, sizing=2.0,
        min_open_interest=10, max_spread_pct=90.0, min_volume=25)
    act.submit_to_broker = True
    act.execute()
    assert acct.legs_submitted, f"{action_type} submitted nothing"
    legs = acct.legs_submitted[-1]
    return {leg.option_type: leg for leg in legs}


# ==================================================================================================
# the STRANGLE learns the method
# ==================================================================================================
def test_the_strangle_is_registered_as_honouring_the_strike_method():
    assert honours_strike_method(ExpertActionType.OPEN_STRANGLE.value)


def test_a_delta_target_selects_BOTH_strangle_legs_by_delta():
    """0.30 delta on this chain is strike 110 (call) and strike 90 (put) -- symmetric about
    spot, and NOT where a 0.30 percent-OTM target would land (100.3 -> the 100 strike, i.e. a
    straddle in all but name). Derived from the fixture's own delta law:
    |delta| = 0.5 - 0.02 * |strike - 100|, so |delta| = 0.30 at |strike - 100| = 10."""
    legs = _legs(ExpertActionType.OPEN_STRANGLE.value,
                 strike_method="delta", strike_param=0.30)
    assert legs[OptionRight.CALL].strike == 110.0
    assert legs[OptionRight.PUT].strike == 90.0


def test_the_delta_target_moves_both_legs_together():
    """A different delta is a different structure: 0.40 sits 5 points out, 0.20 sits 15."""
    near = _legs(ExpertActionType.OPEN_STRANGLE.value,
                 strike_method="delta", strike_param=0.40)
    far = _legs(ExpertActionType.OPEN_STRANGLE.value,
                strike_method="delta", strike_param=0.20)
    assert (near[OptionRight.CALL].strike, near[OptionRight.PUT].strike) == (105.0, 95.0)
    assert (far[OptionRight.CALL].strike, far[OptionRight.PUT].strike) == (115.0, 85.0)


def test_the_percent_otm_strangle_is_UNCHANGED():
    """The regression guard: every existing O_STRG genome is a percent, and stage-1 results
    must stay reproducible. 5% OTM on a $100 spot is the 105/95 pair."""
    legs = _legs(ExpertActionType.OPEN_STRANGLE.value,
                 strike_method="percent_otm", strike_param=5.0)
    assert legs[OptionRight.CALL].strike == 105.0
    assert legs[OptionRight.PUT].strike == 95.0


def test_no_method_configured_still_means_percent_otm():
    """``strike_method=None`` is what every hand-written live rule that predates the choice
    carries. It must keep meaning percent, not silently become a delta."""
    legs = _legs(ExpertActionType.OPEN_STRANGLE.value,
                 strike_method=None, strike_param=5.0)
    assert legs[OptionRight.CALL].strike == 105.0
    assert legs[OptionRight.PUT].strike == 95.0


def test_a_delta_method_with_no_param_uses_the_builders_own_default_delta():
    """The percent path already has ``DEFAULT_OTM_PCT``; the delta path needs its own, or an
    unconfigured delta run would read 5.0 as a 5-DELTA target (the deepest OTM contract on
    the chain)."""
    legs = _legs(ExpertActionType.OPEN_STRANGLE.value,
                 strike_method="delta", strike_param=None)
    d = TA.OpenStrangleAction.DEFAULT_DELTA
    expect = 100.0 + (0.5 - d) / 0.02
    assert legs[OptionRight.CALL].strike == expect
    assert legs[OptionRight.PUT].strike == 200.0 - expect


# ==================================================================================================
# the STRADDLE stays ATM, by construction
# ==================================================================================================
def test_the_straddle_is_NOT_registered_as_honouring_the_strike_method():
    """Its strike is not a parameter -- see the module docstring."""
    assert not honours_strike_method(ExpertActionType.OPEN_STRADDLE.value)


@pytest.mark.parametrize("method,param", [("percent_otm", 5.0), ("delta", 0.30), (None, None)])
def test_the_straddle_picks_the_SAME_atm_strike_whatever_the_method_says(method, param):
    legs = _legs(ExpertActionType.OPEN_STRADDLE.value,
                 strike_method=method, strike_param=param)
    assert legs[OptionRight.CALL].strike == 100.0
    assert legs[OptionRight.PUT].strike == 100.0
