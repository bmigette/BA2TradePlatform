"""``O_PMCC`` — the poor man's covered call, entry through roll through structure exit.

Design: ``docs/superpowers/specs/2026-08-31-leaps-grid-design.md`` §3–§4; plan Task 6.

A LEAPS call standing in for 100 shares, with a nearer-dated short call sold above its
strike and ROLLED at each overlay expiry. It is the first structure in this platform whose
legs disagree about when they expire, so almost everything here is a first: two expiry
windows in one builder, a submit that carries two dates, a max-loss stamp that gets
REWRITTEN mid-life, and a close that has to find a leg the entry order never mentioned.

WHAT THIS FILE PINS, each one a real mutation rather than a restatement:

* **The max loss is the NET DEBIT, measured — not asserted.** Design §3 says "max loss =
  LEAPS debit − net credits", and the claim under test is that the SHARED payoff evaluator
  already produces exactly that for a call diagonal. Every dollar below is hand-derived from
  the fixture chain (LEAPS ask 20.00, overlay bid 3.00 → net 17.00 → $1,700) so a builder
  that invents its own formula, or a payoff change that stops measuring this shape, both
  show up as a number and not as a passing test.
* **Admission is a REFUSAL, not a filter.** A short at or below the LEAPS strike is a bear
  call spread, whose worst case is NOT the debit paid; a short that outlives the long is a
  naked call. Both are recorded verdicts, so a chain that cannot offer the structure today
  produces a reason instead of a different structure.
* **COVERED, never naked.** The cover is an OPTION inside the order's own legs, so
  ``structure_metrics`` measures it without being handed a ``stock_cover_price`` — and the
  rails charge the deployment cap, not the naked sub-cap.
* **The overlay SPEC rides the entry order row.** The roll happens weeks later with a
  different recommendation in hand; the row is the only thing that travels with the
  position.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import (
    PMCC_ADMISSION_REFUSAL, _measured_max_loss_per_contract, create_action,
)
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_expiry import (
    MULTI_EXPIRY_OPTION_STRATEGIES, PMCC_STRATEGY, is_multi_expiry_strategy,
)
from ba2_common.core.option_lifecycle import (
    ORDER_PMCC_OVERLAY_KEY, PMCC_OVERLAY_SPEC_KEYS,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import (
    ExpertActionType, OptionRight, OrderDirection, OrderStatus, get_option_action_values,
    honours_strike_method, is_option_action,
)

PMCC = ExpertActionType.OPEN_PMCC.value

TODAY = date(2024, 6, 3)
#: 382 days out — inside the authored LEAPS window [380, 470], i.e. design §2's "DTE >= 365".
LEAPS_EXPIRY = TODAY + timedelta(days=382)
#: 39 days out — inside the overlay's own [30, 45] window.
OVERLAY_EXPIRY = TODAY + timedelta(days=39)
SPOT = 100.0

#: LEAPS chain: strike -> (bid, ask, delta). The 0.80-delta row is the pick, and its ASK of
#: 20.00 is the left half of every dollar in this file.
LEAPS_ROWS = {
    70.0: (32.80, 33.00, 0.90),
    80.0: (19.80, 20.00, 0.80),     # <- the LONG leg, bought at the ASK = 20.00
    90.0: (14.80, 15.00, 0.70),
}
#: Overlay chain. The 0.20-delta row is the pick, and its BID of 3.00 is the right half.
OVERLAY_ROWS = {
    105.0: (5.00, 5.20, 0.30),
    110.0: (3.00, 3.20, 0.20),      # <- the SHORT leg, sold at the BID = 3.00
    120.0: (0.40, 0.50, 0.10),
}
#: The same overlay deltas at strikes BELOW the LEAPS strike — the admission case.
LOW_OVERLAY_ROWS = {
    60.0: (5.00, 5.20, 0.30),
    70.0: (3.00, 3.20, 0.20),       # 70 < the 80 LEAPS strike -> refused
    75.0: (0.40, 0.50, 0.10),
}

NET_DEBIT = 17.00           # 20.00 ask - 3.00 bid
MAX_LOSS = 1700.0           # x 100, the intrinsic floor: at spot 0 both legs are worthless

BASE = dict(strike_method="delta", strike_param=[0.80, 0.20],
            dte_min=380, dte_max=470, short_dte_min=30, short_dte_max=45, sizing=1.0)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """A real (temp) DB: this builder goes through the REAL ``submit_option_order`` — the
    guard, the parent row, the per-leg children, the Transaction and the entry-fact stamp —
    because what most of these tests assert is which rows exist and what they carry."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "pmcc.sqlite"))
    db.init_db()
    yield


class PMCCAccount(OptionsAccountInterface):
    """An option-capable account with a hand-written two-expiry chain and a real submit path.

    Only the broker call and the transaction factory are stubbed; the guard, the row writes
    and the intent stamp are the production code. ``overlay_rows`` and ``overlay_expiry`` are
    the only knobs the admission cases need.
    """

    def __init__(self, balance=1_000_000.0, overlay_rows=None, overlay_expiry=None,
                 deltas=True):
        self.id = 1
        self._balance = balance
        self._overlay_rows = overlay_rows if overlay_rows is not None else OVERLAY_ROWS
        self._overlay_expiry = overlay_expiry or OVERLAY_EXPIRY
        self._deltas = deltas
        self.submitted = []

    # -- clock / prices ------------------------------------------------------
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

    # -- the chain -----------------------------------------------------------
    def _rows_for(self, expiry_min, expiry_max):
        """Whichever leg's window this fetch is asking for. The two windows never overlap,
        which is the structure: the overlay expires long before the LEAPS."""
        if expiry_min <= LEAPS_EXPIRY <= expiry_max:
            return LEAPS_ROWS, LEAPS_EXPIRY
        if expiry_min <= self._overlay_expiry <= expiry_max:
            return self._overlay_rows, self._overlay_expiry
        return {}, None

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        rows, expiry = self._rows_for(expiry_min, expiry_max)
        out = []
        for strike, (bid, ask, delta) in sorted(rows.items()):
            out.append(OptionContract(
                symbol=f"{underlying}{expiry:%y%m%d}C{int(strike * 1000):08d}",
                underlying=underlying, option_type=OptionRight.CALL, strike=strike,
                expiry=expiry, bid=bid, ask=ask, last=bid, open_interest=1000, volume=500,
                delta=(delta if self._deltas else None)))
        return out

    # -- the broker seam -----------------------------------------------------
    def _create_transaction_for_order(self, trading_order):
        from ba2_common.core.db import add_instance
        trading_order.transaction_id = add_instance(Transaction(
            symbol=trading_order.symbol, quantity=trading_order.quantity,
            side=trading_order.side, multiplier=100, expert_id=None))

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        from ba2_common.core.db import update_instance
        self.submitted.append((trading_order, list(legs), list(leg_orders or [])))
        trading_order.status = OrderStatus.FILLED
        trading_order.broker_order_id = f"double-{trading_order.id}"
        trading_order.open_price = trading_order.limit_price
        trading_order.filled_qty = trading_order.quantity
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = OrderStatus.FILLED
            child.filled_qty = child.quantity
            update_instance(child)
        return trading_order

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return self._balance

    # -- unused abstract bits ------------------------------------------------
    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None


def run(acct=None, **overrides):
    """Execute the PMCC builder; return ``(account, result)``."""
    acct = acct if acct is not None else PMCCAccount()
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    kw = dict(BASE)
    kw.update(overrides)
    action = create_action(ExpertActionType.OPEN_PMCC, "XYZ", acct, SimpleNamespace(),
                           None, rec, **kw)
    action.submit_to_broker = True
    return acct, action.execute()


def submitted(acct):
    assert acct.submitted, "nothing reached the broker seam"
    return acct.submitted[-1]


# ======================================================================================
# 1. THE STRUCTURE — two legs, two expiries, one order
# ======================================================================================
def test_the_builder_opens_a_leaps_and_an_overlay_as_ONE_structure():
    acct, res = run()
    assert res["success"], res["message"]
    parent, legs, children = submitted(acct)

    assert parent.option_strategy == PMCC_STRATEGY
    long_leg, short_leg = legs
    assert (long_leg.side, long_leg.position_intent) == (OrderDirection.BUY, "buy_to_open")
    assert (long_leg.strike, long_leg.expiry) == (80.0, LEAPS_EXPIRY)
    assert (short_leg.side, short_leg.position_intent) == (OrderDirection.SELL, "sell_to_open")
    assert (short_leg.strike, short_leg.expiry) == (110.0, OVERLAY_EXPIRY)
    # ONE order: one parent, two children, one broker call.
    assert len(acct.submitted) == 1
    assert len(children) == 2


def test_each_leg_is_persisted_with_its_OWN_expiry():
    """The Task 6-PRE storage, used in anger for the first time: the child row IS the leg."""
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)

    from ba2_common.core.trade_store import orders_where
    children = [o for o in orders_where(transaction_id=parent.transaction_id)
                if o.parent_order_id == parent.id]
    assert {c.expiry for c in children} == {LEAPS_EXPIRY, OVERLAY_EXPIRY}
    by_side = {c.side: c for c in children}
    assert by_side[OrderDirection.BUY].expiry == LEAPS_EXPIRY
    assert by_side[OrderDirection.SELL].expiry == OVERLAY_EXPIRY


def test_the_structure_level_expiry_stays_NULL_because_no_single_date_is_true():
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    from ba2_common.core.db import get_instance
    assert parent.expiry is None
    assert get_instance(Transaction, parent.transaction_id).expiry is None


def test_the_net_is_a_DEBIT_quoted_at_the_two_touches():
    """Buy the LEAPS at its ASK (20.00), sell the overlay at its BID (3.00) — the touch every
    other builder quotes at. Swapping either side flatters the entry by a whole spread."""
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    assert parent.limit_price == pytest.approx(NET_DEBIT)
    assert res["data"]["limit_price"] == pytest.approx(NET_DEBIT)


# ======================================================================================
# 2. THE INTRINSIC FLOOR — design §3, hand-derived
# ======================================================================================
def test_the_max_loss_is_the_net_debit_and_it_is_MEASURED():
    """LEAPS 20.00 − overlay 3.00 = 17.00 debit → $1,700 per contract.

    Derived by hand from the payoff, not from the builder: at an underlying of ZERO both
    calls are worthless and the whole debit is gone; at the LEAPS strike (80) the position is
    still worth nothing; above the overlay strike (110) the spread is worth its width. So the
    worst point is the debit, which is exactly design §3's "LEAPS debit − net credits" with
    no credits yet collected.
    """
    acct, res = run()
    assert res["success"], res["message"]
    assert res["data"]["max_loss_per_contract"] == pytest.approx(MAX_LOSS)

    _, legs, _ = submitted(acct)
    assert _measured_max_loss_per_contract(legs, NET_DEBIT) == pytest.approx(MAX_LOSS)


def test_the_max_loss_is_stamped_on_the_entry_order_row():
    """``opt_sl_ml`` reads this number BACK off the parent order, and the roll REWRITES it.
    A builder that submits without it silently disarms the stop for the position's whole life."""
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    from ba2_common.core.db import get_instance
    stored = get_instance(TradingOrder, parent.id)
    assert stored.data["max_loss_per_contract"] == pytest.approx(MAX_LOSS)


def test_the_worst_case_really_is_at_zero_and_the_upside_is_the_width():
    from ba2_common.core.TradeActions import _entry_payoff_legs
    from ba2_common.core.option_payoff import payoff_at, upside_slope

    acct, res = run()
    assert res["success"], res["message"]
    _, legs, _ = submitted(acct)
    payoff_legs = _entry_payoff_legs(legs, NET_DEBIT)

    assert payoff_at(payoff_legs, 0.0) == pytest.approx(-MAX_LOSS)
    assert payoff_at(payoff_legs, 80.0) == pytest.approx(-MAX_LOSS)
    # (110 - 80) x 100 - 1700 = +1300 above the short strike.
    assert payoff_at(payoff_legs, 110.0) == pytest.approx(1300.0)
    # And the upside slope is ZERO — the short caps it, which is why this is not UNBOUNDED.
    assert upside_slope(payoff_legs) == pytest.approx(0.0)


def test_the_structure_is_charged_COVERED_and_never_to_the_naked_sub_cap():
    """The cover is an OPTION, inside the order's own legs — so no ``stock_cover_price`` is
    needed and none is passed. ``structure_metrics`` pairs the short call with a long of the
    same right; the rails then charge the deployment cap, not ``undefined_risk_max_pct``."""
    from ba2_common.core.OptionRiskManagement import candidate_from_entry
    from ba2_common.core.option_book import _is_undefined_risk

    acct, res = run()
    assert res["success"], res["message"]
    _, legs, _ = submitted(acct)

    candidate = candidate_from_entry(
        underlying="XYZ", option_strategy=PMCC_STRATEGY, legs=legs, quantity=2,
        max_loss_per_contract=MAX_LOSS)
    assert candidate.is_defined_risk is True
    assert candidate.max_loss == pytest.approx(2 * MAX_LOSS)
    assert _is_undefined_risk(candidate) is False


# ======================================================================================
# 3. ADMISSION — a refusal, not a different structure
# ======================================================================================
def test_a_short_strike_at_or_below_the_leaps_strike_is_REFUSED():
    """70 < 80: that is a bear call spread. Its worst case is not the debit paid, so
    admitting it would put a WRONG number in the stamp the whole design rests on."""
    acct = PMCCAccount(overlay_rows=LOW_OVERLAY_ROWS)
    acct, res = run(acct=acct)

    assert not res["success"]
    assert PMCC_ADMISSION_REFUSAL in res["message"]
    assert "70" in res["message"] and "80" in res["message"]
    assert acct.submitted == [], "a refused structure must never reach the broker"
    from ba2_common.core.trade_store import orders_where
    assert orders_where(account_id=1) == [], "a refused structure left order rows behind"


def _contract(strike, expiry):
    tag = "XXXXXXXX" if strike is None else f"{int(strike * 1000):08d}"
    return OptionContract(symbol=f"XYZ{expiry:%y%m%d}C{tag}",
                          underlying="XYZ", option_type=OptionRight.CALL, strike=strike,
                          expiry=expiry, bid=1.0, ask=1.2, last=1.0, delta=0.2)


def _action():
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    return create_action(ExpertActionType.OPEN_PMCC, "XYZ", PMCCAccount(),
                         SimpleNamespace(), None, rec, **BASE)


def test_a_short_that_OUTLIVES_the_long_is_REFUSED():
    """A "diagonal" whose short expires after its long is a naked call with a decoration:
    once the long is gone the short is uncovered, and the intrinsic floor stops being a bound.

    Asserted on ``_admission_refusal`` DIRECTLY, and that is the honest shape. Two independent
    guards stand in front of this clause — ``_overlay_window`` refuses a window that is not
    strictly nearer (the test above), and ``select_single`` then filters the fetched chain by
    the same window — so no fixture can reach it through ``execute()`` without disabling the
    very machinery the entry depends on. It stays as the last line because both of those are
    configuration-shaped, and a provider that ignores a requested window is a real failure
    mode this codebase has already met more than once.
    """
    action = _action()
    refusal = action._admission_refusal(
        _contract(80.0, LEAPS_EXPIRY), _contract(110.0, LEAPS_EXPIRY + timedelta(days=30)))

    assert refusal is not None and not refusal["success"]
    assert PMCC_ADMISSION_REFUSAL in refusal["message"]
    assert "outlive" in refusal["message"]


def test_a_short_and_a_long_on_the_SAME_expiry_are_REFUSED():
    """Equal is not nearer. Two calls at one expiry with the short above the long is a bull
    call spread — a perfectly good structure, but not this one, and its risk is managed by a
    different set of rules."""
    action = _action()
    refusal = action._admission_refusal(
        _contract(80.0, LEAPS_EXPIRY), _contract(110.0, LEAPS_EXPIRY))

    assert refusal is not None and PMCC_ADMISSION_REFUSAL in refusal["message"]


@pytest.mark.parametrize("long_c,short_c", [
    (_contract(None, LEAPS_EXPIRY), _contract(110.0, OVERLAY_EXPIRY)),
    (_contract(80.0, LEAPS_EXPIRY), _contract(None, OVERLAY_EXPIRY)),
])
def test_a_leg_with_no_strike_is_UNKNOWN_and_refuses_rather_than_comparing(long_c, short_c):
    """``None > 80`` is a TypeError in Python 3 and a silent admission in any language that
    coerces it. Unknown is never a value."""
    refusal = _action()._admission_refusal(long_c, short_c)
    assert refusal is not None and PMCC_ADMISSION_REFUSAL in refusal["message"]


def test_an_overlay_window_that_is_not_nearer_than_the_leaps_window_is_a_CONFIG_error():
    """Refused at the WINDOW, before any fetch — the knob is named instead of the chain
    being blamed for a structure the configuration could never produce."""
    acct, res = run(short_dte_min=380, short_dte_max=470)
    assert not res["success"]
    assert "strictly nearer" in res["message"]
    assert acct.submitted == []


@pytest.mark.parametrize("lo,hi", [(None, 45), (30, None), (45, 30)])
def test_an_unusable_overlay_window_is_named_rather_than_read_as_a_thin_chain(lo, hi):
    acct, res = run(short_dte_min=lo, short_dte_max=hi)
    assert not res["success"]
    assert "overlay DTE window" in res["message"]
    assert acct.submitted == []


# ======================================================================================
# 4. THE GUARD — declared admits, undeclared still refuses
# ======================================================================================
def test_the_submit_guard_admits_the_declared_two_expiry_structure():
    """The relaxation Task 6-PRE opened, walked through for the first time."""
    assert is_multi_expiry_strategy(PMCC_STRATEGY)
    assert PMCC_STRATEGY in MULTI_EXPIRY_OPTION_STRATEGIES
    acct, res = run()
    assert res["success"], res["message"]


def test_the_SAME_legs_under_an_undeclared_tag_are_still_refused():
    """The relaxation is opt-in and narrow: it is the STRATEGY that is declared, never the
    leg shape. The identical two legs submitted under any other tag still raise."""
    acct, res = run()
    assert res["success"], res["message"]
    _, legs, _ = submitted(acct)

    other = PMCCAccount()
    with pytest.raises(ValueError, match="single expiry"):
        other.submit_option_order(legs, quantity=1, order_type="limit",
                                  limit_price=NET_DEBIT, option_strategy="diagonal_spread")
    assert other.submitted == []


# ======================================================================================
# 5. THE OVERLAY SPEC RIDES THE ROW
# ======================================================================================
def test_the_overlay_selection_spec_is_persisted_on_the_entry_order():
    """The roll happens weeks later, with a DIFFERENT recommendation in hand. The order row
    is the only thing that travels with the position — the same reason the earnings event
    date rides it."""
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    from ba2_common.core.db import get_instance
    spec = get_instance(TradingOrder, parent.id).data[ORDER_PMCC_OVERLAY_KEY]

    assert set(spec) == set(PMCC_OVERLAY_SPEC_KEYS)
    assert spec["strike_method"] == "delta"
    assert spec["strike_param"] == 0.20, "the SHORT half of the per-leg pair, not the LEAPS'"
    assert (spec["dte_min"], spec["dte_max"]) == (30, 45)


def test_the_spec_reaches_the_TradeActionResult_too_but_the_ROW_is_what_matters():
    """The whitelist trap, closed by construction: a fact written into ``data`` and missing
    from the persisted set reaches every log and never reaches the order."""
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    from ba2_common.core.db import get_instance
    assert ORDER_PMCC_OVERLAY_KEY in res["data"]
    assert ORDER_PMCC_OVERLAY_KEY in (get_instance(TradingOrder, parent.id).data or {})


# ======================================================================================
# 6. SIZING
# ======================================================================================
def test_sizing_divides_the_budget_by_the_net_debit():
    """1 % of $1,000,000 = $10,000; one structure costs the 17.00 debit x 100 = $1,700;
    floor(10000 / 1700) = 5."""
    acct, res = run()
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    assert parent.quantity == 5


def test_a_budget_below_one_structure_refuses_rather_than_rounding_up():
    acct, res = run(acct=PMCCAccount(balance=100_000.0), sizing=1.0)
    # 1 % of 100,000 = 1,000 < 1,700.
    assert not res["success"]
    assert "Insufficient budget" in res["message"]
    assert acct.submitted == []


# ======================================================================================
# 7. REGISTRY — a new action that is not registered is a new action that never runs
# ======================================================================================
def test_open_pmcc_is_a_registered_option_action_that_honours_the_strike_method():
    assert PMCC in get_option_action_values()
    assert is_option_action(PMCC)
    # Both legs are delta picks (design §2 states both targets in delta), so the method knob
    # is real here and must be offered.
    assert honours_strike_method(PMCC)


def test_create_action_builds_the_pmcc_class():
    from ba2_common.core.TradeActions import OpenPMCCAction
    action = create_action(ExpertActionType.OPEN_PMCC, "XYZ", PMCCAccount(),
                           SimpleNamespace(), None, None, **BASE)
    assert isinstance(action, OpenPMCCAction)
    assert action._action_type_value() == PMCC
    assert (action.short_dte_min, action.short_dte_max) == (30, 45)


def test_the_action_is_documented():
    from ba2_common.core.rules_documentation import get_action_type_documentation
    doc = get_action_type_documentation()[PMCC]
    assert doc["name"] and doc["description"] and doc["example"]
    assert isinstance(doc["use_cases"], list) and doc["use_cases"]
