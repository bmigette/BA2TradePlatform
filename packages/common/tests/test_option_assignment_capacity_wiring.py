"""Assignment capacity, WIRED INTO THE LIVE ENTRY PATH — in ENFORCING mode.

``2ce4efe`` built ``short_put_assignment_exposure()`` / ``check_assignment_capacity()``
and left them un-wired: *"no credit builder calls check_assignment_capacity yet ... the
live TradeActions entry path can still open a book it cannot take delivery of."*

The demonstrated failure, from that agent's probe::

    cash on hand: 45,000 -- assignment bill if all five fill: 50,000
    verdicts: [('AAA', True, 'ok'), ('BBB', True, 'ok'), ('CCC', True, 'ok'),
               ('DDD', True, 'ok'), ('EEE', True, 'ok')]

Five short puts, each individually within every limit, collectively unable to take
delivery. These tests drive the REAL ``TradeActions`` builders and require the fifth to
be refused.

WHY THE HEADLINE USES A STRUCTURE WHOSE RESERVE IS NOT THE STRIKE. For a plain
``cash_secured_put`` the reserve pool and the assignment bill are the SAME number
(``strike x 100``) measured against the same balance, so the pre-existing
buying-power gate already refuses the fifth and this gate adds nothing. The hole is in
every structure whose reserve is *cheaper* than delivery — ``short_straddle`` /
``short_strangle`` at Reg-T naked margin (~20% of notional per leg, both legs summed
since review 2026-08-30 F10) and ``iron_condor`` at its wing width. The headline
therefore uses five short straddles: each carries one short 100-strike put (a $10,000
delivery bill) but reserves only $4,000, so buying power says yes to all five while the
account holds $45,000 against a $50,000 bill.
"""
from datetime import date
from itertools import count
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.interfaces.OptionsAccountInterface import (
    ASSIGNMENT_CAPACITY_REFUSAL,
    OptionsAccountInterface,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import (
    AssetClass, ExpertActionType, OptionRight, OrderDirection, OrderStatus,
)

#: Frozen clock. Never "today": a test whose selection window drifts with the wall
#: clock passes and fails for reasons that have nothing to do with the gate.
TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 6, 21)

_ids = count(1)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows. Sibling DB-seam tests repoint the
    global DB seam at their own temp sqlite without restoring it, so re-point to a
    fresh, fully-initialized sqlite for each test here (order-independence)."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "capacity_wiring.sqlite"))
    db.init_db()
    yield


# ---------------------------------------------------------------------------
# order rows — the SHAPE the platform really writes
# ---------------------------------------------------------------------------
def _row(**kw):
    base = dict(id=next(_ids), account_id=1, symbol="XYZ", underlying_symbol="XYZ",
                quantity=None, filled_qty=None, side=None, status=OrderStatus.FILLED,
                asset_class=AssetClass.OPTION, multiplier=100, contract_symbol=None,
                option_type=None, strike=None, option_strategy=None,
                transaction_id=None, data={})
    base.update(kw)
    return SimpleNamespace(**base)


def held_structure(strategy, reserve, *legs, symbol="XYZ"):
    """A multi-leg parent (carries the RESERVE) plus its leg children (carry the
    CONTRACTS) — exactly how ``submit_option_order`` persists a spread, and exactly
    what makes the reserve pool and the capacity view two readings of ONE list."""
    rows = [_row(symbol=symbol, underlying_symbol=symbol, option_strategy=strategy,
                 data={"option_reserve": reserve})]
    for right, strike, qty, side in legs:
        rows.append(_row(
            symbol=f"{symbol}{EXPIRY:%y%m%d}{'C' if right == OptionRight.CALL else 'P'}"
                   f"{int(strike * 1000):08d}",
            underlying_symbol=symbol,
            contract_symbol=f"{symbol}{EXPIRY:%y%m%d}"
                            f"{'C' if right == OptionRight.CALL else 'P'}"
                            f"{int(strike * 1000):08d}",
            option_type=right, strike=strike, quantity=qty, filled_qty=qty, side=side))
    return rows


def held_short_put(strike=100.0, qty=1, reserve=4_000.0, strategy="short_straddle",
                   symbol="XYZ"):
    """One held structure carrying ONE short put (and, for a straddle, a short call)."""
    legs = [(OptionRight.PUT, strike, qty, OrderDirection.SELL)]
    if strategy == "short_straddle":
        legs.append((OptionRight.CALL, strike, qty, OrderDirection.SELL))
    return held_structure(strategy, reserve, *legs, symbol=symbol)


# ---------------------------------------------------------------------------
# the account
# ---------------------------------------------------------------------------
class FakeAccount(OptionsAccountInterface):
    """An options account with a CONTROLLED book.

    ``open_option_orders_book_wide`` is overridden (its DB query is already pinned by
    ``tests/test_option_assignment_capacity_account.py``); everything downstream of it
    — the reserve pool, the exposure, the capacity verdict — is the real code, reading
    the SAME list, which is what lets the no-double-charge property be tested honestly.
    """

    def __init__(self, spot=100.0, balance=45_000.0, book=None):
        self.id = 1
        self.spot = spot
        self._balance = balance
        self.book = list(book or [])
        self.submitted = []

    # --- book / cash
    def open_option_orders_book_wide(self):
        return list(self.book)

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        """The double's balance IS its CASH — the intent this file has always tested.

        Completed for OPT-L5: ``cash_available_for_delivery`` reads
        ``AccountSnapshot.cash`` and must never fall back to total equity, so a double
        that published only ``get_balance()`` left the delivery gate unmeasurable.
        """
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance, net_liquidation=self._balance)

    def hold(self, rows):
        self.book.extend(rows)

    # --- clock / price
    def _as_of_date(self):
        return TODAY

    def get_instrument_current_price(self, symbol, price_type=None):
        return self.spot

    def get_current_price(self, symbol=None):
        return self.spot

    # --- chain: 80..120 step 5, both rights, convex premium curve
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            if option_type == OptionRight.CALL:
                otm_dist = max(float(s) - self.spot, 0.0)
                intrinsic = max(self.spot - float(s), 0.0)
            else:
                otm_dist = max(self.spot - float(s), 0.0)
                intrinsic = max(float(s) - self.spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm_dist) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type == OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=EXPIRY, bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, delta=None,
                implied_volatility=None))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity,
                                   limit_price=limit_price, strategy=option_strategy))
        return SimpleNamespace(id=len(self.submitted), data={})

    # --- unused abstract bits
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


#: 100-strike ATM short straddle: ONE short 100 put (10,000 of delivery) reserved at
#: Reg-T naked margin, BOTH legs (4,000 — review 2026-08-30 F10). ``sizing`` is set so
#: ONE contract is bought at every balance these tests use, so only the verdict moves,
#: never the size.
STRADDLE = dict(strike_method="percent_otm", strike_param=0.0,
                dte_min=10, dte_max=40, sizing=10.0)
STRADDLE_TIGHT = dict(STRADDLE, sizing=45.0)
CSP = dict(strike_method="percent_otm", strike_param=0.0,
           dte_min=10, dte_max=40, sizing=25.0)
#: Bull put CREDIT spread: SHORT the 95 put, LONG the 90 wing. Reserves the $5 width
#: minus the credit (a few hundred dollars) and owes 95 x 100 per contract on delivery —
#: the widest reserve-vs-delivery gap of any two-leg structure in the file, which is
#: exactly the shape this gate exists for.
BULL_PUT = dict(strike_method="percent_otm", strike_param=[5.0, 10.0],
                dte_min=10, dte_max=40, sizing=5.0)


def is_capacity_refusal(res):
    return not res["success"] and ASSIGNMENT_CAPACITY_REFUSAL in res["message"]


# ==========================================================================
# THE HEADLINE
# ==========================================================================
def test_five_short_puts_each_inside_buying_power_cannot_all_take_delivery(caplog=None):
    """45,000 of cash; five short 100-strike puts is a 50,000 delivery bill.

    Each short straddle reserves Reg-T naked margin on both legs (4,000) and the buying-power gate
    says yes to every one of them — asserted below, so this cannot be mistaken for a
    buying-power refusal. Only the fifth is refused, and only by assignment capacity.
    """
    acct = FakeAccount(spot=100.0, balance=45_000.0)
    verdicts = []
    for i in range(5):
        res = act(acct, "open_short_straddle", **STRADDLE).execute()
        verdicts.append((f"leg{i + 1}", res["success"], res["message"]))
        if res["success"]:
            sub = acct.submitted[-1]
            assert sub["quantity"] == 1, sub
            acct.hold(held_short_put(strike=100.0, qty=1, reserve=4_000.0,
                                     symbol=f"SYM{i + 1}"))

    assert [v[1] for v in verdicts] == [True, True, True, True, False], verdicts
    assert is_capacity_refusal(
        {"success": verdicts[4][1], "message": verdicts[4][2]}), verdicts[4][2]
    # ...and it is NOT a buying-power refusal: the pool still had ample room.
    assert acct.check_option_buying_power(4_000.0) is True
    assert acct.available_option_buying_power() == pytest.approx(45_000.0 - 4 * 4_000.0)
    # The bill the fifth would have created.
    assert acct.short_put_assignment_exposure().cost == pytest.approx(40_000.0)


def test_the_candidate_itself_is_charged_not_only_the_held_book():
    """Admitting the fifth because the first four fit is the whole bug: on an EMPTY
    book the candidate alone must still be measured."""
    acct = FakeAccount(spot=100.0, balance=9_999.99)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(0.0)
    res = act(acct, "open_short_straddle", **STRADDLE_TIGHT).execute()
    assert is_capacity_refusal(res), res["message"]


# ==========================================================================
# the boundary — settled in 2ce4efe, pinned here at the ENTRY path
# ==========================================================================
@pytest.mark.parametrize("cash,admitted", [(10_000.00, True), (9_999.99, False)])
def test_cash_exactly_equal_to_the_bill_admits(cash, admitted):
    """A short put with the cash secured is the definition of the structure, and every
    other cap in this path admits at its line.

    One 100-strike short put, one contract: a 10,000 bill. Sized off Reg-T naked margin
    on both legs (4,000) so the SIZE does not move between the two cash figures — only
    the verdict.
    """
    acct = FakeAccount(spot=100.0, balance=cash)
    res = act(acct, "open_short_straddle", **STRADDLE_TIGHT).execute()
    assert res["success"] is admitted, res["message"]
    if not admitted:
        assert is_capacity_refusal(res), res["message"]


# ==========================================================================
# unmeasurable must refuse — and must say WHICH input is missing
# ==========================================================================
def test_an_unmeasurable_book_refuses_and_names_the_order():
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    rows = held_short_put(strike=100.0, qty=1, reserve=4_000.0)
    rows[1].strike = None                       # the short put leg loses its strike
    acct.hold(rows)
    assert acct.short_put_assignment_exposure().cost is None
    res = act(acct, "sell_cash_secured_put", **CSP).execute()
    assert is_capacity_refusal(res), res["message"]
    assert "unmeasurable" in res["message"] or "cannot price" in res["message"], \
        res["message"]
    assert str(rows[1].id) in res["message"], res["message"]


def test_an_unreadable_balance_refuses_at_the_interface():
    """The builder never reaches the gate with no balance (sizing already returns 0),
    so this is pinned where it is decided."""
    acct = FakeAccount(spot=100.0, balance=None)
    verdict = acct.assignment_capacity(0.0)
    assert verdict.ok is False
    assert "balance" in verdict.reason.lower() or "cash" in verdict.reason.lower()
    assert acct.check_assignment_capacity(0.0) is False


def test_an_unpriceable_candidate_refuses_and_names_the_input():
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    verdict = acct.check_short_put_assignment_capacity(strike=None, contracts=1)
    assert verdict.ok is False
    assert "strike" in verdict.reason.lower()


# ==========================================================================
# which builders are charged, and which are NOT
# ==========================================================================
@pytest.fixture
def full_book():
    """A book whose delivery bill EXACTLY equals the cash: any further short put
    refuses, anything carrying no short put must still be admitted."""
    acct = FakeAccount(spot=100.0, balance=45_000.0)
    acct.hold(held_structure("short_strangle", 3_000.0,
                             (OptionRight.PUT, 90.0, 5, OrderDirection.SELL)))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(45_000.0)
    return acct


def test_a_bear_call_spread_is_never_charged_put_assignment_capacity(full_book):
    """SHORT the lower call, LONG the higher call. There is no put in it — nobody can
    put shares to us — so it must open on a book that is already at its capacity."""
    res = act(full_book, "open_bear_call_spread",
              strike_method="percent_otm", strike_param=5.0,
              dte_min=10, dte_max=40, sizing=20.0).execute()
    assert res["success"], res["message"]
    assert full_book.submitted[-1]["strategy"] == "bear_call_spread"


def test_a_cash_secured_put_on_a_full_book_is_refused(full_book):
    res = act(full_book, "sell_cash_secured_put", **CSP).execute()
    assert is_capacity_refusal(res), res["message"]


@pytest.mark.parametrize("action,strategy,kw", [
    ("sell_cash_secured_put", "cash_secured_put", CSP),
    ("open_short_straddle", "short_straddle", STRADDLE),
    ("open_short_strangle", "short_strangle",
     dict(strike_method="percent_otm", strike_param=10.0, dte_min=10, dte_max=40,
          sizing=5.0)),
    ("open_iron_condor", "iron_condor",
     dict(strike_method="percent_otm", strike_param=10.0, wing_width_pct=5.0,
          dte_min=10, dte_max=40, sizing=5.0)),
    ("open_jade_lizard", "jade_lizard",
     dict(strike_method="percent_otm", strike_param=10.0, wing_width_pct=5.0,
          dte_min=10, dte_max=40, sizing=25.0)),
    ("open_put_ratio_spread", "put_ratio_spread",
     dict(strike_method="percent_otm", strike_param=5.0, wing_width_pct=5.0,
          dte_min=10, dte_max=40, sizing=20.0)),
    ("open_bull_put_spread", "bull_put_spread", BULL_PUT),
])
def test_every_builder_carrying_a_short_put_is_charged(full_book, action, strategy, kw):
    """Seven of the eight reserving builders carry a SHORT PUT and must be refused on a
    book that is already at its delivery capacity."""
    res = act(full_book, action, **kw).execute()
    assert is_capacity_refusal(res), f"{strategy}: {res['message']}"
    # FOUND BY MUTATION F07: a gate that runs after the submit still returns a refusal
    # — while the order is already at the broker.
    assert full_book.submitted == [], f"{strategy} reached the broker anyway"


def test_a_short_call_consumes_no_capacity_even_inside_a_charged_structure():
    """The short straddle sells a 100 call as well as a 100 put. Charging the call
    would double the bill — and a short call assigned pays cash IN."""
    acct = FakeAccount(spot=100.0, balance=10_000.0)
    res = act(acct, "open_short_straddle", **STRADDLE_TIGHT).execute()
    assert res["success"], res["message"]           # 10,000 put only, exactly the cash
    assert acct.submitted[-1]["quantity"] == 1


def test_the_put_ratio_spread_is_charged_BOTH_of_its_short_puts():
    """1 long put / 2 SHORT puts per structure (``ratio_qty=2``). Charging one is
    charging half the obligation."""
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    kw = dict(strike_method="percent_otm", strike_param=5.0, wing_width_pct=5.0,
              dte_min=10, dte_max=40, sizing=20.0)
    res = act(acct, "open_put_ratio_spread", **kw).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    short_leg = [leg for leg in sub["legs"] if leg.side == OrderDirection.SELL][0]
    assert short_leg.ratio_qty == 2
    both = short_leg.strike * 100.0 * 2 * sub["quantity"]
    one = both / 2.0

    def with_room(room):
        """Same BALANCE (so the same quantity is sized) but a held book that leaves
        exactly ``room`` of delivery capacity."""
        a = FakeAccount(spot=100.0, balance=1_000_000.0)
        a.hold(held_structure("short_strangle", 1.0,
                              (OptionRight.PUT, (1_000_000.0 - room) / 100.0, 1,
                               OrderDirection.SELL)))
        return a

    # Exactly the TWO-leg bill of room admits (boundary)...
    ok = act(with_room(both), "open_put_ratio_spread", **kw).execute()
    assert ok["success"], ok["message"]
    # ...and only ONE leg's worth of room does not. Charge one short leg instead of two
    # and this is admitted, on money that is not there.
    half = act(with_room(one), "open_put_ratio_spread", **kw).execute()
    assert is_capacity_refusal(half), half["message"]


# ==========================================================================
# the refusal must be TELLABLE APART from a buying-power refusal
# ==========================================================================
def test_a_capacity_refusal_is_distinguishable_from_a_buying_power_refusal():
    """Different remedies: a BP refusal frees up by repairing/closing ANY reserving
    structure, a capacity refusal only by closing a SHORT PUT or funding cash."""
    # (a) capacity refuses, buying power is fine
    acct = FakeAccount(spot=100.0, balance=45_000.0)
    acct.hold(held_structure("short_strangle", 3_000.0,
                             (OptionRight.PUT, 90.0, 5, OrderDirection.SELL)))
    cap = act(acct, "sell_cash_secured_put", **CSP).execute()
    assert is_capacity_refusal(cap), cap["message"]
    assert "buying power" not in cap["message"].lower()
    assert "BP" not in cap["message"]

    # (b) buying power refuses, and says so in its own words
    poor = FakeAccount(spot=100.0, balance=1_000_000.0)
    poor.hold(held_structure("cash_secured_put", 999_000.0,
                             (OptionRight.PUT, 90.0, 1, OrderDirection.SELL)))
    bp = act(poor, "sell_cash_secured_put", **CSP).execute()
    assert not bp["success"]
    assert "buying power" in bp["message"].lower()
    assert ASSIGNMENT_CAPACITY_REFUSAL not in bp["message"]


# ==========================================================================
# NO DOUBLE CHARGE — the property 2ce4efe spent two tests and two mutations on
# ==========================================================================
def test_a_fully_funded_wheel_is_admitted_at_exactly_the_size_it_is_funded_for():
    """A held CSP reserves its full strike AND owes its full strike. The two views are
    measured against INDEPENDENTLY-derived budgets (the pool against
    balance-minus-reserves, capacity against the balance) and neither subtracts the
    other. Wire either into the other and this exact-fit entry is refused.

    balance 50,000; held CSP 1 x 100 (reserve 10,000, delivery 10,000).
      buying power : 10,000 reserved -> 40,000 available; candidate reserves 40,000 -> OK
      capacity     : 10,000 held     + 40,000 candidate = 50,000 vs 50,000 cash -> OK
    Netting capacity against available BP (40,000) refuses; netting the exposure out of
    the pool (30,000 available) refuses too.
    """
    acct = FakeAccount(spot=100.0, balance=50_000.0)
    acct.hold(held_structure("cash_secured_put", 10_000.0,
                             (OptionRight.PUT, 100.0, 1, OrderDirection.SELL)))
    assert acct.reserved_option_buying_power() == pytest.approx(10_000.0)
    assert acct.available_option_buying_power() == pytest.approx(40_000.0)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)

    res = act(acct, "sell_cash_secured_put",
              strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
              sizing=80.0).execute()
    assert res["success"], res["message"]
    assert acct.submitted[-1]["quantity"] == 4          # 4 x 10,000 = 40,000


# ==========================================================================
# the interface contract 2ce4efe wrote is unchanged
# ==========================================================================
def test_check_assignment_capacity_still_answers_exactly_as_before():
    acct = FakeAccount(spot=100.0, balance=45_000.0)
    acct.hold(held_structure("short_strangle", 3_000.0,
                             (OptionRight.PUT, 100.0, 4, OrderDirection.SELL)))
    assert acct.check_assignment_capacity(5_000.0) is True     # 45,000 == 45,000
    assert acct.check_assignment_capacity(5_000.01) is False
    assert acct.check_assignment_capacity(-1.0) is False       # never buys capacity
    assert acct.check_assignment_capacity(0.0) is True


# ==========================================================================
# WHAT THIS COMMIT STOPS ALLOWING — the two configurations that used to open
# ==========================================================================
@pytest.mark.parametrize("action,kw,bill", [
    # 30% sizing off $2,000/contract of Reg-T naked margin (both legs, F10) = 15 short
    # 90-strike puts. (Before F10 the put-only reserve bought 20 of them at 20% sizing —
    # the F10 sum halves the size the same budget buys, so the sizing is raised to keep
    # this a configuration the CAPACITY gate, not the budget, refuses.)
    ("open_short_strangle",
     dict(strike_method="percent_otm", strike_param=10.0, dte_min=10, dte_max=40,
          sizing=30.0), 135_000.0),
    # 5% sizing off a $460/contract wing = 43 condors, each with a short 90 put.
    ("open_iron_condor",
     dict(strike_method="percent_otm", strike_param=10.0, wing_width_pct=5.0,
          dte_min=10, dte_max=40, sizing=20.0), 387_000.0),
])
def test_the_sizes_this_gate_newly_refuses_on_a_100k_account(action, kw, bill):
    """These two OPENED before this commit and are refused now. Both are sized off a
    budget far cheaper than delivery (Reg-T margin; the condor's wing), which is
    precisely how a $100,000 account came to write a $387,000 delivery obligation.

    They are the reversals of ``test_short_strangle_two_short_legs_credit`` and
    ``test_iron_condor_four_legs_credit_defined_risk``, whose sizing was reduced so
    they keep pinning leg construction rather than an affordable size.
    """
    acct = FakeAccount(spot=100.0, balance=100_000.0)
    assert acct.check_option_buying_power(20_000.0) is True     # buying power is fine
    res = act(acct, action, **kw).execute()
    assert is_capacity_refusal(res), res["message"]
    assert f"{bill:,.2f}" in res["message"], res["message"]


def test_the_refusal_reports_the_held_bill_the_candidate_and_the_total():
    """FOUND BY MUTATION A29 (the message reported the held bill as the total, and no
    test noticed because the cases that checked a figure had an empty book).

    The three numbers an operator needs to act are how much is already owed, how much
    this trade adds, and what the account holds — and the total has to be the sum of the
    first two or the sentence is arithmetic nobody can follow.
    """
    acct = FakeAccount(spot=100.0, balance=45_000.0)
    acct.hold(held_structure("short_strangle", 3_000.0,
                             (OptionRight.PUT, 90.0, 4, OrderDirection.SELL)))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(36_000.0)

    # One 100-strike short put, one contract: 10,000 on top of 36,000, vs 45,000 cash.
    res = act(acct, "open_short_straddle", **STRADDLE).execute()
    assert is_capacity_refusal(res), res["message"]
    for figure in ("46,000.00", "36,000.00", "10,000.00", "45,000.00"):
        assert figure in res["message"], f"{figure} missing from: {res['message']}"
    assert res["data"]["assignment_held_cost"] == pytest.approx(36_000.0)
    assert res["data"]["assignment_candidate_cost"] == pytest.approx(10_000.0)
    assert res["data"]["assignment_cash"] == pytest.approx(45_000.0)


def test_an_admitted_entry_carries_no_refusal_reason():
    """The verdict and its reason must agree: a reason on an admitted candidate would
    make every log line look like a decline."""
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    verdict = acct.check_short_put_assignment_capacity(strike=100.0, contracts=1)
    assert verdict.ok is True
    assert verdict.reason == ""
    assert verdict.held_cost == pytest.approx(0.0)
    assert verdict.candidate_cost == pytest.approx(10_000.0)
    assert verdict.cash == pytest.approx(1_000_000.0)


# ==========================================================================
# WHICH leg, and HOW MANY of it — found by mutation (B17, B21, B25)
# ==========================================================================
def _leaving_room(room, balance=1_000_000.0, spot=100.0):
    """An account with ``balance`` whose held book leaves exactly ``room`` of delivery
    capacity (and a negligible reserve, so buying power is never what bites)."""
    a = FakeAccount(spot=spot, balance=balance)
    if room < balance:
        a.hold(held_structure("short_strangle", 1.0,
                              (OptionRight.PUT, (balance - room) / 100.0, 1,
                               OrderDirection.SELL)))
    assert a.short_put_assignment_exposure().cost == pytest.approx(balance - room)
    return a


JADE = dict(strike_method="percent_otm", strike_param=10.0, wing_width_pct=5.0,
            dte_min=10, dte_max=40, sizing=1.0)


def test_the_jade_lizard_is_charged_its_PUT_strike_not_its_call_strike():
    """FOUND BY MUTATION B17. The short put is 10% BELOW spot (90) and the short call
    10% above (110): a gate reading the call leg charges 11,000 for an obligation of
    9,000 — and, being the larger number, it fails safe and hides itself.

    A jade lizard's call side is a defined-risk credit spread that owes SHARES.
    """
    ok = act(_leaving_room(9_000.0), "open_jade_lizard", **JADE).execute()
    assert ok["success"], ok["message"]
    assert ok["message"].endswith("jade_lizard for XYZ")
    # One dollar less than the put leg's bill, and it must refuse.
    tight = act(_leaving_room(8_999.99), "open_jade_lizard", **JADE).execute()
    assert is_capacity_refusal(tight), tight["message"]


def test_a_cash_secured_put_is_charged_EVERY_contract_it_opens():
    """FOUND BY MUTATION B21. Four contracts at the 100 strike is 40,000 of delivery,
    not 10,000 — charging one contract makes the gate blind to size, which is the one
    dimension the operator actually turns."""
    kw = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
              sizing=4.5)
    sized = _leaving_room(1_000_000.0)
    res = act(sized, "sell_cash_secured_put", **kw).execute()
    assert res["success"], res["message"]
    assert sized.submitted[-1]["quantity"] == 4

    # Exactly four contracts' worth of room admits; ONE contract's worth does not.
    assert act(_leaving_room(40_000.0), "sell_cash_secured_put", **kw).execute()["success"]
    one = act(_leaving_room(10_000.0), "sell_cash_secured_put", **kw).execute()
    assert is_capacity_refusal(one), one["message"]


def test_when_both_gates_would_refuse_the_buying_power_message_is_the_one_kept():
    """FOUND BY MUTATION B25. The capacity gate runs AFTER the buying-power gate on
    purpose: an entry the pre-existing gate already refuses must keep the message it
    has always had, so no decline already on record is re-labelled and the new refusal
    only ever appears where something genuinely new is being caught."""
    acct = FakeAccount(spot=100.0, balance=100_000.0)
    acct.hold(held_structure("cash_secured_put", 99_000.0,
                             (OptionRight.PUT, 900.0, 1, OrderDirection.SELL)))
    assert acct.available_option_buying_power() == pytest.approx(1_000.0)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(90_000.0)

    res = act(acct, "sell_cash_secured_put", **CSP).execute()
    assert not res["success"]
    assert "buying power" in res["message"].lower(), res["message"]
    assert ASSIGNMENT_CAPACITY_REFUSAL not in res["message"], res["message"]
    # ...and the capacity gate WOULD have refused it too, so this is genuinely an
    # ordering assertion and not a case where only one gate applies.
    assert acct.check_assignment_capacity(20_000.0) is False


#: Each entry sizes the builder to MORE THAN ONE contract on a $1,000,000 account, so a
#: gate that charges a flat one contract (or the wrong leg) is discriminated.
MULTI_CONTRACT = [
    # 10% OTM deliberately: at 0% the strike equals the spot, and a gate charging the
    # SPOT is then indistinguishable from one charging the strike (mutation B37).
    ("sell_cash_secured_put", dict(strike_method="percent_otm", strike_param=10.0,
                                   dte_min=10, dte_max=40, sizing=4.5)),
    ("open_short_straddle", dict(strike_method="percent_otm", strike_param=0.0,
                                 dte_min=10, dte_max=40, sizing=1.0)),
    ("open_short_strangle", dict(strike_method="percent_otm", strike_param=10.0,
                                 dte_min=10, dte_max=40, sizing=0.6)),
    ("open_iron_condor", dict(strike_method="percent_otm", strike_param=10.0,
                              wing_width_pct=5.0, dte_min=10, dte_max=40, sizing=0.15)),
    ("open_jade_lizard", dict(strike_method="percent_otm", strike_param=10.0,
                              wing_width_pct=5.0, dte_min=10, dte_max=40, sizing=3.0)),
    ("open_put_ratio_spread", dict(strike_method="percent_otm", strike_param=5.0,
                                   wing_width_pct=5.0, dte_min=10, dte_max=40,
                                   sizing=3.0)),
    ("open_bull_put_spread", dict(BULL_PUT, sizing=0.2)),
]


@pytest.mark.parametrize("action,kw", MULTI_CONTRACT)
def test_every_charged_builder_is_charged_EVERY_short_put_contract_it_opens(action, kw):
    """FOUND BY MUTATIONS B21/B30/B31/B32/B33 — five of the six builders had no test
    that sized past one contract, so a gate charging a flat single contract passed.

    Derived rather than hardcoded: open the structure with unlimited room, read the
    SHORT PUT legs it actually built (``ratio_qty`` included), and require the gate to
    admit at exactly that bill, refuse one cent below it, and refuse at the bill of a
    single contract.
    """
    opened = _leaving_room(1_000_000.0)
    res = act(opened, action, **kw).execute()
    assert res["success"], res["message"]
    sub = opened.submitted[-1]
    short_puts = [l for l in sub["legs"]
                  if l.option_type == OptionRight.PUT and l.side == OrderDirection.SELL]
    assert len(short_puts) == 1, short_puts
    leg = short_puts[0]
    contracts = leg.ratio_qty * sub["quantity"]
    assert contracts >= 2, f"{action} sized {contracts} contract(s) — cannot discriminate"
    bill = leg.strike * 100.0 * contracts

    assert act(_leaving_room(bill), action, **kw).execute()["success"]
    tight = act(_leaving_room(bill - 0.01), action, **kw).execute()
    assert is_capacity_refusal(tight), tight["message"]
    one = act(_leaving_room(leg.strike * 100.0), action, **kw).execute()
    assert is_capacity_refusal(one), one["message"]


def test_the_short_straddle_is_charged_its_STRIKE_not_the_spot():
    """FOUND BY MUTATION B39. Every other straddle test puts spot exactly on a listed
    strike, where "the strike" and "the spot" are the same number. Strikes are discrete;
    a 102 spot writes the 100 straddle, and the shares are put to us at 100.
    """
    kw = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
              sizing=0.8)
    opened = _leaving_room(1_000_000.0, spot=102.0)
    res = act(opened, "open_short_straddle", **kw).execute()
    assert res["success"], res["message"]
    sub = opened.submitted[-1]
    assert sub["quantity"] == 2
    put_leg = [l for l in sub["legs"] if l.option_type == OptionRight.PUT][0]
    assert put_leg.strike == 100.0 and opened.spot == 102.0

    # 2 x 100 x 100 of room admits; charging the 102 spot needs 20,400 and would not.
    ok = act(_leaving_room(20_000.0, spot=102.0), "open_short_straddle", **kw).execute()
    assert ok["success"], ok["message"]
    tight = act(_leaving_room(19_999.99, spot=102.0), "open_short_straddle", **kw).execute()
    assert is_capacity_refusal(tight), tight["message"]


# ==========================================================================
# THE SCOPE OF THE GATE, scanned out of the source (mutations D12/D13/D14)
# ==========================================================================
def _gated_strategies():
    """Every strategy name handed to ``_refuse_if_cannot_take_delivery``, by ``ast``."""
    import ast
    import inspect
    from ba2_common.core import TradeActions

    tree = ast.parse(inspect.getsource(TradeActions))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "_refuse_if_cannot_take_delivery":
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    assert found, "the ast scan found no gated strategies — the scan itself is broken"
    return found


def test_only_the_builders_that_carry_a_SHORT_PUT_call_the_capacity_gate():
    """Charging a structure that owes no cash on assignment refuses trades for an
    obligation that does not exist, and it is silent: the refusal looks exactly like a
    correct one. Seven of the eight reserving builders carry a short put; the leg
    construction, per builder:

    ==================  ============================================================
    cash_secured_put    ONE ``SELL`` ``PUT``                                 -> YES
    short_straddle      ``SELL`` call + ``SELL`` put at the SAME strike      -> YES
    short_strangle      ``SELL`` OTM call + ``SELL`` OTM put                 -> YES
    iron_condor         ``SELL`` put + ``BUY`` put wing + ``SELL``/``BUY`` calls -> YES
    jade_lizard         ``SELL`` naked put + ``SELL`` call + ``BUY`` call wing -> YES
    put_ratio_spread    ``BUY`` 1 put + ``SELL`` 2 puts (``ratio_qty=2``)    -> YES
    bull_put_spread     ``SELL`` higher put + ``BUY`` lower put wing         -> YES
    bear_call_spread    ``SELL`` lower call + ``BUY`` higher call            -> NO
    ==================  ============================================================

    KNOWN AND REPORTED, deliberately out of this commit's scope: ``bear_put_spread``
    (``BUY`` the higher-strike put, ``SELL`` the lower one) also carries a short put and
    is NOT gated — it is a DEBIT builder outside the eight, with no reserve call of its
    own. Its short leg IS counted by ``short_put_assignment_exposure`` once open (pinned
    just below), so the book is never blind to it; only its entry is ungated.
    """
    assert _gated_strategies() == {
        "cash_secured_put", "short_straddle", "short_strangle",
        "iron_condor", "jade_lizard", "put_ratio_spread", "bull_put_spread",
    }


@pytest.mark.parametrize("action,kw", [
    ("buy_call", dict(strike_method="percent_otm", strike_param=5.0, dte_min=10,
                      dte_max=40, sizing=20.0)),
    ("buy_put", dict(strike_method="percent_otm", strike_param=5.0, dte_min=10,
                     dte_max=40, sizing=20.0)),
    ("open_bull_call_spread", dict(strike_method="percent_otm", strike_param=5.0,
                                   dte_min=10, dte_max=40, sizing=20.0)),
    ("open_bear_call_spread", dict(strike_method="percent_otm", strike_param=5.0,
                                   dte_min=10, dte_max=40, sizing=20.0)),
    ("open_straddle", dict(strike_method="percent_otm", strike_param=0.0, dte_min=10,
                           dte_max=40, sizing=20.0)),
    ("open_strangle", dict(strike_method="percent_otm", strike_param=10.0, dte_min=10,
                           dte_max=40, sizing=20.0)),
    ("open_call_butterfly", dict(strike_method="percent_otm", strike_param=0.0,
                                 wing_width_pct=10.0, dte_min=10, dte_max=40,
                                 sizing=20.0)),
])
def test_a_structure_with_no_short_put_opens_on_a_book_at_full_capacity(full_book,
                                                                        action, kw):
    """Nothing here can have shares put to it — the butterfly's short body and the bear
    call spread's short leg are CALLS, which deliver shares and pay cash IN."""
    res = act(full_book, action, **kw).execute()
    assert res["success"], res["message"]
    assert ASSIGNMENT_CAPACITY_REFUSAL not in res["message"]


# ==========================================================================
# THE BULL PUT SPREAD — the reserve is the WIDTH, the bill is the STRIKE
# ==========================================================================
def test_the_bull_put_spreads_long_wing_nets_NOTHING_off_the_assignment_bill():
    """The defect this structure is most exposed to, stated as an experiment.

    A bull put spread SELLS the 95 put and BUYS the 90 wing. Its RESERVE is the $5 width
    minus the credit — a few hundred dollars. Its DELIVERY BILL is the full 95 x 100 per
    contract, because the short leg is assigned TONIGHT while exercising our own 90 put
    is a choice we make LATER, after the shares have already been paid for. Nothing about
    holding the wing puts cash in the account on assignment night.

    Netted (95 - 90) x 100 the bill would be $500/contract instead of $9,500 — a 19x
    understatement, and one that fails OPEN: every entry sails through.

    Derived from the legs the builder actually produced, so it cannot drift from them.
    """
    opened = _leaving_room(1_000_000.0)
    res = act(opened, "open_bull_put_spread", **BULL_PUT).execute()
    assert res["success"], res["message"]
    sub = opened.submitted[-1]
    short = [l for l in sub["legs"]
             if l.option_type == OptionRight.PUT and l.side == OrderDirection.SELL][0]
    long_ = [l for l in sub["legs"]
             if l.option_type == OptionRight.PUT and l.side == OrderDirection.BUY][0]
    assert short.strike > long_.strike
    contracts = short.ratio_qty * sub["quantity"]

    full = short.strike * 100.0 * contracts           # what the gate MUST charge
    netted = (short.strike - long_.strike) * 100.0 * contracts   # the wing-netted lie
    assert netted < full / 2, "the two must be far enough apart to discriminate"

    # Exactly the FULL bill of room admits...
    assert act(_leaving_room(full), "open_bull_put_spread", **BULL_PUT).execute()["success"]
    # ...one cent under it does not...
    tight = act(_leaving_room(full - 0.01), "open_bull_put_spread", **BULL_PUT).execute()
    assert is_capacity_refusal(tight), tight["message"]
    # ...and the wing-netted figure is nowhere near enough.
    netted_room = act(_leaving_room(netted), "open_bull_put_spread", **BULL_PUT).execute()
    assert is_capacity_refusal(netted_room), netted_room["message"]
    # Nor is the RESERVE the structure sets aside — the two budgets are unrelated.
    reserve = res["data"]["option_reserve"]
    assert reserve < full
    by_reserve = act(_leaving_room(reserve), "open_bull_put_spread", **BULL_PUT).execute()
    assert is_capacity_refusal(by_reserve), by_reserve["message"]


def test_the_bull_put_spread_is_charged_its_SHORT_strike_not_its_LONG_one():
    """Reading the wrong leg here fails OPEN (the long strike is the LOWER one), which
    is the direction a mistake hides in — unlike the jade lizard's B17, where the wrong
    leg happened to be the larger number and fail-safe.

    Rooms are one cent apart around each leg's bill, so only the strike being charged
    can decide the verdict.
    """
    opened = _leaving_room(1_000_000.0)
    act(opened, "open_bull_put_spread", **BULL_PUT).execute()
    sub = opened.submitted[-1]
    short = [l for l in sub["legs"] if l.side == OrderDirection.SELL][0]
    long_ = [l for l in sub["legs"] if l.side == OrderDirection.BUY][0]
    contracts = short.ratio_qty * sub["quantity"]

    at_long = long_.strike * 100.0 * contracts
    assert at_long < short.strike * 100.0 * contracts
    refused = act(_leaving_room(at_long), "open_bull_put_spread", **BULL_PUT).execute()
    assert is_capacity_refusal(refused), refused["message"]


def test_the_bull_put_spread_reserves_the_width_but_is_gated_on_the_strike():
    """Both gates are live and they measure DIFFERENT budgets. Buying power says yes
    (the width is cheap); assignment capacity says no (the strike is not). If the
    capacity gate were wired to the reserve — or omitted — this opens.
    """
    acct = FakeAccount(spot=100.0, balance=50_000.0)
    acct.hold(held_structure("short_strangle", 1.0,
                             (OptionRight.PUT, 450.0, 1, OrderDirection.SELL)))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(45_000.0)
    assert acct.check_option_buying_power(2_000.0) is True   # the width is affordable

    res = act(acct, "open_bull_put_spread", **BULL_PUT).execute()
    assert is_capacity_refusal(res), res["message"]
    assert "bull_put_spread" in res["message"], res["message"]
    assert res["data"]["option_strategy"] == "bull_put_spread"
    assert acct.submitted == []


def test_a_held_bull_put_spread_owes_its_full_short_strike_in_the_book_view():
    """The entry gate and the book view must agree about the same structure, or the
    second one opened is measured against a bill the first one under-reported."""
    acct = FakeAccount(spot=100.0, balance=100_000.0)
    acct.hold(held_structure("bull_put_spread", 400.0,
                             (OptionRight.PUT, 95.0, 3, OrderDirection.SELL),
                             (OptionRight.PUT, 90.0, 3, OrderDirection.BUY)))
    # 3 x 95 x 100 — and the 90 long wing nets nothing off it.
    assert acct.short_put_assignment_exposure().cost == pytest.approx(28_500.0)


def test_netting_is_ONE_FOR_ONE_and_only_within_a_SINGLE_contract_symbol():
    """FOUND BY MUTATION N02, which over-credited every long put by 2x and survived the
    whole suite.

    Two properties, and the bull put spread depends on the second one being true:

    * ONE-FOR-ONE. Buying back 1 of a 2-lot short leaves 1 contract of delivery, not 0.
      Over-crediting the close reports a flat book while the account is still short.
    * PER SYMBOL. The spread's long wing sits on a DIFFERENT contract, so it cannot net
      anything off the short leg at all — which is the arithmetic reason the wing does
      not reduce the assignment bill, stated where the netting actually happens.
    """
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    short = held_structure("cash_secured_put", 20_000.0,
                           (OptionRight.PUT, 100.0, 2, OrderDirection.SELL))
    acct.hold(short)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(20_000.0)

    # A long put on a DIFFERENT strike (a wing) nets NOTHING off.
    acct.hold(held_structure("bull_put_spread", 0.0,
                             (OptionRight.PUT, 95.0, 2, OrderDirection.BUY)))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(20_000.0)

    # Buying back ONE of the two on the SAME contract leaves exactly one short.
    same = held_structure("cash_secured_put", 0.0,
                          (OptionRight.PUT, 100.0, 1, OrderDirection.BUY))
    same[1].contract_symbol = short[1].contract_symbol
    acct.hold(same)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)
    assert acct.short_put_assignment_exposure().contracts == pytest.approx(1.0)


def test_the_bear_put_spreads_short_leg_is_still_counted_once_it_is_OPEN():
    """The book view is complete even where the entry gate's scope is not: a debit put
    vertical's short leg owes the full strike on assignment and is summed like any
    other short put."""
    acct = FakeAccount(spot=100.0, balance=100_000.0)
    acct.hold(held_structure("bear_put_spread", 0.0,
                             (OptionRight.PUT, 100.0, 2, OrderDirection.SELL),
                             (OptionRight.PUT, 95.0, 2, OrderDirection.BUY)))
    # 2 x 100 x 100 — and the 95 long wing nets nothing off it.
    assert acct.short_put_assignment_exposure().cost == pytest.approx(20_000.0)


def test_the_marker_an_operator_greps_for_is_pinned():
    """An operator-facing log contract, not an implementation detail: it is shouted so
    it stands out in a log next to the buying-power refusals, and it shares no word
    with them."""
    assert ASSIGNMENT_CAPACITY_REFUSAL == "ASSIGNMENT CAPACITY"
    assert ASSIGNMENT_CAPACITY_REFUSAL.isupper()
    for bp_word in ("buying power", "bp", "reserve", "insufficient"):
        assert bp_word not in ASSIGNMENT_CAPACITY_REFUSAL.lower()


def test_the_capacity_gate_does_not_ALSO_spend_buying_power_on_the_same_dollars():
    """FOUND BY MUTATION E18 — the double charge in its other direction.

    The delivery bill is measured against CASH. Requiring the same dollars to fit in
    available BUYING POWER as well quietly re-imposes the reserve pool on a budget that
    has nothing to do with it, and refuses a wheel the account can plainly fund.

    balance 50,000; a held bear call spread reserves 35,000 and owes NO delivery.
      buying power : 15,000 available; the candidate reserves 10,000 (F10: both
                     legs, 2,000/contract)                                     -> OK
      capacity     : 0 owed + 45,000 for the candidate vs 50,000 of cash       -> OK
    Charge the 45,000 delivery bill against the 15,000 of buying power and it refuses.
    """
    acct = FakeAccount(spot=100.0, balance=50_000.0)
    acct.hold(held_structure("bear_call_spread", 35_000.0,
                             (OptionRight.CALL, 110.0, 8, OrderDirection.SELL),
                             (OptionRight.CALL, 115.0, 8, OrderDirection.BUY)))
    assert acct.available_option_buying_power() == pytest.approx(15_000.0)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(0.0)

    res = act(acct, "open_short_strangle",
              strike_method="percent_otm", strike_param=10.0, dte_min=10, dte_max=40,
              sizing=20.0).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    assert sub["quantity"] == 5                       # 5 x 90 x 100 = 45,000 of delivery
    assert acct.check_option_buying_power(45_000.0) is False   # ...which BP would refuse


def test_a_capacity_refusal_is_logged_and_names_the_structure(monkeypatch):
    """FOUND BY MUTATIONS E19/E20. A silent refusal, or one that does not say WHICH
    structure was refused, is indistinguishable in a log from the entry simply not
    firing — and the whole reason this gate has its own marker is so an operator can
    find it there.

    Uses a recording logger rather than ``caplog``: the assertion is about the call
    this module makes, not about pytest's capture plumbing.
    """
    recorded = []

    class _Recorder:
        def warning(self, msg, *a, **k):
            recorded.append(msg)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    from ba2_common.core import TradeActions as TA_mod
    monkeypatch.setattr(TA_mod, "logger", _Recorder())

    acct = FakeAccount(spot=100.0, balance=45_000.0)
    acct.hold(held_structure("short_strangle", 3_000.0,
                             (OptionRight.PUT, 90.0, 5, OrderDirection.SELL)))
    res = act(acct, "sell_cash_secured_put", **CSP).execute()
    assert is_capacity_refusal(res), res["message"]

    hits = [m for m in recorded if ASSIGNMENT_CAPACITY_REFUSAL in m]
    assert len(hits) == 1, recorded
    assert "cash_secured_put" in hits[0], hits[0]
    assert "XYZ" in hits[0], hits[0]
    # ...and the refusal handed back to the caller names the structure too.
    assert "cash_secured_put" in res["message"], res["message"]
    assert res["data"]["option_strategy"] == "cash_secured_put"


# ==========================================================================
# the IN-FLIGHT window (mutations F03/F05)
# ==========================================================================
def _pending(rows):
    """Mark the leg rows submitted-but-unfilled."""
    for r in rows[1:]:
        r.status = OrderStatus.NEW
        r.filled_qty = None
    return rows


def test_an_in_flight_short_put_is_added_on_top_and_never_netted():
    """FOUND BY MUTATION F03. A submitted-but-unfilled SELL can fill at any moment and
    can only ever ADD an obligation — netting it against a position we already hold
    LONG would hand back capacity for a short that is about to exist.
    """
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    held = held_structure("bear_put_spread", 0.0,
                          (OptionRight.PUT, 100.0, 1, OrderDirection.BUY))
    acct.hold(held)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(0.0)

    # ...now write ONE short put on the very same contract, not yet filled.
    pending = _pending(held_structure("cash_secured_put", 10_000.0,
                                      (OptionRight.PUT, 100.0, 1, OrderDirection.SELL)))
    acct.hold(pending)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)


def test_an_unpriceable_IN_FLIGHT_short_put_makes_the_book_unmeasurable():
    """FOUND BY MUTATION F05. The held branch already refused on this (D08); the
    in-flight branch is the same obligation one status earlier."""
    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    rows = _pending(held_structure("cash_secured_put", 10_000.0,
                                   (OptionRight.PUT, 100.0, 1, OrderDirection.SELL)))
    rows[1].strike = None
    acct.hold(rows)
    exposure = acct.short_put_assignment_exposure()
    assert exposure.cost is None
    assert str(rows[1].id) in "; ".join(exposure.unmeasurable)

    res = act(acct, "sell_cash_secured_put", **CSP).execute()
    assert is_capacity_refusal(res), res["message"]
    assert str(rows[1].id) in res["message"], res["message"]


def test_a_refused_structure_never_reaches_the_broker():
    """FOUND BY MUTATION F07. A refusal returned AFTER ``submit_option_order`` reads
    exactly like a refusal returned before it — except the position exists."""
    for action, kw in MULTI_CONTRACT:
        acct = _leaving_room(1_000.0)          # a thousand dollars of delivery capacity
        res = act(acct, action, **kw).execute()
        assert is_capacity_refusal(res), f"{action}: {res['message']}"
        assert acct.submitted == [], f"{action} was submitted despite being refused"
