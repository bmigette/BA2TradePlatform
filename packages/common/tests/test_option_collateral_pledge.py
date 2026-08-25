"""Are these shares SPOKEN FOR? — the two cover accessors (OPT-L1).

THE HOLE THESE CLOSE. Seam 1 stopped the wrong-instrument order: an option
transaction can no longer be routed through the equity close/adjust paths, and
options no longer enter the allocation plan. It did NOT stop the dangerous half.
A "set AAPL to 0%" allocation run still sells the 100 shares collateralising an
open short call and leaves that call NAKED — the guards refuse the *option's*
leg while the *equity* leg sells normally, because nothing anywhere could ask
"are these shares spoken for?".

``shares_pledged_to_short_calls`` and ``held_shares_for_cover`` are that
question, made answerable. They only MEASURE. The FIRST refusal to consume them
is at the bottom of this file: ``submit_option_order`` now refuses a
``covered_call`` whose cover is short or unmeasurable, before it writes a row.
The remaining two land in later tasks (the close path refuses to sell pledged
shares; a monitor detects cover leaving).

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
from datetime import date
from itertools import count
from types import SimpleNamespace

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces.OptionsAccountInterface import (
    COVER_REFUSAL, DEFAULT_OPTION_MULTIPLIER, OptionsAccountInterface,
)
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_types import OptionLeg
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


def partly_filled(ordered, filled, right=OptionRight.CALL,
                  side=OrderDirection.SELL, **kw):
    """A sell-to-open still WORKING at the broker with part of it done.

    ``PARTIALLY_FILLED`` is an EXECUTED status
    (``OrderStatus.get_executed_statuses``), which is what makes this shape
    interesting: the filled part nets like a fill while the remainder is as
    in-flight as an untouched NEW order. ``leg()`` cannot build it — it ties
    ``filled_qty`` to ``quantity`` — so the two sizes are set explicitly here.
    """
    row = leg(right=right, side=side, qty=ordered,
              status=OrderStatus.PARTIALLY_FILLED, **kw)
    row.filled_qty = filled
    return row


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


# ---------------------------------------------------------------------------
# the PARTIAL-FILL window
#
# ``PARTIALLY_FILLED`` is an EXECUTED status, so the old ``filled_qty if
# filled_qty else quantity`` read the filled part and nothing else: the unfilled
# remainder was netted nowhere and pledged nothing. A sell-to-open of 3 with 1
# filled reported 100 shares pledged instead of 300, and during that window a
# consumer frees 200 shares the next fill leaves NAKED. The docstring's own rule
# for an in-flight SELL -- "it can fill at any moment and can only ever ADD an
# obligation" -- applies to the remaining 2 contracts verbatim.
# ---------------------------------------------------------------------------
def test_a_partially_filled_sell_to_open_pledges_its_WHOLE_ordered_size():
    """3 contracts sold, 1 filled: 300 shares are spoken for, not 100."""
    acct = FakeOptionsAccount(book=[partly_filled(ordered=3, filled=1)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 300


def test_the_unfilled_remainder_is_pledged_on_TOP_of_a_holding():
    """The two halves are counted once each, not one instead of the other."""
    acct = FakeOptionsAccount(book=[partly_filled(ordered=3, filled=1)],
                              positions=[equity_position(qty=300.0)])
    assert acct.held_shares_for_cover("AAPL") - \
        acct.shares_pledged_to_short_calls("AAPL") == 0, \
        "not one of the 300 shares is free while the other 2 contracts are working"


def test_the_remainder_of_a_partial_fill_is_never_netted_away_by_a_buy_to_close():
    """It goes to the in-flight total, not to ``net``.

    Buying back the 1 contract that FILLED releases that contract's cover and
    nothing else — the 2 still working at the broker are untouched by it.
    """
    contract = occ()
    acct = FakeOptionsAccount(book=[
        partly_filled(ordered=3, filled=1, contract=contract),
        long_call(qty=1, contract=contract),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") == 200


def test_a_fully_filled_sell_has_no_remainder_to_add():
    """The regression guard for every ordinary short call in the book.

    Recorded as belt-and-braces rather than as a discriminating test: TWO
    independent things keep a FILLED row out of the remainder (the
    ``PARTIALLY_FILLED`` status test, and ``ordered - filled`` being zero once
    ``raw_qty`` has fallen back to ``quantity``), so no single mutation of either
    can make it fail. It is here because this is the shape of nearly every row in
    a live book, and a partial-fill change that broke it would be the most
    expensive way to be wrong. ``test_a_partially_filled_sell_to_open_pledges_its
    _WHOLE_ordered_size`` and ``test_a_filled_qty_ABOVE_the_ordered_quantity...``
    are the two that DO discriminate the arithmetic.
    """
    row = short_call(qty=2)
    assert row.status == OrderStatus.FILLED and row.filled_qty == row.quantity
    assert FakeOptionsAccount(book=[row]).shares_pledged_to_short_calls("AAPL") == 200


def test_a_filled_qty_ABOVE_the_ordered_quantity_does_not_hand_cover_back():
    """A damaged row is not a negative obligation.

    ``ordered - filled`` would be -1 contract here, and adding it would report
    the pledge as 100 shares smaller than the fills alone already prove.
    """
    acct = FakeOptionsAccount(book=[partly_filled(ordered=2, filled=3)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 300


def test_a_partial_fill_whose_ORDERED_quantity_is_unreadable_is_unmeasurable():
    """How much is still working at the broker is an obligation of unknown size.

    ``filled_qty`` alone is readable, so the old code answered with it and looked
    measured. ``quantity`` is non-nullable on ``TradingOrder``, so this is a
    damaged row rather than a routine one — and a damaged row is not a small one.
    """
    row = partly_filled(ordered=3, filled=1)
    row.quantity = None
    assert FakeOptionsAccount(book=[row]).shares_pledged_to_short_calls("AAPL") is None


def test_a_partially_filled_BUY_to_close_still_releases_only_what_filled():
    """The mirror: an in-flight BUY has closed nothing and must not hand the
    cover back early, whichever part of it has filled."""
    contract = occ()
    acct = FakeOptionsAccount(book=[
        short_call(qty=3, contract=contract),
        partly_filled(ordered=3, filled=1, side=OrderDirection.BUY,
                      contract=contract),
    ])
    assert acct.shares_pledged_to_short_calls("AAPL") == 200


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
# a damaged IDENTITY is deferred per CONTRACT, exactly like the multiplier
#
# The false-refusal argument written for the multiplier applies word for word to
# ``option_type`` and ``underlying_symbol``: a contract that is FLAT by the end
# of the book pledges nothing whatever the missing field would have said. Before
# this, a call fully bought back whose SELL row had lost one of the two made
# every future reading of that ticker UNKNOWN forever -- and once the exit guard
# consumes this number, "forever unknown" stops being a blocked write and
# becomes a permanently blocked SHARE SALE.
#
# (The missing-QUANTITY case genuinely cannot be deferred and is not: a size we
# cannot read cannot be netted off, so there is no way to learn it went flat.)
# ===========================================================================
def test_a_bought_back_call_whose_SELL_row_lost_its_option_type_is_measurable():
    contract = occ()
    damaged = short_call(qty=1, contract=contract)
    damaged.option_type = None
    acct = FakeOptionsAccount(book=[damaged, long_call(qty=1, contract=contract)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


def test_a_bought_back_call_whose_SELL_row_lost_its_underlying_is_measurable():
    contract = occ()
    damaged = short_call(qty=1, contract=contract)
    damaged.underlying_symbol = None
    acct = FakeOptionsAccount(book=[damaged, long_call(qty=1, contract=contract)])
    assert acct.shares_pledged_to_short_calls("AAPL") == 0


def test_a_damaged_row_is_netted_PESSIMISTICALLY_so_a_PARTIAL_buy_back_still_refuses():
    """The deferral must only ever be discharged by a real offsetting BUY.

    The damaged SELL is counted as a short call on this very ticker, so 3 sold
    against 1 bought back is still net short 2 and still UNKNOWN — the deferral
    is not "ignore the row", it is "ask again once the book says it is flat".
    """
    contract = occ()
    damaged = short_call(qty=3, contract=contract)
    damaged.option_type = None
    acct = FakeOptionsAccount(book=[damaged, long_call(qty=1, contract=contract)])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


def test_a_damaged_row_on_ANOTHER_contract_does_not_release_this_one():
    """Deferral is per CONTRACT. A flat contract elsewhere in the book cannot
    discharge a damaged row on the contract that is actually short."""
    damaged = short_call(qty=1, strike=150.0)
    damaged.underlying_symbol = None
    flat = occ(strike=160.0)
    acct = FakeOptionsAccount(book=[damaged,
                                    short_call(qty=1, contract=flat, strike=160.0),
                                    long_call(qty=1, contract=flat, strike=160.0)])
    assert acct.shares_pledged_to_short_calls("AAPL") is None


# ===========================================================================
# WHICH exceptions are benign -- and which must reach the operator
#
# ``_cover_benign_errors`` named ``SQLAlchemyError``, whose subtree also holds
# ``ProgrammingError`` ("no such column: multiplier"), ``IntegrityError``,
# ``InvalidRequestError``/``DetachedInstanceError`` and ``ArgumentError``: every
# one a DEFECT, not a data condition. Absorbed, they answer UNKNOWN forever,
# which the accessors' own comment calls "a gate that has silently stopped
# working" -- and which, at the exit guard, refuses every share sale on every
# underlying until someone notices. Narrowed to ``OperationalError``, the one
# class that means what the justification says.
#
# Both halves are pinned for BOTH accessors. Before this, the only exception
# double in the file was an ``OSError`` subclass, benign via the DEFAULT set, so
# the custom list was never exercised and the propagating half was never tested
# at all: ``except Exception: return None`` would have broken nothing.
# ===========================================================================
def _locked_db():
    """The condition the benign entry exists for: a transient, real-world DB lock."""
    from sqlalchemy.exc import OperationalError
    return OperationalError("SELECT 1", {}, Exception("database is locked"))


def _schema_defect():
    """A ``SQLAlchemyError`` that is a DEFECT: the query names a column that
    does not exist. Not transient, not the world's fault, and permanent."""
    from sqlalchemy.exc import ProgrammingError
    return ProgrammingError("SELECT multiplier FROM tradingorder", {},
                            Exception("no such column: multiplier"))


@pytest.mark.parametrize("accessor, seam", [
    ("shares_pledged_to_short_calls", "book"),
    ("held_shares_for_cover", "positions"),
])
def test_a_locked_database_is_absorbed_into_an_UNMEASURABLE_answer(accessor, seam,
                                                                   errors):
    acct = FakeOptionsAccount(**{seam: _locked_db()})
    assert getattr(acct, accessor)("AAPL") is None
    assert any("UNKNOWN" in m for m in errors), errors


@pytest.mark.parametrize("accessor, seam", [
    ("shares_pledged_to_short_calls", "book"),
    ("held_shares_for_cover", "positions"),
])
def test_a_TYPE_ERROR_from_the_seam_PROPAGATES(accessor, seam):
    """A bad row shape is a defect in this program. It must reach the operator,
    not become a permanent "unmeasurable" that reads as a working safety gate."""
    acct = FakeOptionsAccount(**{seam: TypeError("a row is not what this expects")})
    with pytest.raises(TypeError):
        getattr(acct, accessor)("AAPL")


@pytest.mark.parametrize("accessor, seam", [
    ("shares_pledged_to_short_calls", "book"),
    ("held_shares_for_cover", "positions"),
])
def test_a_SCHEMA_DEFECT_PROPAGATES_even_though_it_is_a_SQLAlchemyError(accessor, seam):
    """The exact probe that motivated the narrowing. A missing column is a
    permanent defect wearing a database-shaped exception; absorbing it turns
    every share sale on every underlying into a refusal, indefinitely."""
    from sqlalchemy.exc import ProgrammingError

    acct = FakeOptionsAccount(**{seam: _schema_defect()})
    with pytest.raises(ProgrammingError):
        getattr(acct, accessor)("AAPL")


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


# ===========================================================================
# the CASH-side twin -- short_put_assignment_exposure, same partial-fill window
#
# It is tested HERE rather than beside its own tests because it inherited the
# defect from the same line of code and is fixed in the same commit. The two
# views deliberately consume ONE query (``open_option_orders_book_wide``), so
# they must agree about what an in-flight obligation is; leaving one of them
# under-reporting the same window would be worse than either behaviour alone.
# ===========================================================================
def test_a_partially_filled_short_put_is_charged_its_WHOLE_ordered_size():
    """3 puts sold at 150, 1 filled: assignment would cost 45,000, not 15,000.

    Under-charging is what admits the NEXT structure on capacity the remaining
    two fills are about to consume — the exact defect ``assignment_capacity``
    exists to prevent, one status early.
    """
    acct = FakeOptionsAccount(book=[partly_filled(ordered=3, filled=1,
                                                  right=OptionRight.PUT)])
    exposure = acct.short_put_assignment_exposure()
    assert exposure.cost == pytest.approx(45_000.0)
    assert exposure.contracts == pytest.approx(3.0)


def test_a_fully_filled_short_put_is_not_double_charged():
    """The boundary: FILLED has no remainder, and charging one would price every
    ordinary cash-secured put in the book at twice its strike."""
    acct = FakeOptionsAccount(book=[short_put(qty=2)])
    assert acct.short_put_assignment_exposure().cost == pytest.approx(30_000.0)


def test_the_remainder_of_a_partially_filled_short_put_is_never_netted_away():
    """Buying back the filled contract relieves that one and no more."""
    contract = occ(right=OptionRight.PUT)
    acct = FakeOptionsAccount(book=[
        partly_filled(ordered=3, filled=1, right=OptionRight.PUT, contract=contract),
        leg(right=OptionRight.PUT, side=OrderDirection.BUY, qty=1, contract=contract),
    ])
    assert acct.short_put_assignment_exposure().cost == pytest.approx(30_000.0)


def test_a_partially_filled_short_put_with_an_unreadable_ordered_size_is_unmeasurable():
    row = partly_filled(ordered=3, filled=1, right=OptionRight.PUT)
    row.quantity = None
    exposure = FakeOptionsAccount(book=[row]).short_put_assignment_exposure()
    assert exposure.cost is None
    assert str(row.id) in "; ".join(exposure.unmeasurable), exposure.unmeasurable


def test_a_short_put_filled_ABOVE_its_ordered_size_buys_no_capacity_back():
    acct = FakeOptionsAccount(book=[partly_filled(ordered=2, filled=3,
                                                  right=OptionRight.PUT)])
    assert acct.short_put_assignment_exposure().cost == pytest.approx(45_000.0)


# ===========================================================================
# THE ENTRY SEAM — ``submit_option_order`` refuses an UNCOVERED covered call
#
# The measurement above is only worth having if something REFUSES on it. The
# first consumer is the submission boundary itself, and it is placed there
# rather than in ``SellCoveredCallAction`` because that action is not the only
# caller: ``PremiumSeller.rebalance`` and ``OptionPortfolioManager`` reach
# ``submit_option_order`` directly, and it validated nothing but leg count and
# a single expiry. Anything else could write a naked short call under the
# ``covered_call`` tag with no test in the repo to notice.
#
# WHY BOTH ACCESSORS AND NOT JUST ``held``. ``SellCoveredCallAction`` sizes
# with ``floor(held / 100)`` and consults NO short-call book, so a second
# covered call written against the SAME 100 shares passes a held-only check
# — three contracts against one 100-share lot is the documented failure. The
# free cover is ``held - pledged``; either half UNKNOWN is a refusal, and the
# two refusals read differently because a broken position feed and a broken
# option book send the operator to different places.
# ===========================================================================
AUG = date(2026, 8, 21)


def option_leg(strike=150.0, side=OrderDirection.SELL, right=OptionRight.CALL,
               underlying="AAPL", intent="sell_to_open", expiry=AUG, **extra):
    """One ``OptionLeg`` as the entry actions build it, plus any extra attribute.

    ``**extra`` exists for ``multiplier=``: the dataclass carries no such field
    today, and a leg that DOES publish one must be believed over the platform
    default (OPT-L7 — an adjusted contract does not deliver 100 shares).
    """
    built = OptionLeg(contract_symbol=occ(underlying, right, strike), side=side,
                      position_intent=intent, option_type=right, strike=strike,
                      expiry=expiry, underlying=underlying)
    for name, value in extra.items():
        setattr(built, name, value)
    return built


#: "this leg publishes NO multiplier field at all" — today's ``OptionLeg``. Distinct
#: from publishing one whose value happens to be the platform default.
_ABSENT = object()


class SubmittingAccount(FakeOptionsAccount):
    """``FakeOptionsAccount`` that can run the REAL ``submit_option_order``.

    Only the broker wire and the transaction factory are stubbed; the guard,
    the persistence and both cover accessors are the code under test. The book
    and the position list stay controllable, which is the whole point: the
    refusals turn on exactly those two seams.
    """

    def __init__(self, book=(), positions=()):
        super().__init__(book=book, positions=positions)
        self.submitted: list = []

    def _create_transaction_for_order(self, trading_order):
        trading_order.transaction_id = add_instance(Transaction(
            symbol=trading_order.symbol, quantity=trading_order.quantity,
            side=trading_order.side, multiplier=100, expert_id=None))

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        self.submitted.append((trading_order, list(legs)))
        trading_order.status = OrderStatus.FILLED
        trading_order.broker_order_id = f"double-{trading_order.id}"
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = OrderStatus.FILLED
            update_instance(child)
        return trading_order


class RoundTripAccount(SubmittingAccount):
    """``SubmittingAccount`` whose option book is the ROWS IT HAS WRITTEN.

    ``FakeOptionsAccount`` pins the book to a fixture list, which is what makes
    the refusal tests above exact — but it also means those tests can never see
    what the write PERSISTED. This one restores the real
    ``open_option_orders_book_wide`` (over the in-RAM store the ``store``
    fixture installs), so a covered call can be written and then re-read through
    ``shares_pledged_to_short_calls``: the only way to compare the cover the
    guard demanded with the cover the resulting book reports.
    """

    def open_option_orders_book_wide(self):
        return OptionsAccountInterface.open_option_orders_book_wide(self)


@pytest.fixture
def store():
    """A fresh in-RAM order/transaction store per test, so "nothing was written"
    is an exact statement rather than a hopeful one."""
    with ts.inmem_trades() as s:
        yield s


def _write(account, legs=None, quantity=1, strategy="covered_call"):
    return account.submit_option_order(
        legs if legs is not None else [option_leg()], quantity=quantity,
        order_type="limit", limit_price=2.5, option_strategy=strategy)


class TestTheEntrySeamRefusesAnUncoveredCoveredCall:

    # -- the shortfall ----------------------------------------------------
    def test_a_covered_call_against_no_shares_at_all_is_refused(self, store):
        """A MEASURED zero holding is still zero cover. This is the naked write."""
        acct = SubmittingAccount(book=[], positions=[])

        with pytest.raises(ValueError) as excinfo:
            _write(acct)

        message = str(excinfo.value)
        assert COVER_REFUSAL in message, message
        assert "AAPL" in message, message
        assert "100 share" in message, "the requirement must be named: " + message
        assert "short by 100" in message, "the SHORTFALL must be named: " + message
        assert acct.submitted == [], "the broker saw a naked short call"

    def test_three_contracts_against_one_hundred_shares_is_refused(self, store):
        """The documented failure: partial cover is not cover."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, quantity=3)

        message = str(excinfo.value)
        assert "300 share" in message and "short by 200" in message, message
        assert acct.submitted == []

    def test_exactly_enough_shares_is_admitted(self, store):
        """The normal covered call must be completely unaffected."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])

        parent = _write(acct)

        assert parent is not None and parent.status == OrderStatus.FILLED
        assert len(acct.submitted) == 1, "the broker must be reached exactly once"
        assert len(store.all(TradingOrder)) == 1
        assert len(store.all(Transaction)) == 1

    def test_one_share_short_of_the_lot_is_refused(self, store):
        """99 shares do not cover a 100-share obligation. The boundary is exact."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=99.0)])

        with pytest.raises(ValueError, match="short by 1 "):
            _write(acct)

    # -- the case a held-only check misses --------------------------------
    def test_shares_already_pledged_to_another_short_call_cover_nothing(self, store):
        """100 shares, one short call ALREADY written against them: the second
        call is naked even though ``held >= contracts * 100`` is satisfied.

        This is precisely what ``SellCoveredCallAction``'s ``floor(held / 100)``
        cannot see, because it consults no short-call book at all.
        """
        acct = SubmittingAccount(book=[short_call(qty=1)],
                                 positions=[equity_position(qty=100.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct)

        message = str(excinfo.value)
        assert "100 already pledged" in message, message
        assert "short by 100" in message, message
        assert acct.submitted == []

    def test_a_bought_back_call_releases_its_cover_again(self, store):
        """The pledge nets: a call bought back stops pledging, so the shares
        are writable again. A refusal that never lifts would be its own bug."""
        acct = SubmittingAccount(book=[short_call(qty=1), long_call(qty=1)],
                                 positions=[equity_position(qty=100.0)])

        parent = _write(acct)

        assert parent is not None and len(acct.submitted) == 1

    # -- UNKNOWN is a refusal, and the two unknowns read differently ------
    def test_an_unmeasurable_holding_is_refused_and_names_the_position_feed(
            self, store, errors):
        """``get_positions()`` returning None is a FETCH FAILURE, not a flat
        account. Writing a covered call on that is how one goes naked."""
        acct = SubmittingAccount(book=[], positions=None)

        with pytest.raises(ValueError) as excinfo:
            _write(acct)

        message = str(excinfo.value)
        assert COVER_REFUSAL in message, message
        assert "held_shares_for_cover" in message, message
        assert "POSITION" in message, "the operator must be sent to the position feed"
        assert "option book" not in message.lower(), (
            "the option book was readable — blaming it sends the operator to the "
            "wrong system: " + message)
        assert acct.submitted == []
        assert errors, "the accessor must also log why it could not measure"

    def test_an_unmeasurable_pledge_is_refused_and_names_the_option_book(
            self, store, errors):
        """A book that cannot be read is not an empty book. The shares may
        already be spoken for and there is no way to find out."""
        acct = SubmittingAccount(book=None, positions=[equity_position(qty=100.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct)

        message = str(excinfo.value)
        assert COVER_REFUSAL in message, message
        assert "shares_pledged_to_short_calls" in message, message
        assert "OPTION BOOK" in message, "the operator must be sent to the option book"
        assert "position feed" not in message.lower(), (
            "the position feed was readable — blaming it sends the operator to the "
            "wrong system: " + message)
        assert acct.submitted == []
        assert errors, "the accessor must also log why it could not measure"

    def test_an_unreadable_short_call_row_in_the_book_is_refused(self, store):
        """The unknown does not have to be a whole-feed outage — one damaged
        row is enough, because a partial pledge looks exactly like a measured one."""
        acct = SubmittingAccount(book=[short_call(qty=1, multiplier=None)],
                                 positions=[equity_position(qty=1000.0)])

        with pytest.raises(ValueError, match="OPTION BOOK"):
            _write(acct)

    # -- the requirement and the ROWS are the same number (OPT-L7) ---------
    #
    # The guard used to prefer a multiplier the LEG published. That is correct
    # arithmetic against a row this method cannot write: it stamps
    # DEFAULT_OPTION_MULTIPLIER on the parent AND on every leg child,
    # unconditionally, and ``shares_pledged_to_short_calls`` reads those rows back.
    # A leg publishing 10 was therefore validated against 30 shares for 3
    # contracts, ADMITTED, and the rows it wrote reported 300 pledged — the gate
    # approved one position and created another. The requirement is now the number
    # that will be persisted, and a leg claiming any other delivery size is
    # REFUSED rather than believed (validate a position we cannot record) or
    # ignored (drop an adjusted contract's delivery size on the floor).
    def test_a_leg_publishing_a_multiplier_the_rows_cannot_record_is_refused(self, store):
        """The reviewer's scenario, flipped. 3 contracts x 10 shares against a
        30-share holding: arithmetically covered, and unwritable."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=30.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(multiplier=10)], quantity=3)

        message = str(excinfo.value)
        assert COVER_REFUSAL in message, message
        assert str(DEFAULT_OPTION_MULTIPLIER) in message, (
            "name the number the rows WILL carry: " + message)
        assert acct.submitted == []

    def test_a_shares_rich_account_does_not_rescue_a_divergent_multiplier(self, store):
        """It is not a shortfall and more shares do not clear it. Refusing only
        when the cover happens to be short would still write the row whose
        multiplier is a lie, just with enough shares to hide it."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=1_000_000.0)])

        with pytest.raises(ValueError, match=COVER_REFUSAL):
            _write(acct, legs=[option_leg(multiplier=10)], quantity=3)

    def test_a_larger_multiplier_is_refused_for_the_same_reason(self, store):
        """The dangerous direction lands in the same place: a leg claiming 500
        shares per contract cannot be recorded either, and admitting it against
        a big holding would write rows that under-report the pledge 5:1."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=1_000_000.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(multiplier=500)])

        assert "500" in str(excinfo.value), str(excinfo.value)

    @pytest.mark.parametrize("published", [_ABSENT, 100, 100.0, "100", 10, 500, 0, "junk"])
    def test_a_covered_call_is_admitted_only_when_the_rows_will_pledge_what_it_demanded(
            self, store, published):
        """ADMITTED IMPLIES AGREEMENT — for every multiplier a leg can publish.

        The invariant, not a case list: either the write is refused (and pledges
        nothing at all), or it is admitted and the rows it wrote pledge EXACTLY
        the cover the guard demanded. Both numbers are measured, neither is
        asserted from the source: the guard's bar is located behaviourally (it
        admits at N free shares and refuses at N-1) and N is compared against
        what a re-read of the persisted rows reports as pledged.

        This is the test the old behaviour fails, in both directions —
        ``multiplier=10`` was admitted at 30 shares and pledged 300, and
        ``multiplier=500`` demanded 1,500 and pledged 300.
        """
        legs = ([option_leg()] if published is _ABSENT
                else [option_leg(multiplier=published)])
        acct = RoundTripAccount(positions=[equity_position(qty=1_000_000.0)])

        if not acct.check_cover_for_covered_call(legs, 3, "covered_call").ok:
            with pytest.raises(ValueError, match=COVER_REFUSAL):
                _write(acct, legs=legs, quantity=3)
            assert acct.shares_pledged_to_short_calls("AAPL") == 0, \
                "a refused covered call still pledged shares"
            return

        assert _write(acct, legs=legs, quantity=3) is not None
        pledged = acct.shares_pledged_to_short_calls("AAPL")

        def admits(shares):
            return SubmittingAccount(
                book=[], positions=[equity_position(qty=float(shares))]
            ).check_cover_for_covered_call(legs, 3, "covered_call").ok

        assert admits(pledged), (
            f"the rows pledge {pledged} shares, which the guard itself calls too "
            f"few to write this position")
        assert not admits(pledged - 1), (
            f"the rows pledge {pledged} shares but the guard would have written "
            f"this position against {pledged - 1} — it validated a smaller "
            f"position than it created")

    def test_a_leg_whose_multiplier_is_unreadable_is_refused(self, store):
        """A leg that publishes the field but not a number is UNKNOWN, and reads
        differently from one that publishes an honest number the rows cannot
        carry: this one is a damaged input, and the remedy is to repair it."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100000.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(multiplier=0)])

        message = str(excinfo.value)
        assert COVER_REFUSAL in message and "multiplier" in message, message
        assert str(DEFAULT_OPTION_MULTIPLIER) in message, (
            "say what was NOT assumed: " + message)

    def test_a_leg_carrying_no_multiplier_field_uses_the_platform_default(self, store):
        """``OptionLeg`` has no ``multiplier`` today — and that ABSENCE is what
        makes the default honest rather than a guess: it is the very number the
        two row writes below stamp. Refusing an absent field would refuse every
        covered call on the platform; believing a published one would validate a
        position those writes cannot record.

        THIS ASSERTION IS A TRIPWIRE. Adding ``multiplier`` to ``OptionLeg`` must
        change the parent write, the leg-child write and this guard TOGETHER, and
        it fires here first.
        """
        assert not hasattr(option_leg(), "multiplier"), (
            "OptionLeg now publishes a multiplier: teach submit_option_order's TWO "
            "row writes to persist it before the cover guard is allowed to believe it")
        acct = SubmittingAccount(
            book=[], positions=[equity_position(qty=float(DEFAULT_OPTION_MULTIPLIER))])

        parent = _write(acct)

        assert parent is not None
        assert parent.multiplier == DEFAULT_OPTION_MULTIPLIER

    def test_a_leg_publishing_the_platform_default_is_admitted(self, store):
        """The field is not banned — the DIVERGENCE is. A leg that says 100 says
        what the rows say, and agrees by construction."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])

        parent = _write(acct, legs=[option_leg(multiplier=DEFAULT_OPTION_MULTIPLIER)])

        assert parent is not None and len(acct.submitted) == 1

    # -- the requirement rounds UP ----------------------------------------
    def test_the_requirement_is_rounded_UP_never_truncated(self, store):
        """100.5 shares of obligation is a shortfall against 100 held, not a fit.

        Every other requirement in this file is a whole number of shares, so
        replacing ``math.ceil`` with a plain ``int()`` truncation leaves them all
        green while under-stating a fractional one — the direction that leaves a
        contract partly uncovered. ``ratio_qty`` is the only input that can be
        fractional and nothing writes one today; the guard accepts any readable
        positive number for it, and this pins which way that rounds.
        """
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(ratio_qty=1.005)])

        message = str(excinfo.value)
        assert "needs 101 shares" in message, message
        assert "short by 1 " in message, message

    def test_float_dust_does_not_round_a_whole_requirement_up(self, store):
        """The other half of the same line: ``round(_, 6)`` before the ceil, so a
        300.0000000001 built out of float addition stays 300 and an exactly
        covered call is not refused by one imaginary share."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=300.0)])
        legs = [option_leg(strike=150.0, ratio_qty=1.0000000001),
                option_leg(strike=160.0), option_leg(strike=170.0)]

        parent = _write(acct, legs=legs)

        assert parent is not None and len(acct.submitted) == 1

    # -- inputs the guard itself cannot read -------------------------------
    #
    # Same discipline as the accessors: the obligation's SIZE is as much a part of
    # the measurement as the cover is, and a covered call whose size cannot be read
    # is an unknown obligation. Each of these would otherwise silently contribute
    # nothing to `required` and wave the write through.
    def test_an_unusable_order_quantity_is_refused(self, store):
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100000.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, quantity=0)

        assert COVER_REFUSAL in str(excinfo.value), str(excinfo.value)
        assert "quantity" in str(excinfo.value), str(excinfo.value)

    def test_a_short_leg_with_no_option_type_is_refused(self, store):
        """It might be the CALL. Unknown must not resolve to 'not a call'."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100000.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(right=None)])

        assert "no option type" in str(excinfo.value), str(excinfo.value)

    def test_a_short_call_with_no_underlying_is_refused(self, store):
        """There is no ticker whose shares could be counted, so nothing can be
        said about cover — least of all that there is enough."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100000.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(underlying=None)])

        assert "no underlying" in str(excinfo.value), str(excinfo.value)

    def test_a_leg_with_an_unusable_ratio_is_refused(self, store):
        """``ratio_qty`` sizes the leg (quantity * ratio_qty is how the children are
        written), so an unreadable one is an obligation of unknown size."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100000.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(ratio_qty=0)])

        assert "ratio_qty" in str(excinfo.value), str(excinfo.value)

    def test_the_ratio_multiplies_the_requirement(self, store):
        """And when it IS readable it must be honoured: 2 x ratio 3 is 6 contracts,
        i.e. 600 shares — not 200."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=500.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=[option_leg(ratio_qty=3)], quantity=2)

        assert "600 share" in str(excinfo.value), str(excinfo.value)

    # -- what the guard must NOT touch ------------------------------------
    def test_a_cash_secured_put_is_unaffected(self, store):
        """A short put obliges CASH, not shares — that is the assignment-capacity
        gate's question. Refusing it here would refuse a structure nothing covers.

        BELT AND BRACES, and recorded as such: TWO independent filters keep this
        write out of the guard (the strategy tag and the CALL-only test), so no
        single mutation of either can make this test fail. The two tests that DO
        discriminate them one at a time are ``test_a_bear_call_spread_is_unaffected``
        (tag) and ``test_a_short_put_riding_along_needs_no_share_cover`` (right).
        """
        acct = SubmittingAccount(book=[], positions=[])

        parent = _write(acct, legs=[option_leg(right=OptionRight.PUT)],
                        strategy="cash_secured_put")

        assert parent is not None and len(acct.submitted) == 1

    def test_a_short_put_riding_along_needs_no_share_cover(self, store):
        """Even UNDER the covered_call tag, only the CALL leg is covered by shares.

        A short put obliges CASH — that is ``check_assignment_capacity``'s question,
        and it has its own refusal with its own remedy. Counting it here would
        demand 200 shares for a structure that can only ever have 100 called away,
        and the two refusals would then contradict each other about what to fix.
        """
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])
        legs = [option_leg(strike=160.0),
                option_leg(strike=140.0, right=OptionRight.PUT)]

        parent = _write(acct, legs=legs)

        assert parent is not None and len(acct.submitted) == 1

    def test_a_bear_call_spread_is_unaffected(self, store):
        """Writing one is never REFUSED here: it answers for its short call with
        a LONG CALL, not with shares, and this guard enforces the promise the
        ``covered_call`` tag makes and no other.

        Not refused is not "outside the cover ledger" — see
        ``test_an_open_credit_spread_consumes_cover_on_the_same_ticker`` below,
        which pins what the spread does to the NEXT write.
        """
        acct = SubmittingAccount(book=[], positions=[])
        legs = [option_leg(strike=160.0),
                option_leg(strike=170.0, side=OrderDirection.BUY, intent="buy_to_open")]

        parent = _write(acct, legs=legs, strategy="bear_call_spread")

        assert parent is not None and len(acct.submitted) == 1

    def test_an_open_credit_spread_consumes_cover_on_the_same_ticker(self, store):
        """A covered-call sleeve and a credit-spread sleeve on one ticker will
        NOT both write, and the docs and the message must say so.

        ``shares_pledged_to_short_calls`` reads the ORDER BOOK, where a bear call
        spread's short leg is a short call like any other; the long wing beside
        it is not cover it can verify (nothing guarantees the long is exercised
        to satisfy an assignment, and it can itself have been sold). So the
        spread pledges 100 AAPL shares and the genuinely covered call written
        beside it is refused. That is fail-safe and deliberate — a short 160C
        really can have 100 shares called away — but it is a real constraint,
        and the remedy must point at the leg that is actually holding the pledge
        rather than at shares the operator already owns.
        """
        acct = SubmittingAccount(
            book=[short_call(qty=1, strike=160.0), long_call(qty=1, strike=170.0)],
            positions=[equity_position(qty=100.0)])

        with pytest.raises(ValueError) as excinfo:
            _write(acct)

        message = str(excinfo.value)
        assert "100 already pledged" in message, message
        assert "credit spread" in message, (
            "the remedy must name the leg that is holding the pledge, which the "
            "operator does not think of as consuming shares: " + message)

    def test_buying_a_covered_call_back_is_never_refused(self, store):
        """A BUY leg needs no cover — it RELEASES some. Refusing a close when
        the feeds are down would strand an open position that cannot be
        flattened, which is far worse than the entry it prevents."""
        acct = SubmittingAccount(book=None, positions=None)
        leg = option_leg(side=OrderDirection.BUY, intent="buy_to_close")

        parent = _write(acct, legs=[leg])

        assert parent is not None and len(acct.submitted) == 1

    def test_the_strategy_tag_is_matched_case_and_whitespace_insensitively(self, store):
        """``" Covered_Call "`` is the same promise. A tag comparison a caller
        can slip past by capitalising it is not a guard."""
        acct = SubmittingAccount(book=[], positions=[])

        with pytest.raises(ValueError, match=COVER_REFUSAL):
            _write(acct, strategy=" Covered_Call ")

    # -- nothing is half-written ------------------------------------------
    def test_a_refusal_writes_nothing(self, store):
        """Counted before and after. A refusal that leaves a parent, a leg or a
        Transaction behind is worse than the naked call it prevented: the book
        then claims a position the broker has never heard of."""
        assert store.all(TradingOrder) == [] and store.all(Transaction) == []
        orders_before = len(store.all(TradingOrder))
        txns_before = len(store.all(Transaction))
        acct = SubmittingAccount(book=[], positions=[])

        with pytest.raises(ValueError, match=COVER_REFUSAL):
            _write(acct, quantity=2)

        assert len(store.all(TradingOrder)) == orders_before, \
            "a refused covered call left order rows behind"
        assert len(store.all(Transaction)) == txns_before, \
            "a refused covered call left a Transaction behind"
        assert acct.submitted == []

    def test_a_refusal_on_an_unmeasurable_feed_writes_nothing_either(self, store):
        """Same property on the UNKNOWN branch, which is the one that fires
        during an outage — i.e. when half-written rows are hardest to spot."""
        with pytest.raises(ValueError, match=COVER_REFUSAL):
            _write(SubmittingAccount(book=None, positions=None))

        assert store.all(TradingOrder) == [] and store.all(Transaction) == []


# ===========================================================================
# THE REFUSAL CHANNEL — a VERDICT for callers that have one, the raise as backstop
#
# The decision is ``check_cover_for_covered_call``, which returns a
# ``CoverCapacity`` exactly as ``check_assignment_capacity`` returns an
# ``AssignmentCapacity``: the three rails on this entry path (buying power,
# assignment capacity, share cover) refuse with three different remedies and
# must all reach a caller the same way. ``SellCoveredCallAction`` consumes the
# verdict and records ``_result(False, ...)``
# (``tests/test_option_actions.py``); the seam keeps RAISING for the direct
# callers — ``PremiumSeller.rebalance``, ``OptionPortfolioManager`` — that have
# nowhere to put one, where the only alternative is a silent naked write.
#
# Both paths are pinned: every test in the class above still drives the seam and
# still requires a ``ValueError``.
# ===========================================================================
class TestTheCoverVerdictAndItsBackstop:

    def test_a_covered_write_returns_an_OK_verdict_with_no_reason(self, store):
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])

        verdict = acct.check_cover_for_covered_call(
            [option_leg()], 1, "covered_call")

        assert verdict.ok is True
        assert verdict.reason == "", "an OK verdict must carry no complaint"

    def test_a_strategy_this_guard_does_not_police_is_OK_not_silent(self, store):
        """The tag filter returns a VERDICT too. Returning ``None`` for the
        99% of writes that are not covered calls would make every caller's
        ``verdict.ok`` an AttributeError on the ordinary path."""
        acct = SubmittingAccount(book=[], positions=[])

        verdict = acct.check_cover_for_covered_call(
            [option_leg()], 1, "bear_call_spread")

        assert verdict.ok is True

    def test_a_shortfall_verdict_carries_the_figures_the_operator_needs(self, store):
        """``AssignmentCapacity``'s shape: the reason AND what was measured.
        "refused" and "refused, held 100 of the 300 needed" send someone to two
        different places, and the second is a number they can act on."""
        acct = SubmittingAccount(book=[short_call(qty=1)],
                                 positions=[equity_position(qty=200.0)])

        verdict = acct.check_cover_for_covered_call(
            [option_leg()], 3, "covered_call")

        assert verdict.ok is False
        assert COVER_REFUSAL in verdict.reason
        assert (verdict.underlying, verdict.required) == ("AAPL", 300)
        assert (verdict.held, verdict.pledged) == (200, 100)

    def test_an_unmeasurable_figure_arrives_as_None_never_as_a_zero(self, store):
        """The tri-state survives the trip into the verdict. A ``pledged`` of 0
        on an unreadable book would be read as "nothing is spoken for" — the
        exact conflation the accessors exist to prevent."""
        acct = SubmittingAccount(book=None, positions=[equity_position(qty=100.0)])

        verdict = acct.check_cover_for_covered_call(
            [option_leg()], 1, "covered_call")

        assert verdict.ok is False
        assert verdict.pledged is None, "UNKNOWN must not arrive as 0"
        assert verdict.held == 100

    def test_the_backstop_raises_the_verdicts_OWN_sentence_verbatim(self, store):
        """One refusal, one text. If the seam ever paraphrased, an operator
        reading a refused action result and an operator reading a direct
        caller's traceback would be comparing two different descriptions of the
        same event."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])
        legs, quantity = [option_leg()], 3
        verdict = acct.check_cover_for_covered_call(legs, quantity, "covered_call")
        assert verdict.ok is False

        with pytest.raises(ValueError) as excinfo:
            _write(acct, legs=legs, quantity=quantity)

        assert str(excinfo.value) == verdict.reason

    def test_asking_the_verdict_writes_nothing_and_reaches_no_broker(self, store):
        """It MEASURES. A check with a side effect could not be placed before
        the sizing decision it is meant to inform."""
        acct = SubmittingAccount(book=[], positions=[equity_position(qty=100.0)])

        acct.check_cover_for_covered_call([option_leg()], 1, "covered_call")

        assert store.all(TradingOrder) == [] and store.all(Transaction) == []
        assert acct.submitted == []
