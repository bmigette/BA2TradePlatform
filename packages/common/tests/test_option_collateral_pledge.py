"""Are these shares SPOKEN FOR? — the two cover accessors (OPT-L1).

THE HOLE THESE CLOSE. Seam 1 stopped the wrong-instrument order: an option
transaction can no longer be routed through the equity close/adjust paths, and
options no longer enter the allocation plan. It did NOT stop the dangerous half.
A "set AAPL to 0%" allocation run still sells the 100 shares collateralising an
open short call and leaves that call NAKED — the guards refuse the *option's*
leg while the *equity* leg sells normally, because nothing anywhere could ask
"are these shares spoken for?".

``shares_pledged_to_short_calls`` and ``held_shares_for_cover`` are that
question, made answerable. They only MEASURE; the refusals that consume them
land in later tasks (entry refuses an uncovered covered call, the close path
refuses to sell pledged shares, a monitor detects cover leaving).

THE TRI-STATE IS THE WHOLE POINT, and it is what most of this file tests::

    an int (including 0)  MEASURED   -- 0 means "nothing is pledged" / "we hold
                                        none", and the caller may proceed
    None                  UNKNOWN    -- the caller must REFUSE, not assume

Returning ``0`` on an unreadable book would strip a covered call of its cover
during a broker outage. That is the most expensive instance of this codebase's
recurring unknown-reads-as-zero defect — the same shape as ``get_positions()``
returning ``None`` (fetch failed) versus ``[]`` (genuinely flat), which all
three adapters honour and which mass-closed 8 real transactions on 2026-07-03
the one time it was conflated.

WHAT THE DOUBLES ARE. ``FakeOptionsAccount`` controls exactly two seams —
``open_option_orders_book_wide()`` and ``get_positions()`` — and everything
above them is the real code under test. Neither accessor is mocked.
"""
from itertools import count
from types import SimpleNamespace

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus,
)

_ids = count(1)


# ---------------------------------------------------------------------------
# order rows -- the SHAPE the platform really persists
#
# Copied from test_option_assignment_capacity_wiring._row on purpose: these two
# accessors read the SAME list as the reserve pool and the assignment exposure,
# so they must be exercised against the same row shape.
# ---------------------------------------------------------------------------
def _row(**kw):
    base = dict(id=next(_ids), account_id=1, symbol="AAPL", underlying_symbol="AAPL",
                quantity=None, filled_qty=None, side=None, status=OrderStatus.FILLED,
                asset_class=AssetClass.OPTION, multiplier=100, contract_symbol=None,
                option_type=None, strike=None, option_strategy=None,
                transaction_id=None, data={})
    base.update(kw)
    return SimpleNamespace(**base)


def occ(underlying="AAPL", right=OptionRight.CALL, strike=150.0):
    return f"{underlying}260116{'C' if right == OptionRight.CALL else 'P'}{int(strike * 1000):08d}"


def leg(right=OptionRight.CALL, side=OrderDirection.SELL, qty=1, underlying="AAPL",
        strike=150.0, multiplier=100, status=OrderStatus.FILLED, contract=None,
        filled=True):
    """One option ORDER row: the identity the book carries per leg."""
    contract = contract if contract is not None else occ(underlying, right, strike)
    return _row(symbol=contract, contract_symbol=contract, underlying_symbol=underlying,
                option_type=right, side=side, strike=strike, multiplier=multiplier,
                status=status, quantity=qty, filled_qty=(qty if filled else None))


def short_call(**kw):
    return leg(right=OptionRight.CALL, side=OrderDirection.SELL, **kw)


def long_call(**kw):
    return leg(right=OptionRight.CALL, side=OrderDirection.BUY, **kw)


def short_put(**kw):
    return leg(right=OptionRight.PUT, side=OrderDirection.SELL, **kw)


def spread_parent(strategy="covered_call", underlying="AAPL"):
    """A multi-leg PARENT: carries the strategy and NO contract identity at all.

    Skipping it must not make the book unmeasurable — its legs carry the facts,
    one row each, and a parent flagged as "a short option with no type" would
    make every spread in the book permanently unknown.
    """
    return _row(symbol=underlying, underlying_symbol=underlying,
                option_strategy=strategy, side=OrderDirection.SELL,
                contract_symbol=None, option_type=None, quantity=1, filled_qty=1)


# ---------------------------------------------------------------------------
# position rows
# ---------------------------------------------------------------------------
def equity_position(symbol="AAPL", qty=100.0, side=OrderDirection.BUY,
                    asset_class="us_equity"):
    return SimpleNamespace(symbol=symbol, qty=qty, qty_available=qty, side=side,
                           asset_class=asset_class)


def option_position(symbol=None, qty=1.0):
    """What Alpaca hands back for a held option: get_positions() does NOT filter
    them out (``AlpacaAccount.get_positions`` maps every row from
    ``get_all_positions()``), so the accessor has to."""
    return SimpleNamespace(symbol=symbol if symbol is not None else occ(),
                           qty=qty, qty_available=qty, side=OrderDirection.SELL,
                           asset_class="us_option")


# ---------------------------------------------------------------------------
# the account
# ---------------------------------------------------------------------------
@pytest.fixture
def errors(monkeypatch):
    """Every ``logger.error`` the accessors emit, as strings.

    NOT ``caplog``: the package logger sets ``propagate = False``
    (``packages/common/ba2_common/logger.py``), so caplog's root handler never sees
    the record — the same reason ``test_option_parent_identity`` patches the shared
    logger OBJECT. The modules under test import it inside the function body, so
    there is no module attribute to patch instead.
    """
    from ba2_common.logger import logger
    captured: list = []
    monkeypatch.setattr(logger, "error", lambda msg, *a, **k: captured.append(str(msg)))
    return captured


class _Boom(OSError):
    """A broker/DB outage: the world being uncooperative, not a defect here."""


class FakeOptionsAccount(OptionsAccountInterface):
    """An options account with a CONTROLLED book and a CONTROLLED position list.

    Pass an exception instance for either to make that seam RAISE.
    """

    def __init__(self, book=(), positions=()):
        self.id = 7
        self._book = book
        self._positions = positions

    def open_option_orders_book_wide(self):
        if isinstance(self._book, BaseException):
            raise self._book
        return self._book

    def get_positions(self):
        if isinstance(self._positions, BaseException):
            raise self._positions
        return self._positions

    # --- unused abstract bits
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        return []

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return None

    def get_option_positions(self):
        return []

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        raise AssertionError("no test here submits an order")

    def close_option_position(self, position, order_type="limit", limit_price=None):
        raise AssertionError("no test here closes a position")


# ===========================================================================
# shares_pledged_to_short_calls -- MEASURED zeros
# ===========================================================================
def test_an_empty_book_pledges_nothing_and_says_so_as_a_measured_zero():
    """0, not None: the book was READ and it holds no options. The caller proceeds."""
    assert FakeOptionsAccount(book=[]).shares_pledged_to_short_calls("AAPL") == 0


def test_one_short_call_pledges_one_hundred_shares():
    acct = FakeOptionsAccount(book=[short_call(qty=1)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


def test_three_contracts_pledge_three_hundred_shares():
    acct = FakeOptionsAccount(book=[short_call(qty=3)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 300


def test_two_separate_short_calls_pledge_the_sum_of_both():
    """Different strikes are different contracts and both need cover."""
    acct = FakeOptionsAccount(book=[short_call(qty=1, strike=150.0),
                                    short_call(qty=2, strike=160.0)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 300


def test_a_long_call_pledges_nothing():
    """Owning a call obliges us to nothing. Only the SHORT side can be called away."""
    acct = FakeOptionsAccount(book=[long_call(qty=5)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


def test_a_short_put_pledges_no_shares():
    """A short put obliges CASH, which is the assignment-capacity gate's question.

    Counting it here would refuse to sell shares that nothing has a claim on.
    """
    acct = FakeOptionsAccount(book=[short_put(qty=3)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


def test_a_short_call_on_a_different_underlying_is_not_counted():
    acct = FakeOptionsAccount(book=[short_call(qty=4, underlying="MSFT")])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


def test_the_underlying_is_matched_case_and_whitespace_insensitively():
    acct = FakeOptionsAccount(book=[short_call(qty=1, underlying=" aapl ")])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


def test_a_short_call_bought_back_pledges_nothing_again():
    """Netted per contract, exactly as short_put_assignment_exposure nets.

    The shares are released the moment the call is closed; a pledge that only
    ever ratcheted up would freeze an account's stock permanently.
    """
    contract = occ()
    acct = FakeOptionsAccount(book=[
        short_call(qty=2, contract=contract),
        long_call(qty=2, contract=contract),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


def test_a_partially_bought_back_short_call_still_pledges_the_remainder():
    contract = occ()
    acct = FakeOptionsAccount(book=[
        short_call(qty=3, contract=contract),
        long_call(qty=1, contract=contract),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") == 200


def test_an_unfilled_sell_to_open_already_pledges_the_shares():
    """It can fill at any moment and can only ever ADD an obligation.

    The mirror of the exposure view's pending-short rule: an in-flight
    BUY-to-close has closed nothing and must not hand the cover back early.
    """
    acct = FakeOptionsAccount(book=[short_call(qty=1, status=OrderStatus.ACCEPTED,
                                               filled=False)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


def test_an_unfilled_buy_to_close_does_not_release_the_cover_early():
    contract = occ()
    acct = FakeOptionsAccount(book=[
        short_call(qty=1, contract=contract),
        long_call(qty=1, contract=contract, status=OrderStatus.ACCEPTED, filled=False),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


def test_a_multi_leg_parent_row_is_skipped_and_its_short_call_leg_is_counted():
    """A parent carries no contract, no right and no size -- the legs do."""
    acct = FakeOptionsAccount(book=[spread_parent(), short_call(qty=1),
                                    long_call(qty=1, strike=160.0)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


# ===========================================================================
# shares_pledged_to_short_calls -- the MULTIPLIER, read per contract
# ===========================================================================
def test_an_adjusted_contract_pledges_its_own_multiplier_not_one_hundred():
    """OPT-L7: a post-split / adjusted contract can deliver a different number
    of shares. Assuming 100 under-reports the pledge on exactly the contract
    where getting it wrong is least visible."""
    acct = FakeOptionsAccount(book=[short_call(qty=1, multiplier=130)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 130


def test_a_short_call_with_no_multiplier_is_unmeasurable():
    """None, not 100. Guessing here silently under-reports the pledge."""
    acct = FakeOptionsAccount(book=[short_call(qty=1, multiplier=None)])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


@pytest.mark.parametrize("bad", ["", "many", 0, -100, float("nan"),
                                 float("inf"), True])
def test_an_unreadable_multiplier_is_unmeasurable(bad):
    acct = FakeOptionsAccount(book=[short_call(qty=1, multiplier=bad)])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_numeric_STRING_multiplier_is_readable():
    """Recorded as a decision, not an accident: ``"100"`` is not ambiguous, and
    ``must_measure`` accepts numeric strings for the same reason (broker payloads
    arrive that way). It is ``""`` / ``"many"`` / ``True`` that are refused."""
    acct = FakeOptionsAccount(book=[short_call(qty=1, multiplier="100")])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


def test_two_rows_of_one_contract_disagreeing_on_the_multiplier_are_unmeasurable():
    """One contract cannot deliver two different share counts; one row is wrong
    and there is no way to tell which."""
    contract = occ()
    acct = FakeOptionsAccount(book=[
        short_call(qty=2, contract=contract, multiplier=100),
        long_call(qty=1, contract=contract, multiplier=130),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_missing_multiplier_on_a_FLAT_contract_does_not_poison_the_answer():
    """The pledge is unknown only where something is actually pledged.

    A closed-out call whose buy-back row lost its multiplier pledges nothing at
    all, and refusing every share sale over it would be a false refusal.
    """
    contract = occ()
    acct = FakeOptionsAccount(book=[
        short_call(qty=1, contract=contract, multiplier=100),
        long_call(qty=1, contract=contract, multiplier=None),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


# ===========================================================================
# shares_pledged_to_short_calls -- UNMEASURABLE
# ===========================================================================
def test_a_book_that_raises_is_unmeasurable(errors):
    """A broker/DB outage is not a flat book. 0 here strips the cover from every
    covered call in the account for the duration of the outage."""
    acct = FakeOptionsAccount(book=_Boom("the option book is unreachable"))
    assert acct.shares_pledged_to_short_calls("AAPL") is None
    assert any("UNKNOWN" in m for m in errors), \
        f"an unmeasurable pledge must be logged at ERROR, not swallowed; got {errors!r}"


def test_a_book_that_returns_none_is_unmeasurable():
    """``open_option_orders_book_wide`` is annotated ``List``, so this is
    off-contract -- which is exactly when a defensive ``for o in (book or [])``
    would silently report a flat book."""
    acct = FakeOptionsAccount(book=None)
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_short_option_with_no_recorded_type_is_unmeasurable():
    """Whether it is a CALL that pledges shares is unknown, and unknown must not
    resolve to 'not a call'."""
    row = short_call(qty=1)
    row.option_type = None
    acct = FakeOptionsAccount(book=[row])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_LONG_option_with_no_recorded_type_is_not_an_unknown():
    """A BUY we fail to recognise can only fail to RELIEVE a short, which
    overstates the pledge -- conservative, so it is not flagged."""
    row = long_call(qty=1)
    row.option_type = None
    acct = FakeOptionsAccount(book=[row, short_call(qty=1, strike=160.0)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 100


def test_a_short_call_with_no_recorded_underlying_is_unmeasurable():
    """It could be a call on the very ticker being asked about. Skipping it
    would report the shares as free."""
    row = short_call(qty=1)
    row.underlying_symbol = None
    acct = FakeOptionsAccount(book=[row])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_short_PUT_with_no_recorded_underlying_is_still_not_a_pledge():
    """The right is known and it is a put -- whose ticker it is cannot change
    that it pledges no shares."""
    row = short_put(qty=1)
    row.underlying_symbol = None
    acct = FakeOptionsAccount(book=[row])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


@pytest.mark.parametrize("bad_qty", [None, 0, -1, "", "many"])
def test_a_short_call_with_no_usable_quantity_is_unmeasurable(bad_qty):
    row = short_call(qty=1)
    row.quantity = bad_qty
    row.filled_qty = None
    acct = FakeOptionsAccount(book=[row])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_blank_underlying_argument_is_unmeasurable():
    """"Shares pledged to short calls on nothing" has no answer, and 0 would be
    read as 'nothing is pledged'."""
    acct = FakeOptionsAccount(book=[short_call(qty=1)])
    assert acct.shares_pledged_to_short_calls("  ") is None
    assert acct.shares_pledged_to_short_calls(None) is None


def test_one_unreadable_row_makes_the_WHOLE_pledge_unknown():
    """Not "the part we could read". A partial sum is a smaller number that
    looks exactly like a measured one, and the caller would free the difference.
    """
    good = short_call(qty=1, strike=150.0)
    bad = short_call(qty=1, strike=160.0)
    bad.multiplier = None
    assert FakeOptionsAccount(book=[good]).shares_pledged_to_short_calls("AAPL") == 100
    assert FakeOptionsAccount(book=[good, bad]).shares_pledged_to_short_calls("AAPL") is None


# ===========================================================================
# held_shares_for_cover
# ===========================================================================
def test_a_failed_position_fetch_is_unmeasurable(errors):
    """``None`` from get_positions() is the FETCH FAILING, and the tri-state
    contract on that seam is load-bearing (2026-07-03: a DNS outage read as a
    flat book and force-closed 8 real transactions)."""
    acct = FakeOptionsAccount(positions=None)
    assert acct.held_shares_for_cover("AAPL") is None
    assert any("UNKNOWN" in m for m in errors), \
        f"an unmeasurable holding must be logged at ERROR, not swallowed; got {errors!r}"


def test_a_position_fetch_that_raises_is_unmeasurable():
    acct = FakeOptionsAccount(positions=_Boom("broker unreachable"))
    assert acct.held_shares_for_cover("AAPL") is None


def test_a_genuinely_flat_account_holds_a_MEASURED_zero():
    """``[]`` is an answer: the broker confirmed it holds nothing."""
    assert FakeOptionsAccount(positions=[]).held_shares_for_cover("AAPL") == 0


def test_a_real_holding_reports_its_quantity():
    acct = FakeOptionsAccount(positions=[equity_position(qty=250.0)])
    assert acct.held_shares_for_cover("AAPL") == 250


def test_only_the_asked_for_symbol_is_counted():
    acct = FakeOptionsAccount(positions=[equity_position(symbol="MSFT", qty=900.0)])
    assert acct.held_shares_for_cover("AAPL") == 0


def test_two_lots_of_the_same_symbol_are_summed():
    acct = FakeOptionsAccount(positions=[equity_position(qty=100.0),
                                         equity_position(qty=40.0)])
    assert acct.held_shares_for_cover("AAPL") == 140


def test_an_option_row_is_not_equity_cover():
    """Alpaca's get_positions() returns option rows too. One contract is not one
    share, and counting it would report 1 share of cover for a 100-share
    obligation."""
    acct = FakeOptionsAccount(positions=[option_position(qty=1.0),
                                         equity_position(qty=100.0)])
    assert acct.held_shares_for_cover("AAPL") == 100


def test_an_option_row_reported_under_the_UNDERLYING_ticker_is_still_skipped():
    """IBKR builds its Position from ``ib_pos.contract.symbol``, which for an
    option is the UNDERLYING -- so the symbol filter alone would not catch it."""
    acct = FakeOptionsAccount(positions=[option_position(symbol="AAPL", qty=2.0)])
    assert acct.held_shares_for_cover("AAPL") == 0


@pytest.mark.parametrize("bad_qty", [None, "", "lots", float("nan")])
def test_a_position_row_with_no_readable_quantity_is_unmeasurable(bad_qty):
    """Not 0. "The broker holds AAPL but will not say how much" is precisely the
    case where selling the lot could uncover a call."""
    acct = FakeOptionsAccount(positions=[equity_position(qty=bad_qty)])
    assert acct.held_shares_for_cover("AAPL") is None


def test_an_unreadable_quantity_on_ANOTHER_symbol_does_not_poison_the_answer():
    acct = FakeOptionsAccount(positions=[equity_position(symbol="MSFT", qty=None),
                                         equity_position(qty=100.0)])
    assert acct.held_shares_for_cover("AAPL") == 100


def test_a_SHORT_equity_position_is_negative_cover_not_positive():
    """TastyTrade reports a short as a POSITIVE qty with ``side=SELL`` (Alpaca
    reports it negative). Reading the magnitude alone would report 100 shares of
    cover for an account that is short 100 shares and owns none."""
    tastytrade_short = equity_position(qty=100.0, side=OrderDirection.SELL)
    alpaca_short = equity_position(qty=-100.0, side=OrderDirection.SELL)
    assert FakeOptionsAccount(positions=[tastytrade_short]).held_shares_for_cover("AAPL") == -100
    assert FakeOptionsAccount(positions=[alpaca_short]).held_shares_for_cover("AAPL") == -100


def test_a_dict_shaped_position_row_is_read_the_same_way():
    """``get_available_position_quantity`` already accepts either shape on this
    seam; a cover reader that silently saw nothing in a dict book would report a
    flat account."""
    acct = FakeOptionsAccount(positions=[{"symbol": "AAPL", "qty": 100.0,
                                          "asset_class": "us_equity",
                                          "side": OrderDirection.BUY}])
    assert acct.held_shares_for_cover("AAPL") == 100


def test_the_symbol_is_matched_case_and_whitespace_insensitively():
    acct = FakeOptionsAccount(positions=[equity_position(symbol=" aapl ", qty=100.0)])
    assert acct.held_shares_for_cover("AAPL") == 100


def test_a_blank_symbol_argument_is_unmeasurable():
    acct = FakeOptionsAccount(positions=[equity_position(qty=100.0)])
    assert acct.held_shares_for_cover("") is None
    assert acct.held_shares_for_cover(None) is None


# ===========================================================================
# the two together -- the scenario that motivated the pair
# ===========================================================================
def test_the_allocation_run_can_now_see_that_the_shares_are_spoken_for():
    """100 shares held, one short call written against them: selling the 100 is
    what leaves the call naked, and BOTH numbers are needed to see it."""
    acct = FakeOptionsAccount(book=[short_call(qty=1)],
                              positions=[equity_position(qty=100.0)])
    held = acct.held_shares_for_cover("AAPL")
    pledged = acct.shares_pledged_to_short_calls("AAPL")
    assert (held, pledged) == (100, 100)
    assert held - pledged == 0, "not one share of this position is free to sell"


def test_an_account_with_no_short_calls_has_every_share_free():
    acct = FakeOptionsAccount(book=[long_call(qty=1)],
                              positions=[equity_position(qty=100.0)])
    assert acct.held_shares_for_cover("AAPL") - \
        acct.shares_pledged_to_short_calls("AAPL") == 100
