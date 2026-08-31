"""The three GA-wired selection weights must reach the REAL entry builders and govern the
contract they select — driven end to end through ``create_action``, exactly as the
``TradeActionEvaluator`` constructs them.

WHY BUILDER-LEVEL AND NOT SELECTOR-LEVEL. ``_OptionEntryAction.__init__`` takes ``**kwargs``,
so a weight the evaluator forwards under a name the ctor does not explicitly store is
SWALLOWED SILENTLY — the gene then looks wired at every upstream layer while every pick runs
the default policy. Only driving the real builder and asserting the SELECTED CONTRACT moved
can catch that.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import ExpertActionType, OptionRight

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 7, 1)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "sel_weights.sqlite"))
    db.init_db()
    yield


def _c(underlying, strike, px, option_type, *, iv=0.30, vol=1000, delta=None):
    return OptionContract(
        symbol=f"{underlying}{strike:g}{'C' if option_type == OptionRight.CALL else 'P'}",
        underlying=underlying, option_type=option_type, strike=float(strike),
        expiry=EXPIRY, bid=px, ask=px, last=px, implied_volatility=iv,
        delta=delta, open_interest=1000, volume=vol)


class _ChainAccount(OptionsAccountInterface):
    """Options account serving ONE hand-built chain (same degenerate bid==ask shape as the
    historical store). ``spec`` rows: (strike, px, {field overrides})."""

    def __init__(self, spec, spot=100.0, balance=50_000_000.0):
        self.id = 1
        self.spot = spot
        self._balance = balance
        self.spec = spec
        self.submitted = []

    def open_option_orders_book_wide(self):
        return []

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance)

    def _as_of_date(self):
        return TODAY

    def get_instrument_current_price(self, symbol, price_type=None):
        return self.spot

    def get_current_price(self, symbol=None):
        return self.spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        return [_c(underlying, s, px, option_type, **over) for s, px, over in self.spec]

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy, legs=list(legs)))
        return SimpleNamespace(id=len(self.submitted), data={})

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


def _strikes(acct):
    return sorted(leg.strike for leg in acct.submitted[-1]["legs"])


def _run(acct, action_type, **kw):
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    a = create_action(ExpertActionType(action_type), "XYZ", acct, SimpleNamespace(),
                      None, rec, **kw)
    a.submit_to_broker = True
    res = a.execute()
    assert res["success"] is True, res["message"]
    return acct


BASE = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40, sizing=5.0)

#: ATM box centre at 100; the 90 is much RICHER per dollar of strike, the 110 much cheaper.
#: Premiums are a plausible call ladder (decaying in strike), so premium richness and the box
#: centre genuinely disagree — which is the whole point of searching the choosing.
CALL_LADDER = [(90, 11.0, {}), (95, 7.0, {}), (100, 4.0, {}), (105, 2.2, {}), (110, 1.1, {})]


# =========================================================================== #
# 1. per weight: the gene value MOVES the real builder's selected contract
# =========================================================================== #
def test_w_premium_moves_the_long_call_builders_selected_contract():
    base = _run(_ChainAccount(CALL_LADDER), "buy_call", **BASE)
    rich = _run(_ChainAccount(CALL_LADDER), "buy_call", **BASE, w_premium=2.0)
    assert _strikes(base) == [100.0]
    assert _strikes(rich) != [100.0], "w_premium reached the ctor but not the pick"


def test_w_premium_is_signed_at_the_builder_positive_and_negative_pick_differently():
    """The pinned w_box_center=1.0 always pulls toward the target, so the cheap tilt can
    only win where the cheaper contract sits CLOSE to the box centre — hand-derived: with
    strikes {100, 102} the box column inverts to [1, 0] and the richness column normalises
    to [1, 0], so w_premium=-2 scores [1-2, 0] and flips the pick to the 102, while +2
    scores [3, 0] and holds the 100."""
    near_pair = [(100, 4.0, {}), (102, 0.4, {})]
    rich = _run(_ChainAccount(near_pair), "buy_call", **BASE, w_premium=2.0)
    cheap = _run(_ChainAccount(near_pair), "buy_call", **BASE, w_premium=-2.0)
    assert _strikes(rich) == [100.0]
    assert _strikes(cheap) == [102.0], (
        "the SIGNED domain is the Task 7 behaviour change; both signs picking the same "
        "contract means the debit half cannot express 'prefer cheaper'")


def test_w_iv_moves_the_credit_builders_selected_contract():
    """The CSP is the credit half's seam. IV rises away from the money on the put ladder, so
    a vol-seller tilt (+w_iv) must leave the ATM box centre."""
    put_ladder = [(80, 0.9, {"iv": 0.65}), (90, 1.8, {"iv": 0.45}),
                  (100, 3.6, {"iv": 0.30}), (110, 9.0, {"iv": 0.22})]
    base = _run(_ChainAccount(put_ladder), "sell_cash_secured_put", **BASE)
    vol = _run(_ChainAccount(put_ladder), "sell_cash_secured_put", **BASE, w_iv=2.0)
    assert _strikes(base) == [100.0]
    assert _strikes(vol) == [80.0]


def test_w_rvol_moves_the_pick_toward_the_traded_contract():
    ladder = [(95, 5.0, {"vol": 12000}), (100, 4.0, {"vol": 40}), (105, 3.2, {"vol": 30})]
    base = _run(_ChainAccount(ladder), "buy_call", **BASE)
    liquid = _run(_ChainAccount(ladder), "buy_call", **BASE, w_rvol=2.0)
    assert _strikes(base) == [100.0]
    assert _strikes(liquid) == [95.0]


# =========================================================================== #
# 2. the vertical builders thread the policy too
# =========================================================================== #
VERT = dict(strike_method="percent_otm", strike_param=2.0, option_short_delta=None,
            dte_min=10, dte_max=40, sizing=5.0)


def test_the_vertical_builders_selected_legs_move_with_a_weight():
    """4 of 15 members select through select_vertical_spread; a weight wired only into
    select_single is a dead gene for all of them."""
    ladder = [(90, 11.0, {}), (95, 7.0, {}), (100, 4.0, {}), (105, 2.2, {}), (110, 1.1, {})]
    base = _run(_ChainAccount(ladder), "open_bull_call_spread",
                strike_method="percent_otm", strike_param=2.0, dte_min=10, dte_max=40,
                sizing=5.0)
    moved = _run(_ChainAccount(ladder), "open_bull_call_spread",
                 strike_method="percent_otm", strike_param=2.0, dte_min=10, dte_max=40,
                 sizing=5.0, w_premium=2.0)
    assert _strikes(base) != _strikes(moved), (
        "w_premium did not reach select_vertical_spread's leg picks")


# =========================================================================== #
# 3. the no-op, at the builder
# =========================================================================== #
@pytest.mark.parametrize("kw", [{}, {"w_premium": 0.0, "w_iv": 0.0, "w_rvol": 0.0},
                                {"w_premium": None}])
def test_absent_and_zero_weights_select_the_identical_contract(kw):
    base = _run(_ChainAccount(CALL_LADDER), "buy_call", **BASE)
    same = _run(_ChainAccount(CALL_LADDER), "buy_call", **BASE, **kw)
    assert _strikes(base) == _strikes(same)
    assert same.submitted[-1]["limit_price"] == base.submitted[-1]["limit_price"]


# =========================================================================== #
# 4. the forwarding layers: rule config -> evaluator keys -> ctor kwargs
# =========================================================================== #
def test_the_rule_builder_maps_the_option_w_keys_to_the_evaluator_names():
    """gene name (option_w_premium, the launcher/action-config key) -> evaluator key
    (w_premium, the ctor kwarg). A key missing from _OPTION_ACTION_PARAM_KEYS never leaves
    the rule dict; one missing from _OPTION_ENTRY_PARAM_KEYS never leaves the config."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    from ba2_common.core.rule_builders import action_from_rule

    rule = {"action_type": "buy_call", "option_strike_method": "percent_otm",
            "option_strike_param": 0.0, "option_dte_min": 10, "option_dte_max": 40,
            "option_w_premium": -1.5, "option_w_iv": 0.5, "option_w_rvol": 1.0}
    cfg = action_from_rule(rule)["act"]
    assert cfg["w_premium"] == -1.5
    assert cfg["w_iv"] == 0.5
    assert cfg["w_rvol"] == 1.0
    for key in ("w_premium", "w_iv", "w_rvol"):
        assert key in _OPTION_ENTRY_PARAM_KEYS, (
            f"{key} maps out of the rule but the evaluator will not forward it to the ctor")


def test_a_zero_weight_survives_the_rule_builder_exactly_like_entry_cross_does():
    """0.0 must reach the action as 0.0, not be dropped as falsy: an absent kwarg and an
    explicit zero agree today, and this keeps them agreeing."""
    from ba2_common.core.rule_builders import action_from_rule

    rule = {"action_type": "buy_call", "option_w_premium": 0.0}
    assert action_from_rule(rule)["act"]["w_premium"] == 0.0


# =========================================================================== #
# 5. the band guard: a weight the search could never have emitted
# =========================================================================== #
# WIRED_WEIGHT_BANDS is the domain the launcher samples AND the domain the live rule editor
# enforces, so a value outside it is a rule no backtest can reproduce. Refusing is the fail-
# closed choice: clamping would show one number and rank on another.
def test_a_weight_inside_its_band_is_accepted():
    """The control arm -- a guard that refuses everything would pass every test below."""
    from ba2_common.core.option_selection_policy import validate_wired_weights

    validate_wired_weights({"w_premium": -2.0, "w_iv": 2.0, "w_rvol": 0.0})


@pytest.mark.parametrize("weights", [
    {"w_premium": 2.5},      # past the signed ceiling
    {"w_premium": -2.5},     # past the signed floor
    {"w_iv": 100.0},
    {"w_rvol": -0.5},        # UNSIGNED by design: nobody wants an illiquid contract
])
def test_a_weight_outside_its_band_is_refused(weights):
    from ba2_common.core.option_selection_policy import (
        SelectionWeightOutOfBand, validate_wired_weights,
    )

    with pytest.raises(SelectionWeightOutOfBand):
        validate_wired_weights(weights)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_weight_is_refused(value):
    """NaN compares False against everything, so a NaN weight does not error the pick -- it
    silently collapses it to list order, which looks exactly like a working policy."""
    from ba2_common.core.option_selection_policy import (
        SelectionWeightOutOfBand, validate_wired_weights,
    )

    with pytest.raises(SelectionWeightOutOfBand):
        validate_wired_weights({"w_premium": value})


def test_a_weight_the_ga_does_not_emit_is_refused_even_though_the_field_exists():
    """``w_profit`` and ``w_spread`` ARE real SelectionPolicy fields, which is what makes this
    the dangerous case: forwarding one would look wired at every layer and rank on nothing
    (no builder supplies the structure_fn w_profit needs; neither grid store can answer
    w_spread). They must be refused here until something emits them, not accepted because the
    dataclass happens to have the attribute."""
    from ba2_common.core.option_selection_policy import (
        SelectionWeightOutOfBand, validate_wired_weights,
    )

    for name in ("w_profit", "w_spread", "w_rr", "w_box_center"):
        with pytest.raises(SelectionWeightOutOfBand):
            validate_wired_weights({name: 1.0})
