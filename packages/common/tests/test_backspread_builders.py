"""The 1x2 ratio BACKSPREADS — ``O_CBS`` (calls) / ``O_PBS`` (puts), design 2026-08-31 §2.

SELL 1 nearer leg, BUY 2 further-OTM legs, one right, one expiry. The convexity-financed
family: the short finances the longs, so the structure owns convexity without paying the
full bleed for it.

WHAT THIS FILE PINS, and why each one is a mutation waiting to happen rather than a
restatement of the code:

* **The max loss is MEASURED, and it is the pin BETWEEN the strikes** — at the LONG strike,
  where the short is fully ITM and the two longs are still worthless. Every number below is
  hand-derived from the brief's own worked example (sell 1 call K=100 @3.00, buy 2 calls
  K=110 @1.00 → net credit 1.00 → loss (10 − 1) × 100 = $900) and re-derived for the put
  mirror. Computing the worst case at the SHORT strike instead gives +$100 — a PROFIT — so
  that mutation is not a rounding error, it is a sign flip on the risk number the stop, the
  sizing and the reserve all divide by.
* **The inverted ratio is refused.** 2 short × 1 long is a net-uncovered short: on calls the
  loss is unbounded above; on puts the payoff evaluator still measures it, so the payoff gate
  ALONE cannot catch it and the structural guard is what does.
* **Sizing divides by the max loss, not by the premium.** A backspread's net is near zero by
  design and can be a credit, so premium sizing is not merely imprecise here — it divides by
  ~0 and sizes without limit. The near-zero-credit case below is the discriminator.
* **The short leg is COVERED**, never charged naked margin: 2 longs cover 1 short, the
  structure measures as defined risk, and the reserve comes from the defined-risk branch.
* **The MLEG sign convention** — negative = credit, positive = debit — on both signs, because
  the limit reaches the broker unchanged.
"""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.TradeActions as TA
from ba2_common.core.TradeActions import (
    BACKSPREAD_LONG_RATIO, BACKSPREAD_SHAPE_REFUSAL, BACKSPREAD_SHORT_RATIO,
    _backspread_shape_refusal, _entry_payoff_legs, _measured_max_loss_per_contract,
    create_action,
)
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_payoff import payoff_at, upside_slope
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import (
    ExpertActionType, OptionRight, OrderDirection, get_arc_floor_action_values,
    get_option_action_values, get_strike_method_action_values,
    get_wing_width_action_values, honours_strike_method,
)

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 6, 21)
SPOT = 100.0

CALL_BS = ExpertActionType.OPEN_CALL_BACKSPREAD.value
PUT_BS = ExpertActionType.OPEN_PUT_BACKSPREAD.value

#: (long-leg target, short-leg target) in ABSOLUTE delta — the ``_spread_params`` shape every
#: two-leg builder already uses. Design §2 searches the short at 0.35–0.50 and the longs at
#: 0.15–0.30; 0.20/0.40 sits in the middle of both bands.
DELTAS = [0.20, 0.40]
BASE = dict(strike_method="delta", strike_param=DELTAS, dte_min=10, dte_max=40, sizing=1.0)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These builders persist a ``TradeActionResult`` row and read the order back to stamp
    it, so they need a real (temp) DB. Sibling DB-seam tests repoint the global seam without
    restoring it, hence a fresh one per test."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "backspreads.sqlite"))
    db.init_db()
    yield


# --------------------------------------------------------------------------------------
# the chain, stated as a table so every dollar below can be derived by hand
# --------------------------------------------------------------------------------------
#: strike -> (bid, ask, |delta|). The two rows that matter are the 0.40-delta leg the builder
#: must SELL and the 0.20-delta leg it must BUY TWICE; the others exist so the pick is a
#: choice rather than the only survivor.
CALL_ROWS = {
    90.0: (11.00, 11.20, 0.60),
    100.0: (3.00, 3.20, 0.40),     # <- the SHORT leg
    110.0: (0.90, 1.00, 0.20),     # <- the LONG legs (x2), bought at the ASK = 1.00
    120.0: (0.20, 0.30, 0.10),
}
PUT_ROWS = {
    110.0: (11.00, 11.20, 0.60),
    100.0: (3.00, 3.20, 0.40),     # <- the SHORT leg
    90.0: (0.90, 1.00, 0.20),      # <- the LONG legs (x2)
    80.0: (0.20, 0.30, 0.10),
}


class FakeAccount(OptionsAccountInterface):
    """An options account with a hand-written chain and a recorded submit.

    ``long_ask`` overrides the 0.20-delta leg's ask, which is the ONLY knob the credit /
    debit / near-zero-credit cases below need: the net is ``2 × long.ask − short.bid`` and
    the short's bid is fixed at 3.00 by the table above.
    """

    def __init__(self, balance=1_000_000.0, long_ask=None, deltas=True,
                 half_spread=None):
        self.id = 1
        self._balance = balance
        self._long_ask = long_ask
        self._deltas = deltas
        self.submitted = []
        self.bp_checks = []
        if half_spread is not None:
            # The BACKTEST-only seam ``_modelled_half_spreads`` duck-types. Attached to the
            # INSTANCE, not the class, so every other test here stays a live-shaped account
            # that models no spread and concedes nothing.
            self.option_modelled_half_spread = lambda symbol: half_spread

    def _as_of_date(self):
        return TODAY

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance)

    def get_positions(self):
        return []

    def get_instrument_current_price(self, symbol, price_type=None):
        return SPOT

    def get_current_price(self, symbol=None):
        return SPOT

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        rows = CALL_ROWS if option_type == OptionRight.CALL else PUT_ROWS
        letter = "C" if option_type == OptionRight.CALL else "P"
        out = []
        for strike, (bid, ask, delta) in sorted(rows.items()):
            if self._long_ask is not None and delta == 0.20:
                ask = self._long_ask
                bid = round(ask - 0.10, 4)
            signed = delta if option_type == OptionRight.CALL else -delta
            out.append(OptionContract(
                symbol=f"{underlying}{strike:g}{letter}", underlying=underlying,
                option_type=option_type, strike=strike, expiry=EXPIRY,
                bid=bid, ask=ask, last=bid, open_interest=1000, volume=500,
                delta=(signed if self._deltas else None)))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy))
        return SimpleNamespace(id=len(self.submitted), data={})

    def check_option_buying_power(self, required):
        self.bp_checks.append(required)
        return True

    def available_option_buying_power(self):
        return self._balance

    # --- unused abstract bits
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


def run(action_type, acct=None, **overrides):
    """Execute one backspread builder; return ``(account, result)``."""
    acct = acct if acct is not None else FakeAccount()
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    kw = dict(BASE)
    kw.update(overrides)
    action = create_action(ExpertActionType(action_type), "XYZ", acct, SimpleNamespace(),
                           None, rec, **kw)
    action.submit_to_broker = True
    return acct, action.execute()


def submitted(acct):
    assert acct.submitted, "nothing was submitted"
    return acct.submitted[-1]


# ======================================================================================
# 1. THE MAX LOSS — hand-derived, both rights, at the LONG strike
# ======================================================================================
def test_the_call_backspread_prices_and_measures_the_briefs_worked_example():
    """SELL 1 call K=100 @3.00 (bid), BUY 2 calls K=110 @1.00 (ask).

        net       = 2 x 1.00 - 3.00 = -1.00       -> a NET CREDIT of 1.00
        width     = 110 - 100 = 10
        max loss  = (10 - 1.00) x 100 = $900      at expiry spot = 110 exactly:
                    short call ITM by 10 (-1000), longs worthless (0), credit kept (+100).
    """
    acct, res = run(CALL_BS)
    assert res["success"], res["message"]
    sub = submitted(acct)
    short_leg, long_leg = sub["legs"]
    assert (short_leg.strike, short_leg.side, short_leg.ratio_qty) == (
        100.0, OrderDirection.SELL, 1)
    assert (long_leg.strike, long_leg.side, long_leg.ratio_qty) == (
        110.0, OrderDirection.BUY, 2)
    assert sub["limit_price"] == pytest.approx(-1.00)
    assert res["data"]["max_loss_per_contract"] == pytest.approx(900.0)


def test_the_put_backspread_is_the_exact_mirror():
    """SELL 1 put K=100 @3.00, BUY 2 puts K=90 @1.00 → credit 1.00, max loss $900 at 90:
    the short put is ITM by 10, the two longs are worthless, the credit is kept."""
    acct, res = run(PUT_BS)
    assert res["success"], res["message"]
    sub = submitted(acct)
    short_leg, long_leg = sub["legs"]
    assert (short_leg.strike, short_leg.side, short_leg.ratio_qty) == (
        100.0, OrderDirection.SELL, 1)
    assert (long_leg.strike, long_leg.side, long_leg.ratio_qty) == (
        90.0, OrderDirection.BUY, 2)
    assert sub["limit_price"] == pytest.approx(-1.00)
    assert res["data"]["max_loss_per_contract"] == pytest.approx(900.0)


@pytest.mark.parametrize("action_type,long_strike,short_strike", [
    (CALL_BS, 110.0, 100.0),
    (PUT_BS, 90.0, 100.0),
])
def test_the_worst_case_is_at_the_LONG_strike_and_the_short_strike_is_a_PROFIT(
        action_type, long_strike, short_strike):
    """THE MUTATION THIS KILLS: measuring the worst case at the SHORT strike.

    At the short strike the structure has kept its whole credit and nothing is ITM — the
    payoff is +$100, a profit. Reading the risk number off that point does not merely
    understate the loss, it reports the wrong SIGN, and every consumer (``opt_sl_ml``'s
    denominator, the sizing divisor, the reserve) is downstream of it.
    """
    acct, res = run(action_type)
    assert res["success"], res["message"]
    sub = submitted(acct)
    payoff_legs = _entry_payoff_legs(sub["legs"], sub["limit_price"])

    assert payoff_at(payoff_legs, long_strike) == pytest.approx(-900.0)
    assert payoff_at(payoff_legs, short_strike) == pytest.approx(+100.0)
    # ...and the trough really is BETWEEN the strikes rather than at either tail: far away
    # in both directions the structure is better off than at the pin.
    assert payoff_at(payoff_legs, 0.0) > -900.0
    assert payoff_at(payoff_legs, 400.0) > -900.0
    assert res["data"]["max_loss_per_contract"] == pytest.approx(900.0)


def test_a_net_DEBIT_backspread_adds_the_debit_to_the_width():
    """Longs at 2.00 instead of 1.00: net = 2 x 2.00 - 3.00 = +1.00, a DEBIT.

    Max loss = (10 + 1.00) x 100 = $1,100 — the debit is money already spent that the
    worst case does not give back, so it ADDS to the width instead of netting off it.
    """
    acct, res = run(CALL_BS, acct=FakeAccount(long_ask=2.00))
    assert res["success"], res["message"]
    sub = submitted(acct)
    assert sub["limit_price"] == pytest.approx(+1.00)
    assert res["data"]["max_loss_per_contract"] == pytest.approx(1100.0)
    assert payoff_at(_entry_payoff_legs(sub["legs"], sub["limit_price"]),
                     110.0) == pytest.approx(-1100.0)


# ======================================================================================
# 2. THE STAMP (design 2026-08-29 §8.2)
# ======================================================================================
@pytest.mark.parametrize("action_type", [CALL_BS, PUT_BS])
def test_the_measured_max_loss_is_stamped_on_the_order_row(action_type):
    """``opt_sl_ml`` reads this number BACK off the parent order; a builder that submits
    without it silently disarms the stop for every structure it opens."""
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import AssetClass, OrderStatus, OrderType

    acct = FakeAccount()
    order_id = add_instance(TradingOrder(
        account_id=1, symbol="XYZ", underlying_symbol="XYZ", quantity=1,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.NEW, asset_class=AssetClass.OPTION, multiplier=100,
        data={}))
    acct.submit_option_order = (  # noqa: E731 — point the stamp at a real row
        lambda **kw: (acct.submitted.append(kw)
                      or SimpleNamespace(id=order_id, data={})))
    _, res = run(action_type, acct=acct)
    assert res["success"], res["message"]
    assert res["data"]["max_loss_per_contract"] == pytest.approx(900.0)
    stored = get_instance(TradingOrder, order_id)
    assert stored.data["max_loss_per_contract"] == pytest.approx(900.0)
    assert stored.data["option_reserve"] == pytest.approx(
        900.0 * acct.submitted[-1]["quantity"])


def test_the_stamp_is_the_CONCEDED_measurement_and_sizing_is_the_unconceded_one():
    """The entry-quote concession makes the stamp and the sizing divisor DIFFERENT NUMBERS,
    deliberately, and the comment beside the stamp says so -- this is that comment, executed.

    ``_quote_with_concession`` runs inside ``_submit_option_order``, AFTER the builder has
    sized and reserved (that ordering is the F3 decision: the concession is a quote gene, not
    a size gene). So with the gene live:

        modelled half-spread     0.10/leg, entry_cross 1.0
        conceded amount          1.0 x (0.10 x 1 short + 0.10 x 2 long) = 0.30/share
        limit                   -1.00 credit -> -0.70 (a credit CONCEDES by shrinking)
        sized + reserved on      (10 - 1.00) x 100 = 900   <- unconceded
        STAMPED                  (10 - 0.70) x 100 = 930   <- conceded, and LARGER

    The stamp is the conservative one in both directions (a debit rises, a credit shrinks),
    the gap is exactly the conceded amount x 100, and it is zero at the 0.0 default -- which
    is why every other test in this file reads one number for all three.
    """
    acct, res = run(CALL_BS, acct=FakeAccount(half_spread=0.10), entry_cross=1.0)
    assert res["success"], res["message"]
    sub = submitted(acct)
    assert sub["limit_price"] == pytest.approx(-0.70)
    assert res["data"]["max_loss_per_contract"] == pytest.approx(930.0)
    # ...sized and reserved on the UNCONCEDED 900, not on the 930 that got stamped.
    assert sub["quantity"] == 11                       # floor(10,000 / 900)
    assert res["data"]["option_reserve"] == pytest.approx(900.0 * 11)
    assert res["data"]["max_loss_per_contract"] > 900.0, (
        "the concession must never make the stamped risk SMALLER than the number the "
        "structure was sized against")


def test_without_the_concession_the_three_numbers_are_one():
    """The default, and the reason the rest of this file can read a single number: no
    modelled spread (every live account, and any backtest at entry_cross 0.0) means the
    stamp, the sizing divisor and the reserve are the same measurement AND the same value."""
    acct, res = run(CALL_BS, entry_cross=1.0)           # gene on, but nothing models a spread
    assert res["success"], res["message"]
    sub = submitted(acct)
    assert sub["limit_price"] == pytest.approx(-1.00)
    assert res["data"]["max_loss_per_contract"] == pytest.approx(900.0)
    assert res["data"]["option_reserve"] == pytest.approx(900.0 * sub["quantity"])


# ======================================================================================
# 3. THE INVERTED RATIO
# ======================================================================================
def _legs(right, *, short_strike, long_strike, short_ratio, long_ratio):
    letter = "C" if right == OptionRight.CALL else "P"
    return [
        OptionLeg(contract_symbol=f"XYZ{short_strike:g}{letter}", side=OrderDirection.SELL,
                  ratio_qty=short_ratio, position_intent="sell_to_open", option_type=right,
                  strike=short_strike, expiry=EXPIRY, underlying="XYZ"),
        OptionLeg(contract_symbol=f"XYZ{long_strike:g}{letter}", side=OrderDirection.BUY,
                  ratio_qty=long_ratio, position_intent="buy_to_open", option_type=right,
                  strike=long_strike, expiry=EXPIRY, underlying="XYZ"),
    ]


@pytest.mark.parametrize("right,short_strike,long_strike", [
    (OptionRight.CALL, 100.0, 110.0),
    (OptionRight.PUT, 100.0, 90.0),
])
def test_the_flipped_ratio_is_refused_as_a_net_uncovered_short(right, short_strike,
                                                               long_strike):
    """2 SHORT × 1 LONG — the mutation named in the plan. It must never be submitted."""
    flipped = _legs(right, short_strike=short_strike, long_strike=long_strike,
                    short_ratio=2, long_ratio=1)
    reason = _backspread_shape_refusal(flipped, option_type=right)
    assert reason is not None
    assert BACKSPREAD_SHAPE_REFUSAL in reason
    assert "UNCOVERED" in reason and "UNBOUNDED" in reason
    # ...and the correct 1x2 is admitted, so the guard is not simply always-refusing.
    assert _backspread_shape_refusal(
        _legs(right, short_strike=short_strike, long_strike=long_strike,
              short_ratio=BACKSPREAD_SHORT_RATIO, long_ratio=BACKSPREAD_LONG_RATIO),
        option_type=right) is None


def test_the_payoff_gate_alone_could_not_have_caught_the_flipped_PUT():
    """WHY THE STRUCTURAL GUARD EXISTS ALONGSIDE THE MEASURED-MAX-LOSS GATE.

    A flipped CALL backspread is caught twice — ``upside_slope`` is negative, so the payoff
    evaluator returns UNBOUNDED and the builder refuses on that alone. A flipped PUT is
    caught ONLY by the shape guard: 2 short puts against 1 long is bounded below (the
    underlying stops at zero), so the evaluator MEASURES it and would hand back a perfectly
    respectable number for a structure whose short leg nobody covers.
    """
    flipped_calls = _legs(OptionRight.CALL, short_strike=100.0, long_strike=110.0,
                          short_ratio=2, long_ratio=1)
    assert _measured_max_loss_per_contract(flipped_calls, -5.0) is None

    flipped_puts = _legs(OptionRight.PUT, short_strike=100.0, long_strike=90.0,
                         short_ratio=2, long_ratio=1)
    measured = _measured_max_loss_per_contract(flipped_puts, -5.0)
    assert measured is not None and measured > 0
    assert _backspread_shape_refusal(flipped_puts, option_type=OptionRight.PUT) is not None


@pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
def test_a_long_leg_that_is_not_further_OTM_is_refused(right):
    """The other half of the shape: the longs must sit FURTHER out than the short, or the
    trough is not between the strikes and the structure is not the one that was priced."""
    inside = (_legs(right, short_strike=100.0, long_strike=90.0,
                    short_ratio=1, long_ratio=2) if right == OptionRight.CALL
              else _legs(right, short_strike=100.0, long_strike=110.0,
                         short_ratio=1, long_ratio=2))
    reason = _backspread_shape_refusal(inside, option_type=right)
    assert reason is not None and "further OTM" in reason


def test_a_builder_whose_legs_do_not_form_a_backspread_never_reaches_the_broker(
        monkeypatch):
    """The guard is wired INTO the submit path, not merely importable beside it.

    Forcing the shape check to fail (the closest in-process stand-in for the flipped-ratio
    source mutation) must produce a refusal with nothing on the wire — never a submit.
    """
    monkeypatch.setattr(TA, "_backspread_shape_refusal",
                        lambda legs, *, option_type: "forced shape failure")
    acct, res = run(CALL_BS)
    assert not res["success"]
    assert "forced shape failure" in res["message"]
    assert acct.submitted == []


# ======================================================================================
# 4. SIZING — by MAX LOSS, never by premium
# ======================================================================================
def test_sizing_divides_the_budget_by_the_max_loss_and_not_by_the_net_premium():
    """THE DISCRIMINATOR. Longs at 1.495 → net = 2 × 1.495 − 3.00 = −0.01, a credit of ONE
    CENT, and max loss = (10 − 0.01) × 100 = $999.

        budget            = 1,000,000 x 1% = 10,000
        by MAX LOSS       = floor(10,000 / 999)  = 10 contracts
        by NET PREMIUM    = floor(10,000 / 1.00) = 10,000 contracts

    A premium-sized backspread is not off by a little: it is off by three orders of
    magnitude here, and by MORE the closer the structure prices to even — which is the
    direction the design deliberately pushes it. (A net-CREDIT backspread cannot be
    premium-sized at all: the divisor is negative.)
    """
    acct, res = run(CALL_BS, acct=FakeAccount(long_ask=1.495))
    assert res["success"], res["message"]
    assert res["data"]["max_loss_per_contract"] == pytest.approx(999.0)
    sub = submitted(acct)
    assert sub["limit_price"] == pytest.approx(-0.01)
    assert sub["quantity"] == 10


def test_the_debit_case_sizes_off_the_same_measured_number():
    """floor(10,000 / 1,100) = 9 — the same divisor rule, no premium special case."""
    acct, res = run(CALL_BS, acct=FakeAccount(long_ask=2.00))
    assert res["success"], res["message"]
    assert submitted(acct)["quantity"] == 9


def test_a_budget_below_one_contracts_max_loss_refuses_instead_of_rounding_up():
    _, res = run(CALL_BS, sizing=0.05)      # 1,000,000 x 0.05% = 500 < 900
    assert not res["success"]
    assert "Insufficient budget" in res["message"]


# ======================================================================================
# 5. COVERED, NEVER NAKED
# ======================================================================================
@pytest.mark.parametrize("action_type", [CALL_BS, PUT_BS])
def test_the_structure_is_never_charged_naked_margin(action_type, monkeypatch):
    """STRUCTURAL PIN. Neither Reg-T naked-margin helper may be invoked for a backspread:
    its single short is covered by two longs, so a naked charge would be pricing a risk the
    structure does not carry (and, at ~20% of notional, pricing it at the wrong number)."""
    calls = []
    for name in ("naked_margin_per_contract", "short_pair_margin_per_contract"):
        monkeypatch.setattr(
            OptionsAccountInterface, name,
            classmethod(lambda cls, *a, _n=name, **kw: calls.append(_n)))
    acct, res = run(action_type)
    assert res["success"], res["message"]
    assert calls == [], f"a backspread was priced through {calls}"


@pytest.mark.parametrize("action_type,expected_slope", [
    (CALL_BS, +100.0),      # +2 long calls -1 short call = +1 contract of upside
    (PUT_BS, 0.0),          # no call leg at all: flat above the short strike
])
def test_the_two_longs_cover_the_one_short(action_type, expected_slope):
    """``upside_slope`` is the property that decides bounded vs unbounded loss. A backspread
    is non-negative on it BECAUSE of the 1x2 ratio — flip the ratio and the call version
    goes to −100, which is what ``max_loss`` reports as UNBOUNDED."""
    acct, res = run(action_type)
    assert res["success"], res["message"]
    sub = submitted(acct)
    legs = _entry_payoff_legs(sub["legs"], sub["limit_price"])
    assert upside_slope(legs) == pytest.approx(expected_slope)


@pytest.mark.parametrize("action_type", [CALL_BS, PUT_BS])
def test_the_risk_manager_sees_a_DEFINED_risk_structure(action_type):
    """The rails' own classification, through the production seam. ``is_defined_risk`` False
    would route the structure to the ``undefined_risk_max_pct`` sub-cap — the naked bucket —
    instead of the deployment cap."""
    from ba2_common.core.OptionRiskManagement import candidate_from_entry

    acct, res = run(action_type)
    assert res["success"], res["message"]
    sub = submitted(acct)
    candidate = candidate_from_entry(
        underlying="XYZ", option_strategy=sub["strategy"], legs=sub["legs"],
        quantity=sub["quantity"],
        max_loss_per_contract=res["data"]["max_loss_per_contract"])
    assert candidate.is_defined_risk is True
    # The put version owes delivery on its ONE short put; the call version owes none.
    expected = (100.0 * 100.0 * sub["quantity"]) if action_type == PUT_BS else 0.0
    assert candidate.short_put_assignment == pytest.approx(expected)


def test_only_the_PUT_backspread_is_charged_short_put_delivery_capacity(monkeypatch):
    """A short CALL delivers shares and takes cash IN, so charging the call version would
    refuse entries for an obligation that does not exist."""
    seen = []
    real = TA._OptionEntryAction._downsize_to_delivery_capacity

    def spy(self, strategy, **kw):
        seen.append((strategy, kw["strike"], kw["contracts_per_unit"]))
        return real(self, strategy, **kw)

    monkeypatch.setattr(TA._OptionEntryAction, "_downsize_to_delivery_capacity", spy)
    _, res = run(CALL_BS)
    assert res["success"], res["message"]
    assert seen == []
    _, res = run(PUT_BS)
    assert res["success"], res["message"]
    assert seen == [("put_backspread", 100.0, BACKSPREAD_SHORT_RATIO)]


# ======================================================================================
# 6. THE RESERVE — the defined-risk basis, equal to the measured max loss
# ======================================================================================
@pytest.mark.parametrize("action_type,strategy", [
    (CALL_BS, "call_backspread"), (PUT_BS, "put_backspread"),
])
def test_the_reserve_equals_the_measured_max_loss(action_type, strategy):
    """THE BASIS QUESTION, answered: ``option_reserve_required``'s DEFINED-RISK branch — the
    one the credit verticals use — so the reserve is ``(width − net_credit) × 100 × qty``,
    which IS the measured max loss. Not the ``put_ratio_spread`` full-notional branch (that
    exists because a FRONTspread is net short), and not a Reg-T naked charge.

    The two numbers are computed by different code — one scans the payoff's critical points,
    the other is closed form — so their agreement is a real cross-check, not a tautology.
    """
    acct, res = run(action_type)
    assert res["success"], res["message"]
    sub = submitted(acct)
    per_contract = res["data"]["max_loss_per_contract"]
    assert res["data"]["option_reserve"] == pytest.approx(per_contract * sub["quantity"])
    assert OptionsAccountInterface.option_reserve_required(
        strategy, sub["quantity"], spread_width=10.0,
        net_credit=1.00) == pytest.approx(per_contract * sub["quantity"])
    # The buying-power gate was consulted with that same figure.
    assert acct.bp_checks and acct.bp_checks[0] == pytest.approx(
        res["data"]["option_reserve"])


@pytest.mark.parametrize("strategy", ["call_backspread", "put_backspread"])
def test_both_names_are_priced_rather_than_falling_off_the_reserve_table(strategy):
    """An unlisted strategy raises out of ``option_reserve_required`` (an unknown capital
    requirement must never read as zero, which passes every buying-power gate)."""
    assert strategy in OptionsAccountInterface.RESERVING_STRATEGIES
    assert strategy not in OptionsAccountInterface.ZERO_RESERVE_STRATEGIES
    # A debit of 1.00 (net_credit = -1.00) reserves the width PLUS the debit.
    assert OptionsAccountInterface.option_reserve_required(
        strategy, 2, spread_width=10.0, net_credit=-1.00) == pytest.approx(2200.0)


# ======================================================================================
# 7. THE MLEG SIGN CONVENTION
# ======================================================================================
@pytest.mark.parametrize("long_ask,expected_limit", [
    (1.00, -1.00),     # credit: 2 x 1.00 - 3.00
    (2.00, +1.00),     # debit:  2 x 2.00 - 3.00
    (1.495, -0.01),    # a credit of one cent is still a credit
])
def test_the_limit_price_carries_the_house_sign_convention(long_ask, expected_limit):
    """Negative = net credit, positive = net debit. The limit reaches the broker unchanged,
    so a flipped sign is an order that pays out instead of collecting (or vice versa)."""
    acct, res = run(CALL_BS, acct=FakeAccount(long_ask=long_ask))
    assert res["success"], res["message"]
    assert submitted(acct)["limit_price"] == pytest.approx(expected_limit)


def test_both_signs_are_admissible_and_neither_is_refused_for_its_sign():
    """Design §2: the delta bands decide which sign a given day produces; the builder has no
    opinion. (Contrast the credit verticals, which refuse a non-positive net credit.)"""
    for long_ask in (1.00, 2.00):
        _, res = run(CALL_BS, acct=FakeAccount(long_ask=long_ask))
        assert res["success"], res["message"]


# ======================================================================================
# 8. REGISTRY / PLUMBING
# ======================================================================================
@pytest.mark.parametrize("value", [CALL_BS, PUT_BS])
def test_both_actions_are_registered_where_the_strike_method_honouring_builders_are(value):
    assert value in get_option_action_values()
    assert value in get_strike_method_action_values()
    assert honours_strike_method(value)
    # NOT wing-width structures (no leg is placed a fixed % from another) and NOT arc-floor
    # structures: the premium-richness floor measures annualised return on collateral, and a
    # backspread's credit is near zero BY DESIGN, so a floor would delete exactly the
    # structures the design wants (see get_arc_floor_action_values' docstring).
    assert value not in get_wing_width_action_values()
    assert value not in get_arc_floor_action_values()


@pytest.mark.parametrize("class_name,expected", [
    ("OpenCallBackspreadAction", ExpertActionType.OPEN_CALL_BACKSPREAD),
    ("OpenPutBackspreadAction", ExpertActionType.OPEN_PUT_BACKSPREAD),
])
def test_the_evaluator_resolves_the_class_back_to_its_action_type(class_name, expected):
    """The wiring that has silently dropped a new option action before: three hardcoded
    lists live in ``TradeActionEvaluator``, and a class missing from the class map resolves
    to None, which routes the action to the 'unknown type' branch — it never submits, and
    the only symptom is zero fills."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    cls = getattr(TA, class_name)
    inst = cls.__new__(cls)
    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    assert ev._get_action_type_from_action(inst) == expected


@pytest.mark.parametrize("action_type", [CALL_BS, PUT_BS])
def test_the_delta_cause_is_NAMED_when_the_chain_carries_no_greeks(action_type):
    """The Task-1 seam, wired the same way as on the other nine: a chain with no delta at
    all must say so, not read as generic illiquidity — the two point at opposite remedies."""
    _, res = run(action_type, acct=FakeAccount(deltas=False))
    assert not res["success"]
    assert "strike_method='delta'" in res["message"]
    assert "carry a delta" in res["message"]


@pytest.mark.parametrize("action_type", [CALL_BS, PUT_BS])
def test_an_empty_chain_refuses_before_anything_else(action_type):
    acct = FakeAccount()
    acct.get_option_chain = lambda *a, **kw: []
    _, res = run(action_type, acct=acct)
    assert not res["success"]
    assert "Empty option chain" in res["message"]


@pytest.mark.parametrize("action_type", [CALL_BS, PUT_BS])
def test_the_percent_otm_method_is_honoured_too(action_type):
    """``strike_method`` is READ, not hard-coded — the registry above promises exactly that,
    and a 5%/12% pair must select the 5%-out leg short and the 12%-out leg long."""
    acct, res = run(action_type, strike_method="percent_otm", strike_param=[12.0, 5.0])
    assert res["success"], res["message"]
    short_leg, long_leg = submitted(acct)["legs"]
    if action_type == CALL_BS:
        assert (short_leg.strike, long_leg.strike) == (100.0, 110.0)
    else:
        assert (short_leg.strike, long_leg.strike) == (100.0, 90.0)
