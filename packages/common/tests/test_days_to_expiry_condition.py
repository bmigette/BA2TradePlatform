"""``DaysToExpiryCondition`` — remaining option life as a rule condition.

The rule engine had no way to express "how much life is LEFT" on an option structure:
``days_opened`` counts days ELAPSED, which is a different quantity and diverges the
moment the entry DTE window is itself a gene. Roll-at-DTE therefore lived only inside
``OptionPortfolioManager`` and no ruleset could reach it.

The governing rule here is the one ``option_lifecycle`` was written to enforce:
**unknown is never a value.** An expiry that cannot be determined must make the
condition UNEVALUABLE — never a permissive default, never a number that reads as a
measurement. The two failure modes this file exists to prevent:

* a missing expiry read as ``0`` DTE  -> ``days_to_expiry <= 21`` fires an immediate
  close on every position whose expiry we could not see;
* a missing expiry read as ``+inf``   -> the exit is silently dead, the GA tunes a gene
  that cannot fire (exactly the dead roll-DTE gene that burned a whole campaign).

Both are pinned below, in both operator directions.

Every test freezes the clock by construction: the evaluation instant is the
recommendation's ``created_at`` (2024 sim dates), and the wall clock is 2026, so any
implementation that reads ``date.today()`` produces a wildly different number.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


# The simulated evaluation bar. Deliberately in the past, and deliberately NOT today:
# a condition that reads the wall clock cannot accidentally agree with these numbers.
SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
SIM_TODAY = date(2024, 6, 15)


def _setup_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "days_to_expiry.sqlite"))
    db.init_db()
    return db


class _FakeAccount:
    """Minimal stand-in: the condition never asks the account anything."""
    id = 1


def _rec(as_of=SIM_AS_OF, symbol="AAPL"):
    return SimpleNamespace(created_at=as_of, instance_id=1, symbol=symbol)


def _option_txn(db, *, expiry=None, symbol="AAPL", strategy="bull_call_spread"):
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import AssetClass, OrderDirection, TransactionStatus

    txn = Transaction(
        symbol=symbol,
        quantity=1,
        side=OrderDirection.BUY,
        status=TransactionStatus.OPENED,
        open_date=SIM_AS_OF - timedelta(days=40),
        asset_class=AssetClass.OPTION,
        option_strategy=strategy,
        multiplier=100,
        expiry=expiry,
    )
    return db.add_instance(txn)


def _parent_order(db, txn_id, *, expiry=None, symbol="AAPL"):
    """The multi-leg PARENT: option asset class, no contract_symbol."""
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import (AssetClass, OrderDirection, OrderStatus, OrderType)

    order = TradingOrder(
        account_id=1, symbol=symbol, quantity=1,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, underlying_symbol=symbol,
        option_strategy="bull_call_spread", multiplier=100,
        expiry=expiry,
        created_at=datetime.now(timezone.utc),  # wall clock (2026) on purpose
    )
    db.add_instance(order, expunge_after_flush=True)
    return order


def _leg(db, txn_id, contract, *, side, expiry, symbol="AAPL", status=None, qty=1):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import (AssetClass, OrderStatus, OrderType, OptionRight)

    leg = TradingOrder(
        account_id=1, symbol=symbol, quantity=qty,
        side=side, order_type=OrderType.MARKET,
        status=status or OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=contract,
        underlying_symbol=symbol, option_type=OptionRight.CALL,
        strike=100.0, multiplier=100, expiry=expiry, open_price=1.0,
        filled_qty=qty,
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(leg, expunge_after_flush=True)
    return leg


def _cond(order, *, op="<=", value=21, rec=None):
    from ba2_common.core.TradeConditions import DaysToExpiryCondition
    return DaysToExpiryCondition(
        account=_FakeAccount(), instrument_name="AAPL",
        expert_recommendation=rec or _rec(), operator_str=op, value=value,
        existing_order=order,
    )


# ---------------------------------------------------------------------------
# where the expiry comes from
# ---------------------------------------------------------------------------
def test_expiry_from_the_transaction(tmp_path):
    """Priority 1: ``Transaction.expiry`` (Task 1's column, Task 3's stamping)."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=30))
    parent = _parent_order(db, txn_id)          # no expiry on the parent
    cond = _cond(parent)

    assert cond.evaluate() is False             # 30 DTE is not <= 21
    assert cond.get_calculated_value() == 30


def test_expiry_from_the_multileg_parent_order(tmp_path):
    """Priority 2: the parent ``TradingOrder.expiry`` when the transaction has none.

    This is the row Task 3 started stamping. It was NULL for every multi-leg before,
    which is precisely why roll-at-DTE never fired.
    """
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=SIM_TODAY + timedelta(days=12))
    cond = _cond(parent)

    assert cond.evaluate() is True              # 12 <= 21
    assert cond.get_calculated_value() == 12


def test_expiry_derived_from_held_legs_when_neither_parent_nor_txn_has_one(tmp_path):
    """Priority 3: the legs. Historical rows predate both stamps, and
    ``option_lifecycle._dte`` derives from held legs for exactly this reason."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    exp = SIM_TODAY + timedelta(days=9)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL240920C00100000", side=OrderDirection.BUY, expiry=exp)
    _leg(db, txn_id, "AAPL240920C00110000", side=OrderDirection.SELL, expiry=exp)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 9


def test_single_leg_option_uses_its_own_expiry(tmp_path):
    """A single-leg option order IS the leg; its own ``expiry`` answers."""
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import (AssetClass, OrderDirection, OrderStatus,
                                       OptionRight, OrderType)

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None, strategy="long_call")
    order = TradingOrder(
        account_id=1, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol="AAPL240920C00100000",
        underlying_symbol="AAPL", option_type=OptionRight.CALL, strike=100.0,
        multiplier=100, expiry=SIM_TODAY + timedelta(days=5), open_price=2.0,
        filled_qty=1, created_at=datetime.now(timezone.utc),
    )
    db.add_instance(order, expunge_after_flush=True)

    cond = _cond(order)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 5


def test_a_leg_with_no_expiry_does_not_veto_the_legs_that_have_one(tmp_path):
    """``_dte``: "a leg that simply has no expiry adds no information"."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    exp = SIM_TODAY + timedelta(days=14)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL240920C00100000", side=OrderDirection.BUY, expiry=exp)
    _leg(db, txn_id, "AAPL240920C00110000", side=OrderDirection.SELL, expiry=None)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 14


def test_a_closed_leg_does_not_contribute_its_expiry(tmp_path):
    """Netting matters. A leg bought back to close nets to zero and stops counting —
    otherwise a rolled-off leg's stale expiry manufactures a permanent contradiction and
    the condition goes dark for the rest of the structure's life."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    live = SIM_TODAY + timedelta(days=18)
    dead = SIM_TODAY + timedelta(days=200)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_LIVE", side=OrderDirection.SELL, expiry=live)
    # opened then fully closed -> nets to 0, must not be consulted
    _leg(db, txn_id, "AAPL_DEAD", side=OrderDirection.SELL, expiry=dead)
    _leg(db, txn_id, "AAPL_DEAD", side=OrderDirection.BUY, expiry=dead)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 18


def test_an_unexecuted_leg_does_not_contribute_its_expiry(tmp_path):
    """A CANCELLED/REJECTED leg never existed as a position; its expiry must not
    manufacture a contradiction with the legs that actually filled."""
    from ba2_common.core.types import OrderDirection, OrderStatus

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_OK", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=7))
    _leg(db, txn_id, "AAPL_CANCELLED", side=OrderDirection.BUY,
         expiry=SIM_TODAY + timedelta(days=400), status=OrderStatus.CANCELED)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 7


# ---------------------------------------------------------------------------
# unknown is never a value
# ---------------------------------------------------------------------------
def test_no_expiry_anywhere_does_not_read_as_zero_dte(tmp_path):
    """THE failure mode. A structure whose expiry we cannot see must NOT satisfy
    ``days_to_expiry <= 21`` — that would flatten every such position on sight."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)   # and no legs at all

    cond = _cond(parent, op="<=", value=21)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_no_expiry_anywhere_does_not_read_as_infinity(tmp_path):
    """The mirror image: nor may it satisfy ``days_to_expiry > 5``. A permissive
    default in EITHER direction is a measurement we did not make."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)

    cond = _cond(parent, op=">", value=5)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_unknown_never_satisfies_any_operator(tmp_path):
    """Swept over the whole operator vocabulary, because ``!=`` and ``==`` are where a
    False-means-equal reading would quietly become a real answer."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)

    for op in (">", ">=", "<", "<=", "==", "!="):
        cond = _cond(parent, op=op, value=0)
        assert cond.evaluate() is False, f"unknown fired for operator {op!r}"
        assert cond.get_calculated_value() is None


def test_conflicting_held_leg_expiries_are_unevaluable(tmp_path):
    """Two held legs, two expiries. That is a contradiction, not a measurement — and
    NOT ``min()`` (which would close early) nor ``max()`` (which would never close)."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_NEAR", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=3))
    _leg(db, txn_id, "AAPL_FAR", side=OrderDirection.BUY,
         expiry=SIM_TODAY + timedelta(days=90))

    assert _cond(parent, op="<=", value=21).evaluate() is False   # not min() == 3
    assert _cond(parent, op=">", value=21).evaluate() is False    # not max() == 90
    assert _cond(parent).get_calculated_value() is None


def test_transaction_and_leg_disagreement_is_unevaluable(tmp_path):
    """A declared expiry that its own held legs contradict is not a fact about the
    position. Priority must not paper over the contradiction."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=30))
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_LEG", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=3))

    cond = _cond(parent, op="<=", value=21)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_transaction_and_parent_order_disagreement_is_unevaluable(tmp_path):
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=30))
    parent = _parent_order(db, txn_id, expiry=SIM_TODAY + timedelta(days=10))

    cond = _cond(parent, op="<=", value=21)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_agreeing_sources_are_not_a_conflict(tmp_path):
    """The symmetric guard: transaction, parent and legs all saying the same date must
    resolve, not trip the contradiction check."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    exp = SIM_TODAY + timedelta(days=20)
    txn_id = _option_txn(db, expiry=exp)
    parent = _parent_order(db, txn_id, expiry=exp)
    _leg(db, txn_id, "AAPL_A", side=OrderDirection.SELL, expiry=exp)
    _leg(db, txn_id, "AAPL_B", side=OrderDirection.BUY, expiry=exp)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 20


def test_no_existing_order_is_unevaluable(tmp_path):
    """An entry-rule context has no position; there is no remaining life to measure.

    The reason must NAME that (``LIFECYCLE_UNKNOWN``'s contract: say which input was
    missing) — "no expiry recorded anywhere" would be a different, misleading diagnosis
    for an evaluation that never had a position to look at.
    """
    _setup_db(tmp_path)
    cond = _cond(None, op="<=", value=21)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None
    assert "no open position" in cond.get_actual_value_display()


def test_a_partially_closed_leg_still_counts(tmp_path):
    """Netting is by QUANTITY, not by order count. Sold 2, bought 1 back -> one contract
    is still held and its expiry is still the structure's. Counting orders instead of
    contracts nets this to zero and the position goes dark."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    exp = SIM_TODAY + timedelta(days=13)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_PART", side=OrderDirection.SELL, expiry=exp, qty=2)
    _leg(db, txn_id, "AAPL_PART", side=OrderDirection.BUY, expiry=exp, qty=1)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 13


def test_netting_counts_the_FILLED_quantity(tmp_path):
    """A partial fill is what the book actually holds. Netting the ORDERED size instead
    leaves a fully-closed contract looking half-open, and its expiry then contradicts the
    legs that really are held — a position that goes dark for no reason."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    # ordered 2, only 1 filled ... then that 1 bought back -> flat
    partial = _leg(db, txn_id, "AAPL_PARTIAL", side=OrderDirection.SELL,
                   expiry=SIM_TODAY + timedelta(days=120), qty=2)
    partial.filled_qty = 1
    db.update_instance(partial)
    _leg(db, txn_id, "AAPL_PARTIAL", side=OrderDirection.BUY,
         expiry=SIM_TODAY + timedelta(days=120), qty=1)
    # the leg that IS still held
    _leg(db, txn_id, "AAPL_HELD", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=16), qty=1)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 16


def test_a_row_that_moved_no_contracts_is_not_part_of_the_position(tmp_path):
    """A zero-quantity order moved nothing, so it says nothing about what is held. Letting
    it through means a stale/garbage row can inject an expiry that contradicts the real
    legs and blinds the condition permanently."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_HELD", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=17), qty=1)
    ghost = _leg(db, txn_id, "AAPL_HELD", side=OrderDirection.BUY,
                 expiry=SIM_TODAY + timedelta(days=365), qty=1)
    ghost.quantity = 0
    ghost.filled_qty = 0
    db.update_instance(ghost)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 17


def test_one_contract_with_two_different_expiries_is_unevaluable(tmp_path):
    """An OCC symbol determines its expiry, so two values on one contract is corrupt
    data. Keeping whichever row happened to be read first would be a silent default."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    _leg(db, txn_id, "AAPL_SAME", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=8), qty=2)
    _leg(db, txn_id, "AAPL_SAME", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=44), qty=1)

    cond = _cond(parent, op="<=", value=21)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_legs_of_other_transactions_are_never_consulted(tmp_path):
    """An order with no ``transaction_id`` must not borrow some other position's expiry.

    ``orders_where(transaction_id=None)`` does not filter — it returns EVERY order — so
    a missing transaction id has to short-circuit, or an orphan row silently inherits the
    expiry of whatever else is in the book.
    """
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import (AssetClass, OrderDirection, OrderStatus,
                                       OptionRight, OrderType)

    db = _setup_db(tmp_path)
    # an unrelated, fully-populated option position living in the same book
    other_txn = _option_txn(db, expiry=None, symbol="MSFT")
    _leg(db, other_txn, "MSFT_LEG", side=OrderDirection.SELL,
         expiry=SIM_TODAY + timedelta(days=2), symbol="MSFT")

    orphan = TradingOrder(
        account_id=1, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=None,
        asset_class=AssetClass.OPTION, contract_symbol="AAPL_ORPHAN",
        underlying_symbol="AAPL", option_type=OptionRight.CALL, strike=100.0,
        multiplier=100, expiry=None, open_price=1.0, filled_qty=1,
        created_at=datetime.now(timezone.utc))
    db.add_instance(orphan, expunge_after_flush=True)

    cond = _cond(orphan, op="<=", value=21)
    assert cond.evaluate() is False, "borrowed another position's expiry"
    assert cond.get_calculated_value() is None


def test_equity_order_is_unevaluable(tmp_path):
    """Stock has no expiry. It must read unknown, not 0 DTE."""
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import (OrderDirection, OrderStatus, OrderType,
                                       TransactionStatus)

    db = _setup_db(tmp_path)
    txn_id = db.add_instance(Transaction(
        symbol="AAPL", quantity=10, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=5)))
    order = TradingOrder(
        account_id=1, symbol="AAPL", quantity=10, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        transaction_id=txn_id, created_at=datetime.now(timezone.utc))
    db.add_instance(order, expunge_after_flush=True)

    cond = _cond(order, op="<=", value=21)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_missing_evaluation_date_is_unevaluable(tmp_path):
    """No as-of means no "today" — and substituting the wall clock in a backtest is the
    lookahead bug ``DaysOpenedCondition``'s docstring was written about."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=3))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, op="<=", value=21, rec=SimpleNamespace(instance_id=1,
                                                                created_at=None))
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


# ---------------------------------------------------------------------------
# the measurement itself
# ---------------------------------------------------------------------------
def test_remaining_days_not_elapsed_days(tmp_path):
    """``days_to_expiry`` is NOT ``days_opened``. Opened 40 sim-days ago, 10 days left:
    the answer is 10. An implementation that measured elapsed life would say 40 and a
    ``<= 21`` roll would never fire on a long-held position."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=10))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, op="<=", value=21)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 10


def test_uses_the_evaluation_bar_not_the_wall_clock(tmp_path):
    """Frozen by construction: the as-of is 2024 and the expiry is 2024, so a wall-clock
    implementation lands hundreds of days away (and negative)."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=45))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, op=">", value=30)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 45


def test_a_naive_as_of_is_handled(tmp_path):
    """Rows round-trip through naive DateTime columns; a naive as-of must still work."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=6))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, rec=_rec(as_of=datetime(2024, 6, 15, 15, 30)))
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 6


def test_a_naive_as_of_is_read_as_utc_not_as_local_time(tmp_path):
    """Naive datetimes in this codebase are UTC by convention (the DB columns strip
    tzinfo). Letting ``astimezone()`` reinterpret them in the MACHINE's local zone shifts
    an early-morning bar onto the previous calendar day and the DTE off by one — and the
    same backtest then measures a different quantity on a different developer's laptop."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=date(2024, 6, 22))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, rec=_rec(as_of=datetime(2024, 6, 15, 0, 30)))
    cond.evaluate()
    assert cond.get_calculated_value() == 7


def test_expiry_stored_as_a_datetime_is_handled(tmp_path):
    """Some writers hand back a ``datetime`` for a ``date`` column; the day count must
    not silently become a ``TypeError``-swallowed unknown."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id)
    parent.expiry = datetime(2024, 6, 25, 0, 0)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 10


@pytest.mark.parametrize("days_left,op,threshold,expected", [
    # the <= boundary, both sides and dead on it
    (22, "<=", 21, False),
    (21, "<=", 21, True),
    (20, "<=", 21, True),
    # < must NOT fire on the boundary (the <= / < mutation)
    (21, "<", 21, False),
    (20, "<", 21, True),
    # >= / > around the same point
    (21, ">=", 21, True),
    (21, ">", 21, False),
    (22, ">", 21, True),
    # the 0DTE arm: expiry day itself
    (0, "<=", 0, True),
    (1, "<=", 0, False),
    (0, "<", 0, False),
])
def test_threshold_boundaries(tmp_path, days_left, op, threshold, expected):
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=days_left))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, op=op, value=threshold)
    assert cond.evaluate() is expected
    assert cond.get_calculated_value() == days_left


def test_expiry_day_itself_is_zero_dte_not_one(tmp_path):
    """Off-by-one guard, stated on its own: on the expiry date the answer is 0."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY)
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, op="<=", value=0)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 0


def test_time_of_day_does_not_shift_the_day_count(tmp_path):
    """Calendar days, from the evaluation DATE. A 23:59 bar and a 00:01 bar on the same
    session must report the same DTE, or the exit fires a day early on late bars."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=7))
    parent = _parent_order(db, txn_id)

    early = _cond(parent, rec=_rec(as_of=datetime(2024, 6, 15, 0, 1, tzinfo=timezone.utc)))
    late = _cond(parent, rec=_rec(as_of=datetime(2024, 6, 15, 23, 59, tzinfo=timezone.utc)))
    early.evaluate()
    late.evaluate()
    assert early.get_calculated_value() == late.get_calculated_value() == 7


def test_past_expiry_reports_a_negative_dte_not_zero(tmp_path):
    """An open structure past its expiry is a real (alarming) state. Report -3, not a
    comforting 0 and not a clamp: a clamp to 0 makes ``> 0`` say "still alive"."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY - timedelta(days=3))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent, op="<=", value=0)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == -3
    assert _cond(parent, op=">", value=0).evaluate() is False


# ---------------------------------------------------------------------------
# display: an unknown must not render as a number
# ---------------------------------------------------------------------------
def test_display_reports_the_day_count(tmp_path):
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=11))
    parent = _parent_order(db, txn_id)
    cond = _cond(parent)
    cond.evaluate()
    assert cond.get_actual_value_display() == "11 DTE"


def test_display_says_unknown_and_why(tmp_path):
    """``LIFECYCLE_UNKNOWN`` carries a ``detail`` naming the missing input; so does this.
    The audit row must never show a plausible number for a measurement we did not make."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    cond = _cond(parent)
    cond.evaluate()

    shown = cond.get_actual_value_display()
    assert shown is not None
    assert "unknown" in shown.lower()
    assert "expiry" in shown.lower()
    # and it must not be parseable as a DTE
    with pytest.raises(ValueError):
        float(shown.split()[0])


def test_a_second_evaluation_does_not_keep_the_first_ones_number(tmp_path):
    """Conditions are re-evaluated bar after bar on the same object. A stale
    ``calculated_value`` left over from a bar that COULD be measured turns a later
    unknown into a confident (and wrong) reading — the exact "unknown wearing a
    measurement's clothes" this condition exists to prevent."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=SIM_TODAY + timedelta(days=4))
    cond = _cond(parent, op="<=", value=21)

    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 4

    parent.expiry = None                       # the expiry is no longer visible
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None
    assert "unknown" in cond.get_actual_value_display().lower()


def test_a_successful_evaluation_clears_any_earlier_unknown_reason(tmp_path):
    """``unknown_reason`` is the ``LIFECYCLE_UNKNOWN`` detail channel. Left stale it
    reports a missing input on a bar that measured one perfectly well."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=None)
    parent = _parent_order(db, txn_id, expiry=None)
    cond = _cond(parent)

    assert cond.evaluate() is False
    assert cond.unknown_reason

    parent.expiry = SIM_TODAY + timedelta(days=4)
    assert cond.evaluate() is True
    assert cond.unknown_reason is None
    assert cond.get_actual_value_display() == "4 DTE"


def test_an_unnamed_defect_propagates(tmp_path):
    """``failure_modes``' deny-by-default: the broad ``except Exception`` must not turn a
    code defect into "no expiry today". The ATR bug this project is still paying for was
    exactly that — a ``TypeError`` indistinguishable from legitimately-absent data."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=30))
    parent = _parent_order(db, txn_id)

    cond = _cond(parent)
    cond._resolve_expiry = lambda: (_ for _ in ()).throw(RuntimeError("defect"))
    with pytest.raises(RuntimeError):
        cond.evaluate()


def test_an_absorbed_transport_error_is_unevaluable_not_an_exit_trigger(tmp_path):
    """The last line of defence, on the path that IS absorbed (``OSError`` is benign by
    default). It must land in the SAME unknown state as a missing expiry — never a
    fabricated 0 DTE (which closes everything) and never a True (same, louder)."""
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY + timedelta(days=30))
    parent = _parent_order(db, txn_id)

    def _boom():
        raise OSError("book unreadable")

    for op, threshold in (("<=", 21), (">", 5), ("==", 0), ("!=", 0)):
        cond = _cond(parent, op=op, value=threshold)
        cond._resolve_expiry = _boom
        assert cond.evaluate() is False, f"errored evaluation fired for {op}"
        assert cond.get_calculated_value() is None
        assert "unknown" in cond.get_actual_value_display().lower()


def test_display_marks_an_expired_structure(tmp_path):
    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, expiry=SIM_TODAY - timedelta(days=2))
    parent = _parent_order(db, txn_id)
    cond = _cond(parent)
    cond.evaluate()
    assert "expired" in cond.get_actual_value_display().lower()


def test_description_names_the_operator_and_threshold(tmp_path):
    _setup_db(tmp_path)
    cond = _cond(None, op="<=", value=21)
    desc = cond.get_description()
    assert "21" in desc and "<=" in desc


# ---------------------------------------------------------------------------
# registration / wiring
# ---------------------------------------------------------------------------
def test_event_type_exists_and_is_numeric():
    from ba2_common.core.types import (ExpertEventType, get_numeric_event_values,
                                       is_numeric_event)
    assert ExpertEventType.N_DAYS_TO_EXPIRY.value == "days_to_expiry"
    assert is_numeric_event("days_to_expiry")
    assert "days_to_expiry" in get_numeric_event_values()


def test_factory_builds_the_condition(tmp_path):
    from ba2_common.core.TradeConditions import DaysToExpiryCondition, create_condition
    from ba2_common.core.types import ExpertEventType

    _setup_db(tmp_path)
    cond = create_condition(
        event_type=ExpertEventType.N_DAYS_TO_EXPIRY, account=_FakeAccount(),
        instrument_name="AAPL", expert_recommendation=_rec(),
        existing_order=None, operator_str="<=", value=21)
    assert isinstance(cond, DaysToExpiryCondition)


def test_field_reaches_the_engine_as_a_trigger():
    """A rule-tree leaf naming ``days_to_expiry`` must become a real trigger.

    ``triggers_from_condition_tree`` SILENTLY DROPS fields missing from ``FIELD_EVENT``
    ("an unknown field is skipped"), so a condition that exists but is not registered
    there is a gene the GA can tune and the engine can never see.
    """
    from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree
    from ba2_common.core.types import ExpertEventType

    assert FIELD_EVENT["days_to_expiry"] is ExpertEventType.N_DAYS_TO_EXPIRY
    triggers = triggers_from_condition_tree({"type": "AND", "conditions": [
        {"id": "dte", "field": "days_to_expiry", "op": "<=", "value": 21}]})
    assert list(triggers.values()) == [
        {"event_type": "days_to_expiry", "operator": "<=", "value": 21}]


def test_event_type_is_documented():
    from ba2_common.core.rules_documentation import get_event_type_documentation
    doc = get_event_type_documentation()["days_to_expiry"]
    assert doc["type"] == "numeric"
    assert doc["name"]
