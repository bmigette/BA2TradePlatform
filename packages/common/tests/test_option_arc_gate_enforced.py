"""The ARC richness gate, WIRED INTO THE LIVE ENTRY PATH (OPT-C1).

``option_economics`` computes per-contract annualised return on collateral and is fully
unit-tested; ``rule_builders`` forwards ``option_min_arc`` into the action config. Nothing
consulted it. So a credit structure was still admitted on ``net_credit > 0`` alone, and a
search rewarded for win rate could still learn to sell near-worthless premium -- the exact
behaviour the gate exists to stop.

These tests drive the REAL ``TradeActions`` builders. The headline is a cash-secured put
paying FIFTEEN CENTS: measurable, positive, comfortably over the selector's own
``_MIN_TRADEABLE_PREMIUM`` of 0.10 -- and worth 1.8 %/yr on the ten thousand dollars it
ties up, i.e. less than a Treasury bill, for open-ended assignment risk.

ARITHMETIC USED THROUGHOUT (so the numbers below are checkable, not magic):
a 100-strike CSP reserves ``100 x 100 = 10,000``; at 30 days to expiry the annualisation
is ``365/30 = 12.1667``; so ``arc = bid * 100 / 10,000 * 12.1667 = bid * 0.121667``.
A $5.00 bid is 0.608/yr; a $0.15 bid is 0.0183/yr. A floor of 0.10 separates them.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_economics import ARC_FLOOR_REFUSAL
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import ExpertActionType, OptionRight

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 7, 1)          # exactly 30 days out
DTE = (EXPIRY - TODAY).days


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These builders persist TradeActionResult rows; sibling DB-seam tests repoint the
    global seam without restoring it, so take a fresh sqlite per test."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "arc_gate.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Options account with a fully-controlled premium curve.

    ``premium`` is the bid AT THE MONEY, decaying multiplicatively by 0.85 per $5 of
    distance (ask = bid + 0.02). Two properties matter:

      * the ATM bid is EXACTLY ``premium``, which keeps the CSP arithmetic in the module
        docstring exact rather than approximate;
      * the curve slopes, so a vertical / condor / jade lizard actually earns a net credit
        (on a flat curve ``short.bid - long.ask`` is negative and every builder refuses
        for the pre-existing reason, which would make the ARC tests vacuous).

    The decay is multiplicative rather than additive so the SHAPE is the same at every
    ``premium`` level -- a rich chain and a thin one differ only in scale.

    Cash is large enough that neither buying power nor assignment capacity can be the
    reason for a refusal; both are asserted separately below.
    """

    def __init__(self, spot=100.0, balance=50_000_000.0, premium=5.0):
        self.id = 1
        self.spot = spot
        self._balance = balance
        self.premium = premium
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
        out = []
        for s in range(60, 141, 5):
            bid = round(self.premium * (0.85 ** (abs(float(s) - self.spot) / 5.0)), 4)
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type == OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=EXPIRY, bid=bid, ask=round(bid + 0.02, 4),
                last=bid, open_interest=1000, volume=1000))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy))
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


def act(acct, action_type, **kw):
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    a = create_action(ExpertActionType(action_type), "XYZ", acct,
                      SimpleNamespace(), None, rec, **kw)
    a.submit_to_broker = True
    return a


def is_arc_refusal(res):
    return not res["success"] and ARC_FLOOR_REFUSAL in res["message"]


#: ATM cash-secured put: strike 100, collateral 10,000, 30 DTE.
CSP = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
           sizing=5.0)


# ==========================================================================
# THE HEADLINE — fifteen cents of premium on ten thousand dollars of collateral
# ==========================================================================
def test_fifteen_cents_of_premium_is_admitted_when_no_floor_is_configured():
    """Today's behaviour, pinned so the change below is visibly a CHANGE and the gate is
    genuinely opt-in. Note the selector's own ``_MIN_TRADEABLE_PREMIUM`` (0.10) does NOT
    catch this -- 15 cents clears it, which is exactly why a premium floor in dollars is
    not a richness criterion."""
    acct = FakeAccount(premium=0.15)
    res = act(acct, "sell_cash_secured_put", **CSP).execute()
    assert res["success"] is True, res["message"]


def test_fifteen_cents_of_premium_is_refused_by_a_floor():
    """0.15 x 0.121667 = 0.0183/yr against a 0.10 floor."""
    acct = FakeAccount(premium=0.15)
    res = act(acct, "sell_cash_secured_put", min_arc=0.10, **CSP).execute()
    assert is_arc_refusal(res), res["message"]


def test_a_rich_credit_clears_the_SAME_floor():
    """DISCRIMINATOR for the pair above: the gate must key on richness, not on the mere
    presence of a floor. 5.00 x 0.121667 = 0.608/yr, comfortably over 0.10."""
    acct = FakeAccount(premium=5.0)
    res = act(acct, "sell_cash_secured_put", min_arc=0.10, **CSP).execute()
    assert res["success"] is True, res["message"]


def test_the_refusal_is_not_buying_power_or_assignment_capacity():
    """The account holds 50M against a 10,000 bill, so nothing else in the path can be
    doing the refusing."""
    acct = FakeAccount(premium=0.15)
    assert acct.check_option_buying_power(10_000.0) is True
    assert acct.short_put_assignment_exposure().cost == pytest.approx(0.0)
    res = act(acct, "sell_cash_secured_put", min_arc=0.10, **CSP).execute()
    assert is_arc_refusal(res), res["message"]


def test_the_boundary_admits():
    """arc == floor is a satisfied floor, like every other cap in this path.
    0.15 x 0.121667 = 0.01825; use it as the floor exactly."""
    acct = FakeAccount(premium=0.15)
    floor = 0.15 * 100.0 / 10_000.0 * (365.0 / DTE)
    res = act(acct, "sell_cash_secured_put", min_arc=floor, **CSP).execute()
    assert res["success"] is True, res["message"]


# ==========================================================================
# EVERY credit builder consults it (not just the one that got wired)
# ==========================================================================
#: strategy key -> (action_type, builder kwargs). Every name here is in
#: ``OptionsAccountInterface.RESERVING_STRATEGIES``: those are exactly the structures that
#: post collateral and therefore have a return ON collateral at all.
#:
#: SIZING IS 2 % FOR THE DEFINED-RISK SHAPES, and that is not cosmetic. The assignment-
#: capacity gate compares the whole delivery bill against CASH, and the bill-to-reserve
#: ratio is scale-invariant: a bull put spread reserving (width - credit) x 100 = ~438
#: owes 9,500 per contract on delivery, so any sizing above ~4.6 % of equity is refused by
#: THAT gate no matter how much cash the account holds. Raising the balance cannot fix it
#: (the budget scales with the balance), so the size is what comes down -- otherwise these
#: tests would be measuring the capacity gate and calling it the ARC gate.
_DEFINED_RISK_SIZING = 2.0
CREDIT_BUILDERS = {
    "cash_secured_put": ("sell_cash_secured_put", dict(CSP)),
    "bear_call_spread": ("open_bear_call_spread",
                         dict(strike_method="percent_otm", strike_param=5.0,
                              dte_min=10, dte_max=40, sizing=_DEFINED_RISK_SIZING)),
    "bull_put_spread": ("open_bull_put_spread",
                        dict(strike_method="percent_otm", strike_param=5.0,
                             dte_min=10, dte_max=40, sizing=_DEFINED_RISK_SIZING)),
    "short_straddle": ("open_short_straddle",
                       dict(strike_method="percent_otm", strike_param=0.0,
                            dte_min=10, dte_max=40, sizing=_DEFINED_RISK_SIZING)),
    "short_strangle": ("open_short_strangle",
                       dict(strike_method="percent_otm", strike_param=10.0,
                            dte_min=10, dte_max=40, sizing=_DEFINED_RISK_SIZING)),
    "iron_condor": ("open_iron_condor",
                    dict(strike_method="percent_otm", strike_param=10.0,
                         wing_width_pct=5.0, dte_min=10, dte_max=40,
                         sizing=_DEFINED_RISK_SIZING)),
    "jade_lizard": ("open_jade_lizard",
                    dict(strike_method="percent_otm", strike_param=10.0,
                         wing_width_pct=5.0, dte_min=10, dte_max=40,
                         sizing=_DEFINED_RISK_SIZING)),
    "put_ratio_spread": ("open_put_ratio_spread",
                         dict(strike_method="percent_otm", strike_param=5.0,
                              wing_width_pct=5.0, dte_min=10, dte_max=40,
                              sizing=_DEFINED_RISK_SIZING)),
}


def test_the_builder_table_covers_every_reserving_strategy():
    """Drift guard. A structure added to ``RESERVING_STRATEGIES`` posts collateral, so it
    has an ARC and must be gated; this table is what the parametrised tests below walk.
    ``credit_spread``/``naked_put``/``debit_spread`` are reserve-table aliases with no
    builder of their own -- named here so the exemption is explicit."""
    aliases = {"credit_spread", "naked_put", "debit_spread"}
    assert set(CREDIT_BUILDERS) == set(OptionsAccountInterface.RESERVING_STRATEGIES) - aliases


@pytest.mark.parametrize("strategy", sorted(CREDIT_BUILDERS))
def test_every_credit_builder_opens_with_no_floor(strategy):
    """Baseline for the parametrised refusal below: without a floor every one of these
    structures reaches ``submit_option_order``. Without this, the refusal test could pass
    because the builder never got that far."""
    action_type, kw = CREDIT_BUILDERS[strategy]
    acct = FakeAccount(premium=5.0)
    res = act(acct, action_type, **kw).execute()
    assert res["success"] is True, res["message"]
    assert acct.submitted and acct.submitted[-1]["strategy"] == strategy


@pytest.mark.parametrize("strategy", sorted(CREDIT_BUILDERS))
def test_every_credit_builder_honours_an_unreachable_floor(strategy):
    """The wiring test: a 10,000 %/yr floor is unreachable by construction, so a builder
    that does not consult the gate is the one that still submits."""
    action_type, kw = CREDIT_BUILDERS[strategy]
    acct = FakeAccount(premium=5.0)
    res = act(acct, action_type, min_arc=100.0, **kw).execute()
    assert is_arc_refusal(res), res["message"]
    assert acct.submitted == []


# ==========================================================================
# The gate does NOT apply to structures with no collateral
# ==========================================================================
@pytest.mark.parametrize("action_type,kw", [
    ("buy_call", dict(strike_method="percent_otm", strike_param=5.0,
                      dte_min=10, dte_max=40, sizing=5.0)),
    ("buy_put", dict(strike_method="percent_otm", strike_param=5.0,
                     dte_min=10, dte_max=40, sizing=5.0)),
    ("open_bull_call_spread", dict(strike_method="percent_otm", strike_param=5.0,
                                   dte_min=10, dte_max=40, sizing=5.0)),
])
def test_a_debit_structure_is_untouched_by_the_floor(action_type, kw):
    """A long/debit structure reserves nothing, so "return on collateral" has no
    denominator and the gate does not apply (``option_economics.applies_to``).

    This is the failure mode of putting the call in the shared base class instead of in
    each credit builder: every debit structure would compute a None ARC, and a configured
    floor turns None into a refusal -- silently deleting half the search space.
    """
    acct = FakeAccount(premium=5.0)
    res = act(acct, action_type, min_arc=100.0, **kw).execute()
    assert res["success"] is True, res["message"]


def test_a_covered_call_is_untouched_by_the_floor():
    """A covered call collects a credit but reserves NO cash -- it is collateralised by
    shares, and is in ``ZERO_RESERVE_STRATEGIES`` for that reason. Gating it on return on
    CASH collateral would refuse every one of them."""
    acct = FakeAccount(premium=5.0)
    acct.get_option_positions = lambda: []
    res = act(acct, "sell_covered_call", min_arc=100.0,
              strike_method="percent_otm", strike_param=5.0,
              dte_min=10, dte_max=40).execute()
    # No shares are held, so it declines for THAT reason -- never the ARC one.
    assert not is_arc_refusal(res), res["message"]


# ==========================================================================
# Unknown is not a pass
# ==========================================================================
def test_an_unmeasurable_arc_refuses(monkeypatch):
    """``admits_credit_structure(None, floor)`` is False and unit-tested; this pins that
    the BUILDER honours it rather than treating a None as "gate had nothing to say"."""
    import ba2_common.core.TradeActions as ta

    monkeypatch.setattr(ta, "annualized_return_on_collateral",
                        lambda **_: None)
    acct = FakeAccount(premium=5.0)
    res = act(acct, "sell_cash_secured_put", min_arc=0.10, **CSP).execute()
    assert is_arc_refusal(res), res["message"]


def test_an_unreadable_floor_refuses():
    """A misconfigured gate must not silently become no gate."""
    acct = FakeAccount(premium=5.0)
    res = act(acct, "sell_cash_secured_put", min_arc="tight", **CSP).execute()
    assert is_arc_refusal(res), res["message"]


# ==========================================================================
# Sizing invariance, in situ
# ==========================================================================
@pytest.mark.parametrize("sizing", [1.0, 5.0, 40.0])
def test_the_verdict_does_not_move_with_position_size(sizing):
    """ARC is PER CONTRACT, which is what makes it usable as a gate: premium and
    collateral both scale with contract count. Pinned through the real builder, where the
    contract count really does change."""
    kw = dict(CSP, sizing=sizing)
    rich = act(FakeAccount(premium=5.0), "sell_cash_secured_put", min_arc=0.10, **kw).execute()
    poor = act(FakeAccount(premium=0.15), "sell_cash_secured_put", min_arc=0.10, **kw).execute()
    assert rich["success"] is True, rich["message"]
    assert is_arc_refusal(poor), poor["message"]


# ==========================================================================
# The floor reaches the ctor from a rule
# ==========================================================================
def test_min_arc_is_forwarded_by_the_evaluator():
    """``rule_builders`` already maps ``option_min_arc`` -> ``min_arc``; the evaluator
    forwards only the keys in ``_OPTION_ENTRY_PARAM_KEYS``, so the floor reaches no
    builder until it is named there."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS

    assert "min_arc" in _OPTION_ENTRY_PARAM_KEYS


def test_the_floor_survives_the_whole_rule_to_action_path():
    """End to end at the config level: a strategy rule carrying ``option_min_arc`` builds
    an action config the option action constructs with that floor set."""
    from ba2_common.core.rule_builders import action_from_rule

    cfg = action_from_rule({"action_type": "sell_cash_secured_put",
                            "option_min_arc": 0.25})["act"]
    acct = FakeAccount(premium=5.0)
    action = act(acct, cfg["action_type"], min_arc=cfg["min_arc"], **CSP)
    assert action.min_arc == 0.25
