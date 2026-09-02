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
from datetime import date, datetime, timedelta, timezone
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

#: The NEXT overlay, one cycle out — what a roll 35 days later selects from the SAME
#: [30, 45] DTE window the entry recorded.
ROLL_EXPIRY = TODAY + timedelta(days=70)
ROLL_ROWS = {
    108.0: (4.20, 4.40, 0.30),
    112.0: (2.50, 2.70, 0.20),      # <- the replacement overlay, sold at the BID = 2.50
    122.0: (0.30, 0.40, 0.10),
}
#: The day the roll is taken: 35 days on, so the entry's own [30, 45] window now points at
#: ROLL_EXPIRY (70 days from TODAY = 35 from here) and NOT at the overlay being replaced.
ROLL_DAY = TODAY + timedelta(days=35)
#: What the expiring overlay costs to buy back on ROLL_DAY. 3.00 collected, 0.50 to close:
#: 83.33 % of its credit has decayed, and the roll banks a further 2.50 - 0.50 = 2.00.
OLD_OVERLAY_BUYBACK = 0.50

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
                 deltas=True, roll_rows=None):
        self.id = 1
        self._balance = balance
        self._overlay_rows = overlay_rows if overlay_rows is not None else OVERLAY_ROWS
        self._overlay_expiry = overlay_expiry or OVERLAY_EXPIRY
        self._roll_rows = ROLL_ROWS if roll_rows is None else roll_rows
        self._deltas = deltas
        self.submitted = []
        #: The simulated clock. Advanced by the roll tests, because a roll that reruns the
        #: entry's own [30, 45] window on the SAME day would re-select the very contract it
        #: is replacing.
        self.today = TODAY
        #: ``{contract_symbol: OptionQuote}`` overrides. Everything else is priced off the
        #: chain tables, so only the contract whose price has MOVED needs a row here.
        self.quotes = {}

    # -- clock / prices ------------------------------------------------------
    def _as_of_date(self):
        return self.today

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
        """Whichever leg's window this fetch is asking for. The windows never overlap, which
        is the structure: the overlay expires long before the LEAPS, and its replacement one
        cycle after it."""
        for rows, expiry in ((LEAPS_ROWS, LEAPS_EXPIRY),
                             (self._overlay_rows, self._overlay_expiry),
                             (self._roll_rows, ROLL_EXPIRY)):
            if expiry is not None and expiry_min <= expiry <= expiry_max:
                return rows, expiry
        return {}, None

    def _contracts(self, underlying, rows, expiry):
        return [OptionContract(
            symbol=f"{underlying}{expiry:%y%m%d}C{int(strike * 1000):08d}",
            underlying=underlying, option_type=OptionRight.CALL, strike=strike,
            expiry=expiry, bid=bid, ask=ask, last=bid, open_interest=1000, volume=500,
            delta=(delta if self._deltas else None))
            for strike, (bid, ask, delta) in sorted(rows.items())]

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        rows, expiry = self._rows_for(expiry_min, expiry_max)
        return self._contracts(underlying, rows, expiry) if expiry is not None else []

    def _every_contract(self):
        out = {}
        for rows, expiry in ((LEAPS_ROWS, LEAPS_EXPIRY),
                             (self._overlay_rows, self._overlay_expiry),
                             (self._roll_rows, ROLL_EXPIRY)):
            if expiry is None:
                continue
            for c in self._contracts("XYZ", rows, expiry):
                out[c.symbol] = c
        return out

    def _fill_price(self, contract_symbol, side):
        """What one leg fills at: BUY pays the ask, SELL takes the bid. The double's stand-in
        for a broker, so the per-leg ``open_price`` rows the lifecycle reads are real prices
        rather than the structure's net smeared across the legs."""
        quote = self.quotes.get(contract_symbol) or self._every_contract().get(contract_symbol)
        if quote is None:
            return None
        return quote.ask if side == OrderDirection.BUY else quote.bid

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
            child.open_price = self._fill_price(child.contract_symbol, child.side)
            update_instance(child)
        return trading_order

    def get_option_quote(self, contract_symbol):
        return self.quotes.get(contract_symbol) or self._every_contract().get(contract_symbol)

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return self._balance

    # -- unused abstract bits ------------------------------------------------
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


# ======================================================================================
# 8. THE PURE LIFECYCLE FUNCTIONS — one implementation, two runtimes
# ======================================================================================
from ba2_common.core.option_lifecycle import (  # noqa: E402  (grouped with its own section)
    LIFECYCLE_HOLD, LIFECYCLE_ROLL_DTE, LIFECYCLE_ROLL_SHORT, LifecycleLeg, OptionStructure,
    PMCC_ROLL_STRATEGY, SETTING_PMCC_BUYBACK_PCT, close_legs_are_fail_closed,
    credit_decay_pct, decide, held_long_leg, held_short_leg, pmcc_roll_due,
    restamped_max_loss, roll_legs_are_fail_closed, roll_window_dte, uncovered_short_calls,
)
from ba2_common.core.option_types import OptionLeg  # noqa: E402


def _structure(strategy=PMCC_STRATEGY, short_expiry=OVERLAY_EXPIRY, short_credit=3.00,
               with_long=True, with_short=True):
    legs = []
    if with_long:
        legs.append(LifecycleLeg(contract_symbol="LONG", net_qty=1.0, strike=80.0,
                                 option_type=OptionRight.CALL, expiry=LEAPS_EXPIRY,
                                 entry_premium=20.00))
    if with_short:
        legs.append(LifecycleLeg(contract_symbol="SHORT", net_qty=-1.0, strike=110.0,
                                 option_type=OptionRight.CALL, expiry=short_expiry,
                                 entry_premium=short_credit))
    return OptionStructure(transaction_id=1, underlying="XYZ", strategy=strategy, legs=legs,
                           quantity=1.0, multiplier=100, entry_net_premium=NET_DEBIT)


@pytest.mark.parametrize("credit,ask,expected", [
    (3.00, 0.50, 83.3333),      # most of it gone -- the buyback case
    (3.00, 3.00, 0.0),          # sold a moment ago
    (3.00, 0.00, 100.0),        # worthless
    (3.00, 4.50, -50.0),        # gone AGAINST us: costs more to close than it brought in
])
def test_credit_decay_is_measured_from_the_legs_own_credit(credit, ask, expected):
    assert credit_decay_pct(credit, ask) == pytest.approx(expected)


@pytest.mark.parametrize("credit,ask", [
    (None, 0.5), (3.0, None), (0.0, 0.5), (-1.0, 0.5), (3.0, -0.1), ("3", None),
])
def test_an_unmeasurable_credit_decay_is_NONE_and_never_a_hundred(credit, ask):
    """An overlay sold for nothing has no credit to decay: the percentage is UNDEFINED, not
    100. Reading it as 100 would roll every position whose entry price we failed to record."""
    assert credit_decay_pct(credit, ask) is None


def test_the_restamp_lowers_the_floor_by_the_credit_banked():
    """Design §3: max loss = LEAPS debit MINUS net credits. A roll that nets a 2.00 credit
    (limit -2.00, the MLEG sign convention) takes $200 off the floor."""
    assert restamped_max_loss(1700.0, -2.00) == pytest.approx(1500.0)


def test_a_roll_that_COSTS_more_than_it_collects_raises_the_floor():
    """The same expression, the other sign. A roll is not guaranteed to be a credit, and
    pretending it is would understate the risk of exactly the positions that went wrong."""
    assert restamped_max_loss(1700.0, +0.50) == pytest.approx(1750.0)


def test_the_restamp_clamps_at_zero_rather_than_reporting_a_negative_risk():
    """Once the accrued credits have paid for the LEAPS there is no defined loss left, and
    ``loss_pct_of_max_loss`` self-disarms on a non-positive stamp. A structure that cannot
    lose has no loss-as-a-percentage-of-its-loss."""
    assert restamped_max_loss(1700.0, -20.00) == 0.0


@pytest.mark.parametrize("previous,net", [(None, -1.0), (1700.0, None), ("x", -1.0)])
def test_an_unmeasurable_restamp_leaves_the_stamp_ALONE(previous, net):
    assert restamped_max_loss(previous, net) is None


def test_the_invariant_reads_a_covered_short_as_covered():
    assert uncovered_short_calls(_structure().held_legs) == ()


def test_the_invariant_NAMES_a_short_whose_long_is_gone():
    legs = _structure(with_long=False).held_legs
    assert uncovered_short_calls(legs) == ("SHORT",)


def test_the_invariant_catches_TWO_shorts_against_one_long():
    """The roll's own failure mode: a second overlay written before the first was closed."""
    legs = list(_structure().held_legs) + [
        LifecycleLeg(contract_symbol="SHORT2", net_qty=-1.0, strike=112.0,
                     option_type=OptionRight.CALL, expiry=ROLL_EXPIRY)]
    assert uncovered_short_calls(legs) == ("SHORT", "SHORT2")


def test_the_invariant_does_not_police_short_PUTS():
    """A short put is bounded below (strike minus credit, at an underlying of zero), so it is
    a risk this codebase MEASURES rather than an invariant it forbids. Folding it in here
    would refuse every cash-secured put ever written."""
    legs = [LifecycleLeg(contract_symbol="P", net_qty=-1.0, strike=90.0,
                         option_type=OptionRight.PUT, expiry=OVERLAY_EXPIRY)]
    assert uncovered_short_calls(legs) == ()


def _close_legs(short_first=True):
    buy = OptionLeg(contract_symbol="SHORT", side=OrderDirection.BUY,
                    position_intent="buy_to_close", option_type=OptionRight.CALL)
    sell = OptionLeg(contract_symbol="LONG", side=OrderDirection.SELL,
                     position_intent="sell_to_close", option_type=OptionRight.CALL)
    return [buy, sell] if short_first else [sell, buy]


def test_a_close_that_buys_the_short_back_first_is_fail_closed():
    assert close_legs_are_fail_closed(_close_legs(short_first=True)) is None


def test_a_close_that_releases_the_LONG_first_is_refused():
    """THE MUTATION THIS KILLS. The long IS the cover; letting it go first describes a moment
    at which the position holds a naked short call."""
    why = close_legs_are_fail_closed(_close_legs(short_first=False))
    assert why is not None and "naked short call" in why


def _roll_legs(close_first=True):
    buy = OptionLeg(contract_symbol="SHORT", side=OrderDirection.BUY,
                    position_intent="buy_to_close", option_type=OptionRight.CALL)
    sell = OptionLeg(contract_symbol="SHORT2", side=OrderDirection.SELL,
                     position_intent="sell_to_open", option_type=OptionRight.CALL)
    return [buy, sell] if close_first else [sell, buy]


def test_a_roll_that_closes_before_it_opens_is_fail_closed():
    assert roll_legs_are_fail_closed(_roll_legs(close_first=True)) is None


def test_a_roll_that_OPENS_the_new_short_first_is_refused():
    """THE OTHER MUTATION. Written that way round, the ticket describes a position owing two
    overlays against one long."""
    why = roll_legs_are_fail_closed(_roll_legs(close_first=False))
    assert why is not None and "buy back" in why


def test_a_roll_ticket_is_exactly_two_legs():
    assert "exactly two legs" in (roll_legs_are_fail_closed(_roll_legs()[:1]) or "")


def test_the_named_legs_are_the_nearest_short_and_the_farthest_long():
    s = _structure()
    assert held_short_leg(s).contract_symbol == "SHORT"
    assert held_long_leg(s).contract_symbol == "LONG"


def test_the_roll_window_reads_the_SHORT_leg_even_though_the_long_lives_a_year():
    """The Task 6-PRE rule, exercised by the roll for the first time: reading the LEAPS here
    would put the roll a year out and the roll would never fire."""
    days, blind = roll_window_dte(_structure(), TODAY)
    assert blind == ""
    assert days == (OVERLAY_EXPIRY - TODAY).days == 39


# ======================================================================================
# 9. decide() — the LIVE pass rolls the overlay instead of closing the structure
# ======================================================================================
_LIFECYCLE_SETTINGS = {
    "profit_capture_pct": 1000.0, "roll_dte": 5, "tested_delta_enabled": False,
    "dr_stop_enabled": False, "ur_stop_enabled": False,
}


def _chain_row(symbol, bid, ask, delta=0.2, strike=110.0, expiry=None):
    return OptionContract(symbol=symbol, underlying="XYZ", option_type=OptionRight.CALL,
                          strike=strike, expiry=expiry or OVERLAY_EXPIRY, bid=bid, ask=ask,
                          last=bid, delta=delta)


def test_a_declared_two_expiry_structure_ROLLS_the_overlay_instead_of_closing():
    """THE HEADLINE. ``_dte`` reads the SHORT leg, so without this branch every PMCC would be
    CLOSED the moment its overlay came inside roll_dte — a LEAPS with a year left, thrown away
    on schedule, and filed under "roll"."""
    structure = _structure(short_expiry=TODAY + timedelta(days=3))
    chain = {"LONG": _chain_row("LONG", 20.0, 20.2, 0.8),
             "SHORT": _chain_row("SHORT", 0.4, 0.5)}
    decision = decide([structure], chain, _LIFECYCLE_SETTINGS, TODAY)[0]

    assert decision.reason == LIFECYCLE_ROLL_SHORT
    assert not decision.should_close, "rolling the overlay must NEVER close the structure"
    assert "roll_dte" in decision.detail


def test_a_SINGLE_expiry_structure_still_closes_on_the_same_input():
    """The other half: nothing about the pre-existing roll-at-DTE close moved. The same two
    legs on ONE date under an undeclared strategy still produce a CLOSING decision."""
    one_date = TODAY + timedelta(days=3)
    structure = OptionStructure(
        transaction_id=1, underlying="XYZ", strategy="bull_call_spread",
        legs=[LifecycleLeg(contract_symbol="LONG", net_qty=1.0, strike=80.0,
                           option_type=OptionRight.CALL, expiry=one_date),
              LifecycleLeg(contract_symbol="SHORT", net_qty=-1.0, strike=110.0,
                           option_type=OptionRight.CALL, expiry=one_date)],
        quantity=1.0, multiplier=100, entry_net_premium=NET_DEBIT)
    chain = {"LONG": _chain_row("LONG", 20.0, 20.2, 0.8),
             "SHORT": _chain_row("SHORT", 0.4, 0.5)}
    decision = decide([structure], chain, _LIFECYCLE_SETTINGS, TODAY)[0]

    assert decision.reason == LIFECYCLE_ROLL_DTE
    assert decision.should_close


def test_the_buyback_trigger_rolls_an_overlay_that_is_nowhere_near_expiry():
    """Design §4's second trigger. 3.00 collected, 0.50 to buy back = 83.33 % decayed, with
    36 days still to run."""
    structure = _structure()
    chain = {"LONG": _chain_row("LONG", 20.0, 20.2, 0.8),
             "SHORT": _chain_row("SHORT", 0.4, OLD_OVERLAY_BUYBACK)}
    settings = {**_LIFECYCLE_SETTINGS, SETTING_PMCC_BUYBACK_PCT: 70.0}
    decision = decide([structure], chain, settings, TODAY)[0]

    assert decision.reason == LIFECYCLE_ROLL_SHORT
    assert "83.33% decayed" in decision.detail


def test_an_ABSENT_buyback_setting_is_OFF_and_never_zero():
    """A missing trigger must not read as "0 % decayed", which every overlay satisfies the
    instant it is sold."""
    structure = _structure()
    chain = {"LONG": _chain_row("LONG", 20.0, 20.2, 0.8),
             "SHORT": _chain_row("SHORT", 0.4, OLD_OVERLAY_BUYBACK)}
    decision = decide([structure], chain, _LIFECYCLE_SETTINGS, TODAY)[0]
    assert decision.reason == LIFECYCLE_HOLD


def test_the_roll_reason_is_not_a_closing_reason():
    from ba2_common.core.option_lifecycle import LIFECYCLE_CLOSING_REASONS
    assert LIFECYCLE_ROLL_SHORT not in LIFECYCLE_CLOSING_REASONS


def test_pmcc_roll_due_prefers_the_expiry_window_over_the_buyback_trigger():
    """Both true at once; the recorded reason is the one that would have fired anyway, so a
    roll is never attributed to a trigger that merely happened to agree."""
    structure = _structure(short_expiry=TODAY + timedelta(days=2))
    chain = {"SHORT": _chain_row("SHORT", 0.4, OLD_OVERLAY_BUYBACK)}
    due, detail, blind = pmcc_roll_due(structure, chain, TODAY, roll_dte=5, buyback_pct=70.0)
    assert due and "roll_dte" in detail and blind == ""


# ======================================================================================
# 10. THE ROLL, END TO END — buy the overlay back, sell the next, keep the LEAPS
# ======================================================================================
from ba2_common.core.option_types import OptionQuote  # noqa: E402

ROLL = ExpertActionType.ROLL_PMCC_SHORT.value
OLD_SHORT = f"XYZ{OVERLAY_EXPIRY:%y%m%d}C{int(110.0 * 1000):08d}"
NEW_SHORT = f"XYZ{ROLL_EXPIRY:%y%m%d}C{int(112.0 * 1000):08d}"
LONG_LEAPS = f"XYZ{LEAPS_EXPIRY:%y%m%d}C{int(80.0 * 1000):08d}"
#: 1 x (0.50 buy-back - 2.50 re-sale) = -2.00, a net CREDIT under the house MLEG convention.
ROLL_NET = -2.00
#: 1700 + (-2.00 x 100). Design §3's "restamped as credits accrue", one term later.
ROLLED_MAX_LOSS = 1500.0


def _open(acct=None):
    """Open the PMCC and hand back ``(account, entry parent order)``."""
    acct, res = run(acct=acct)
    assert res["success"], res["message"]
    parent, _, _ = submitted(acct)
    return acct, parent


def _arrive_at_roll_day(acct):
    """Advance the clock and decay the overlay: 3.00 collected, 0.50 to buy back."""
    acct.today = ROLL_DAY
    acct.quotes[OLD_SHORT] = OptionQuote(symbol=OLD_SHORT, bid=0.40,
                                         ask=OLD_OVERLAY_BUYBACK, last=0.45, delta=0.05)
    return acct


def _roll(acct, parent):
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    action = create_action(ExpertActionType.ROLL_PMCC_SHORT, "XYZ", acct, SimpleNamespace(),
                           parent, rec)
    action.submit_to_broker = True
    return action.execute()


def _held(transaction_id):
    """``{contract_symbol: signed net contracts}`` over the transaction's executed legs."""
    from ba2_common.core.trade_store import orders_where
    from ba2_common.core.types import AssetClass
    executed = OrderStatus.get_executed_statuses()
    out = {}
    for o in orders_where(transaction_id=transaction_id):
        if o.asset_class != AssetClass.OPTION or not o.contract_symbol:
            continue
        if o.status not in executed:
            continue
        q = float(o.filled_qty or o.quantity or 0.0)
        out[o.contract_symbol] = out.get(o.contract_symbol, 0.0) + (
            q if o.side == OrderDirection.BUY else -q)
    return {k: v for k, v in out.items() if abs(v) > 1e-9}


def test_the_roll_replaces_the_overlay_and_leaves_the_leaps_alone():
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)

    assert res["success"], res["message"]
    assert res["data"]["closed_contract"] == OLD_SHORT
    assert res["data"]["opened_contract"] == NEW_SHORT
    held = _held(parent.transaction_id)
    assert OLD_SHORT not in held, "the expiring overlay is flat"
    assert held[NEW_SHORT] == pytest.approx(-5.0), "the next overlay is short, 5 structures"
    assert held[LONG_LEAPS] == pytest.approx(+5.0), "the LEAPS was never touched"


def test_the_roll_is_ONE_order_on_the_SAME_transaction():
    """Structure identity survives the roll: same transaction, same entry parent, so every
    later bar's ``existing_order`` still resolves to the row the stamps live on."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    before = len(acct.submitted)
    res = _roll(acct, parent)

    assert res["success"], res["message"]
    assert len(acct.submitted) == before + 1, "a roll is exactly one broker ticket"
    roll_parent, roll_legs, _ = submitted(acct)
    assert roll_parent.transaction_id == parent.transaction_id
    assert roll_parent.option_strategy == PMCC_ROLL_STRATEGY
    assert len(roll_legs) == 2


def test_the_roll_ticket_closes_BEFORE_it_opens():
    """The fail-closed ordering, on the legs actually sent."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]
    _, roll_legs, _ = submitted(acct)

    assert roll_legs[0].position_intent == "buy_to_close"
    assert roll_legs[0].contract_symbol == OLD_SHORT
    assert roll_legs[1].position_intent == "sell_to_open"
    assert roll_legs[1].contract_symbol == NEW_SHORT
    assert roll_legs_are_fail_closed(roll_legs) is None


def test_the_roll_prices_at_both_crossing_touches():
    """Buy the old back at its ASK, sell the new at its BID. Quoting either at the mid would
    flatter every roll for the life of the position."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)
    assert res["success"], res["message"]
    assert res["data"]["limit_price"] == pytest.approx(ROLL_NET)


def test_the_roll_RESTAMPS_the_max_loss_on_the_entry_order():
    """Design §3: the intrinsic floor is the LEAPS debit less EVERY credit collected since.
    ``loss_pct_of_max_loss`` divides by this row; leaving it at 1700 would make the stop
    progressively too loose as credits came in."""
    from ba2_common.core.db import get_instance

    acct, parent = _open()
    assert get_instance(TradingOrder, parent.id).data["max_loss_per_contract"] == \
        pytest.approx(MAX_LOSS)
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)

    assert res["success"], res["message"]
    assert res["data"]["max_loss_per_contract"] == pytest.approx(ROLLED_MAX_LOSS)
    assert get_instance(TradingOrder, parent.id).data["max_loss_per_contract"] == \
        pytest.approx(ROLLED_MAX_LOSS)


def test_the_roll_leaves_the_overlay_SPEC_and_the_transaction_intent_untouched():
    """A roll is maintenance, not a redefinition: the position is still a pmcc, and the next
    roll reads the same box this one did."""
    from ba2_common.core.db import get_instance

    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]

    stored = get_instance(TradingOrder, parent.id)
    assert stored.data[ORDER_PMCC_OVERLAY_KEY]["strike_param"] == 0.20
    assert get_instance(Transaction, parent.transaction_id).option_strategy == PMCC_STRATEGY


def test_the_new_overlay_is_selected_from_the_ENTRYs_recorded_box():
    """0.20 delta, 30-45 DTE — the entry's own numbers, not this action's (it has none)."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]
    _, roll_legs, _ = submitted(acct)

    assert roll_legs[1].strike == 112.0, "the 0.20-delta row of the replacement chain"
    assert roll_legs[1].expiry == ROLL_EXPIRY
    assert (ROLL_EXPIRY - ROLL_DAY).days == 35, "inside the recorded [30, 45] window"


# ======================================================================================
# 11. THE INVARIANT — every failure point, and the engine never holds the short alone
# ======================================================================================
def test_a_roll_already_in_flight_is_never_stacked():
    """The rule fires on every bar its trigger holds. Without this the second bar would write
    a SECOND overlay behind the first, and the position would owe two shorts against one long
    — the invariant, broken by an action that is individually correct."""
    from ba2_common.core.db import update_instance
    from ba2_common.core.trade_store import orders_where

    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]
    # The roll's own rows go back to "still working at the broker", which is what they look
    # like on the very next bar under a next-bar fill model.
    for o in orders_where(transaction_id=parent.transaction_id):
        if o.option_strategy == PMCC_ROLL_STRATEGY or o.contract_symbol == NEW_SHORT:
            o.status = OrderStatus.ACCEPTED
            update_instance(o)
    before = len(acct.submitted)
    res = _roll(acct, parent)

    assert not res["success"]
    assert "still working" in res["message"]
    assert len(acct.submitted) == before, "no second overlay reached the broker"


def test_a_roll_with_no_replacement_contract_leaves_the_LONG_ALONE():
    """The SAFE failure, and it is deliberately allowed to happen. An uncovered long costs
    theta; a second short is unbounded. When the chain cannot offer a replacement the roll
    refuses and nothing is closed — including the overlay it was going to buy back."""
    acct, parent = _open(PMCCAccount(roll_rows={}))
    _arrive_at_roll_day(acct)
    before = len(acct.submitted)
    res = _roll(acct, parent)

    assert not res["success"]
    assert "no option chain" in res["message"] or "no liquid replacement" in res["message"]
    assert len(acct.submitted) == before
    held = _held(parent.transaction_id)
    assert held[OLD_SHORT] == pytest.approx(-5.0), "the old overlay is still there"
    assert held[LONG_LEAPS] == pytest.approx(+5.0)


def test_a_replacement_at_or_below_the_LEAPS_strike_is_refused():
    """A roll onto a strike inside the long is a vertical, not a covered call, and its worst
    case is no longer the debit paid."""
    low = {60.0: (4.20, 4.40, 0.30), 70.0: (2.50, 2.70, 0.20), 75.0: (0.30, 0.40, 0.10)}
    acct, parent = _open(PMCCAccount(roll_rows=low))
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)

    assert not res["success"]
    assert "not above the long leg" in res["message"]
    assert _held(parent.transaction_id)[OLD_SHORT] == pytest.approx(-5.0)


def test_a_position_that_was_not_opened_as_a_pmcc_is_refused():
    """No overlay spec on the row means this action does not know what the next overlay
    should be — and guessing is the fabricated input this codebase refuses everywhere."""
    from ba2_common.core.db import get_instance, update_instance

    acct, parent = _open()
    stored = get_instance(TradingOrder, parent.id)
    stored.data = {k: v for k, v in (stored.data or {}).items()
                   if k != ORDER_PMCC_OVERLAY_KEY}
    update_instance(stored)
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)

    assert not res["success"]
    assert "no overlay spec" in res["message"]


def test_a_transaction_that_is_not_declared_two_expiry_is_refused():
    from ba2_common.core.db import get_instance, update_instance

    acct, parent = _open()
    txn = get_instance(Transaction, parent.transaction_id)
    txn.option_strategy = "bull_call_spread"
    update_instance(txn)
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)

    assert not res["success"]
    assert "not a declared two-expiry structure" in res["message"]


def test_an_unpriceable_roll_is_refused_rather_than_guessed():
    acct, parent = _open()
    acct.today = ROLL_DAY
    acct.quotes[OLD_SHORT] = OptionQuote(symbol=OLD_SHORT, bid=None, ask=None, last=None)
    res = _roll(acct, parent)

    assert not res["success"]
    assert "cannot be priced" in res["message"]


def test_the_close_after_a_roll_flattens_BOTH_legs_including_the_ROLLED_short():
    """THE ENUMERATION FIX, and the reason it is not cosmetic. The live overlay is a child of
    the ROLL order; the entry parent has never heard of it. Closing only what the entry named
    would sell the LEAPS and leave that overlay standing — a naked short call."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]

    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    close = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", acct, SimpleNamespace(),
                          parent, rec)
    close.submit_to_broker = True
    res = close.execute()

    assert res["success"], res["message"]
    assert set(res["data"]["contract_symbols"]) == {LONG_LEAPS, NEW_SHORT}
    assert OLD_SHORT not in res["data"]["contract_symbols"], (
        "already flat, never reversed twice")
    assert not _held(parent.transaction_id), "every leg is flat"


def test_the_close_of_a_two_expiry_structure_buys_the_SHORT_back_first():
    """The fail-closed ordering on the ticket the account actually receives."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]

    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    close = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", acct, SimpleNamespace(),
                          parent, rec)
    close.submit_to_broker = True
    assert close.execute()["success"]
    _, close_legs, _ = submitted(acct)

    assert close_legs[0].position_intent == "buy_to_close"
    assert close_legs[0].contract_symbol == NEW_SHORT
    assert close_legs[-1].position_intent == "sell_to_close"
    assert close_legs[-1].contract_symbol == LONG_LEAPS
    assert close_legs_are_fail_closed(close_legs) is None


def test_a_closing_ticket_that_would_STRAND_a_short_is_refused():
    """The backstop behind the enumeration, asked directly. Whatever produced a closing set
    that releases the long and leaves a short behind, the answer is a refusal: a structure
    that cannot be flattened safely stays open."""
    from ba2_common.core.TradeActions import CloseOptionAction

    long_only = [OptionLeg(contract_symbol=LONG_LEAPS, side=OrderDirection.SELL,
                           position_intent="sell_to_close", option_type=OptionRight.CALL)]
    txn_rows = [SimpleNamespace(contract_symbol=LONG_LEAPS, option_type=OptionRight.CALL),
                SimpleNamespace(contract_symbol=NEW_SHORT, option_type=OptionRight.CALL)]
    stranded = CloseOptionAction._uncovered_after(
        {LONG_LEAPS: 1.0, NEW_SHORT: -1.0}, long_only, txn_rows, 1)

    assert stranded == (NEW_SHORT,)


def test_a_complete_flatten_strands_nothing():
    from ba2_common.core.TradeActions import CloseOptionAction

    both = [OptionLeg(contract_symbol=NEW_SHORT, side=OrderDirection.BUY,
                      position_intent="buy_to_close", option_type=OptionRight.CALL),
            OptionLeg(contract_symbol=LONG_LEAPS, side=OrderDirection.SELL,
                      position_intent="sell_to_close", option_type=OptionRight.CALL)]
    txn_rows = [SimpleNamespace(contract_symbol=LONG_LEAPS, option_type=OptionRight.CALL),
                SimpleNamespace(contract_symbol=NEW_SHORT, option_type=OptionRight.CALL)]
    assert CloseOptionAction._uncovered_after(
        {LONG_LEAPS: 1.0, NEW_SHORT: -1.0}, both, txn_rows, 1) == ()


def test_the_close_of_a_pmcc_passes_the_submit_guard_under_the_close_tag():
    """The guard reads the declaration off the TRANSACTION. Without that a PMCC could be
    opened and never flattened — the close carries two expiries under the tag ``close``,
    which is not and must not become a declared multi-expiry strategy."""
    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]

    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    close = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", acct, SimpleNamespace(),
                          parent, rec)
    close.submit_to_broker = True
    assert close.execute()["success"]
    close_parent, _, _ = submitted(acct)
    assert close_parent.option_strategy == "close"
    assert not is_multi_expiry_strategy("close"), (
        "the relaxation must stay keyed on the position, never on the close tag")


def test_a_two_expiry_close_on_an_UNDECLARED_transaction_is_still_refused():
    """Fail-closed: the transaction's own tag is the declaration, and an undeclared one
    refuses exactly as it did before this task."""
    from ba2_common.core.db import get_instance, update_instance

    acct, parent = _open()
    _arrive_at_roll_day(acct)
    assert _roll(acct, parent)["success"]
    txn = get_instance(Transaction, parent.transaction_id)
    txn.option_strategy = "bull_call_spread"
    update_instance(txn)

    legs = [OptionLeg(contract_symbol=NEW_SHORT, side=OrderDirection.BUY,
                      position_intent="buy_to_close", option_type=OptionRight.CALL,
                      strike=112.0, expiry=ROLL_EXPIRY, underlying="XYZ"),
            OptionLeg(contract_symbol=LONG_LEAPS, side=OrderDirection.SELL,
                      position_intent="sell_to_close", option_type=OptionRight.CALL,
                      strike=80.0, expiry=LEAPS_EXPIRY, underlying="XYZ")]
    with pytest.raises(ValueError, match="single expiry"):
        acct.submit_option_order(legs, quantity=1, order_type="limit", limit_price=1.0,
                                 option_strategy="close",
                                 transaction_id=parent.transaction_id)


# ======================================================================================
# 12. PARITY — the backtest-shaped caller and the live-shaped caller ask ONE function
# ======================================================================================
def test_the_rule_reader_and_the_live_pass_read_the_SAME_roll_window():
    """``ShortLegDaysToExpiryCondition`` (the grid's roll trigger, evaluated by the backtest
    engine) and ``option_lifecycle.decide`` (the live exit pass) are two callers of ONE
    function. Both are run here against the SAME persisted position, and they must agree —
    not "look similar", agree, because a divergence is a PMCC that rolls on one runtime and
    not the other."""
    from ba2_common.core.OptionRiskManagement import build_structure
    from ba2_common.core.TradeConditions import create_condition
    from ba2_common.core.db import get_instance
    from ba2_common.core.types import ExpertEventType

    acct, parent = _open()
    acct.today = ROLL_DAY
    txn = get_instance(Transaction, parent.transaction_id)

    # The LIVE shape: a structure value built from the rows, asked directly.
    live_days, blind = roll_window_dte(build_structure(txn), ROLL_DAY)
    assert blind == ""

    # The BACKTEST shape: the rule-level condition, reading the same rows through the same
    # builder, with the as-of coming off the bar's recommendation.
    rec = SimpleNamespace(
        id=1, instance_id=None, data=None, price_at_date=None, expected_profit_percent=None,
        recommended_action=None,
        created_at=datetime(ROLL_DAY.year, ROLL_DAY.month, ROLL_DAY.day, tzinfo=timezone.utc))
    cond = create_condition(ExpertEventType.N_SHORT_LEG_DAYS_TO_EXPIRY, acct, "XYZ", rec,
                            existing_order=parent, operator_str="<=", value=10)
    fired = cond.evaluate()

    assert cond.calculated_value == live_days
    assert live_days == (OVERLAY_EXPIRY - ROLL_DAY).days == 4
    assert fired is True


def test_the_rule_reader_and_the_live_pass_read_the_SAME_credit_decay():
    from ba2_common.core.OptionRiskManagement import build_structure
    from ba2_common.core.TradeConditions import create_condition
    from ba2_common.core.db import get_instance
    from ba2_common.core.option_lifecycle import pmcc_credit_decay
    from ba2_common.core.types import ExpertEventType

    acct, parent = _open()
    _arrive_at_roll_day(acct)
    txn = get_instance(Transaction, parent.transaction_id)
    structure = build_structure(txn)

    live_pct, blind = pmcc_credit_decay(
        structure, {OLD_SHORT: acct.get_option_quote(OLD_SHORT)})
    assert blind == ""

    rec = SimpleNamespace(
        id=1, instance_id=None, data=None, price_at_date=None, expected_profit_percent=None,
        recommended_action=None,
        created_at=datetime(ROLL_DAY.year, ROLL_DAY.month, ROLL_DAY.day, tzinfo=timezone.utc))
    cond = create_condition(ExpertEventType.N_CREDIT_DECAYED_PCT, acct, "XYZ", rec,
                            existing_order=parent, operator_str=">=", value=70)

    assert cond.evaluate() is True
    assert cond.calculated_value == live_pct == pytest.approx(83.3333)


def test_the_long_leg_delta_condition_reads_the_LEAPS_not_the_overlay():
    from ba2_common.core.TradeConditions import create_condition
    from ba2_common.core.types import ExpertEventType

    acct, parent = _open()
    rec = SimpleNamespace(
        id=1, instance_id=None, data=None, price_at_date=None, expected_profit_percent=None,
        recommended_action=None,
        created_at=datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc))
    cond = create_condition(ExpertEventType.N_LONG_LEG_DELTA, acct, "XYZ", rec,
                            existing_order=parent, operator_str="<", value=0.50)

    assert cond.evaluate() is False, "an 0.80-delta LEAPS is nowhere near the floor"
    assert cond.calculated_value == pytest.approx(0.80), "the LEAPS, not the 0.20 overlay"


def test_the_delta_floor_fires_once_the_leaps_stops_tracking():
    from ba2_common.core.TradeConditions import create_condition
    from ba2_common.core.types import ExpertEventType

    acct, parent = _open()
    acct.quotes[LONG_LEAPS] = OptionQuote(symbol=LONG_LEAPS, bid=4.0, ask=4.2, last=4.0,
                                          delta=0.42)
    rec = SimpleNamespace(
        id=1, instance_id=None, data=None, price_at_date=None, expected_profit_percent=None,
        recommended_action=None,
        created_at=datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc))
    cond = create_condition(ExpertEventType.N_LONG_LEG_DELTA, acct, "XYZ", rec,
                            existing_order=parent, operator_str="<", value=0.50)

    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(0.42)


@pytest.mark.parametrize("event,op,value", [
    ("N_SHORT_LEG_DAYS_TO_EXPIRY", "<=", 10),
    ("N_CREDIT_DECAYED_PCT", ">=", 70),
    ("N_LONG_LEG_DELTA", "<", 0.50),
])
def test_an_unreadable_position_never_fires_any_of_the_three(event, op, value):
    """UNKNOWN NEVER FIRES. Two of these drive a ROLL, so a reader that answered a plausible
    number for a structure it could not see would roll every overlay on sight."""
    from ba2_common.core.TradeConditions import create_condition
    from ba2_common.core.types import ExpertEventType

    acct = PMCCAccount()
    rec = SimpleNamespace(
        id=1, instance_id=None, data=None, price_at_date=None, expected_profit_percent=None,
        recommended_action=None,
        created_at=datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc))
    cond = create_condition(getattr(ExpertEventType, event), acct, "XYZ", rec,
                            existing_order=None, operator_str=op, value=value)

    assert cond.evaluate() is False
    assert cond.calculated_value is None
    assert "unknown" in (cond.get_actual_value_display() or "")


@pytest.mark.parametrize("field", ["short_leg_days_to_expiry", "credit_decayed_pct",
                                   "long_leg_delta"])
def test_each_new_field_survives_the_shared_converter_into_a_trigger(field):
    """The ``rule_builders.FIELD_EVENT`` rule: a field missing there is SILENTLY DROPPED by
    ``triggers_from_condition_tree`` and the engine never sees the gate at all."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    triggers = triggers_from_condition_tree(
        {"type": "AND", "conditions": [
            {"id": "x", "field": field, "op": "<=", "value": 3}]})
    assert any(t.get("event_type") == field for t in triggers.values()), triggers


def test_the_roll_action_is_registered_and_documented():
    from ba2_common.core.TradeActions import RollPMCCShortAction
    from ba2_common.core.rules_documentation import get_action_type_documentation

    assert ROLL in get_option_action_values() and is_option_action(ROLL)
    # NOT on the strike-method registry: the roll re-selects from the spec the ENTRY stamped,
    # never from its own ``strike_method``, so offering it the knob would be a decoy.
    assert not honours_strike_method(ROLL)
    action = create_action(ExpertActionType.ROLL_PMCC_SHORT, "XYZ", PMCCAccount(),
                           SimpleNamespace(), None, None)
    assert isinstance(action, RollPMCCShortAction)
    doc = get_action_type_documentation()[ROLL]
    assert doc["name"] and doc["description"] and doc["use_cases"]
