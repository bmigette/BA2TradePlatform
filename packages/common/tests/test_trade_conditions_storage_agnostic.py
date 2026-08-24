"""Trade gates must behave identically wherever the rows are stored.

Split deliberately in two:

1. **Condition tests** — drive the conditions against a ``FakeTradeRepository`` holding plain
   lists. They mention no backend at all, because a condition has no business knowing whether
   its trades came from RAM or SQLite: it asks the repository and reasons about what it gets.
   This is the contract that was broken.

2. **Backend parity tests** — the only place storage is named. They seed the SAME data into the
   in-memory store and into SQLite and assert both ``TradeRepository`` implementations return
   the same answers, which is what makes (1) a valid proof for both execution modes.

WHY: ``DaysSinceLastCloseCondition`` queried storage directly with ``select(Transaction)``.
``Transaction`` is an ``IN_MEM_MODEL``, so under a RAM-only backtest that query returned EMPTY
rather than raising — with a close 3 days old and a ">15 day" cooldown the condition read the
1e9 "never closed" sentinel and allowed the entry. The gate was inert for a WHOLE optimization,
so the GA tuned a gene that did nothing: the same genome scored 103 trades / 17.55% annualised
in-memory and 169 / 0.20% on SQLite, and live (SQLite) behaved like the latter.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.trade_repository import (
    InMemoryTradeRepository, SqlTradeRepository, TradeRepository,
    reset_trade_repository, set_trade_repository,
)
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)
EXPERT_ID = 1
SYMBOL = "AAPL"


class FakeTradeRepository(TradeRepository):
    """A repository over plain lists — no storage, no backend, no setup."""

    def __init__(self, transactions=None, orders=None, recommendation_orders=None):
        self.rows: List[Transaction] = list(transactions or [])
        self.orders: List[TradingOrder] = list(orders or [])
        # Orders attributed via ExpertRecommendation rather than via a transaction.
        self.recommendation_orders: List[TradingOrder] = list(recommendation_orders or [])

    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        return next((t for t in self.rows if t.id == transaction_id), None)

    def transactions(self, *, expert_id, statuses, symbol=None):
        sset = set(statuses)
        return [t for t in self.rows
                if t.expert_id == expert_id
                and t.status in sset
                and (symbol is None or t.symbol == symbol)]

    def _orders_for_transactions(self, transaction_ids):
        wanted = set(transaction_ids)
        return [o for o in self.orders if o.transaction_id in wanted]

    def _orders_by_recommendation(self, *, expert_id, symbol=None):
        return [o for o in self.recommendation_orders
                if symbol is None or o.symbol == symbol]


class _Rec:
    """Minimal stand-in for the ExpertRecommendation the conditions read."""

    def __init__(self, expert_id=EXPERT_ID, created_at=NOW):
        self.instance_id = expert_id
        self.created_at = created_at
        self.symbol = SYMBOL


def _closed_txn(days_ago, expert_id=EXPERT_ID, symbol=SYMBOL, txn_id=None):
    return Transaction(
        id=txn_id, expert_id=expert_id, symbol=symbol, status=TransactionStatus.CLOSED,
        side=OrderDirection.BUY, quantity=1.0, open_price=100.0, close_price=110.0,
        open_date=NOW - timedelta(days=days_ago + 7), close_date=NOW - timedelta(days=days_ago),
    )


def _open_txn(side=OrderDirection.BUY, expert_id=EXPERT_ID, symbol=SYMBOL, txn_id=None):
    return Transaction(
        id=txn_id, expert_id=expert_id, symbol=symbol, status=TransactionStatus.OPENED,
        side=side, quantity=1.0, open_price=100.0, open_date=NOW - timedelta(days=2),
    )


def _option_order(txn_id, status=OrderStatus.FILLED, option_type=OptionRight.CALL,
                  side=OrderDirection.BUY, strategy=None, underlying=SYMBOL):
    return TradingOrder(
        account_id=1, symbol="AAPL240719C00200000", quantity=1.0, side=side,
        order_type=OrderType.MARKET, status=status, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, underlying_symbol=underlying,
        option_type=option_type, option_strategy=strategy,
    )


@pytest.fixture
def repo():
    """Inject a fake repository for the duration of one test, then restore auto-resolution."""
    fake = FakeTradeRepository()
    set_trade_repository(fake)
    yield fake
    reset_trade_repository()


# ---------------------------------------------------------------- conditions
def _days_since_close(value=15.0):
    from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition
    return DaysSinceLastCloseCondition(
        account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
        operator_str=">", value=value, existing_order=None,
    )


def test_cooldown_blocks_entry_while_the_last_close_is_recent(repo):
    """THE regression: a 3-day-old close must block a ">15 day" cooldown.

    Before the fix the condition never saw the close and allowed the entry, making the gate —
    and the GA gene tuning it — completely inert.
    """
    repo.rows.append(_closed_txn(days_ago=3))
    cond = _days_since_close()
    allowed = cond.evaluate()

    assert cond.calculated_value == 3.0
    assert allowed is False


def test_cooldown_allows_entry_once_elapsed(repo):
    """Proves the fix didn't just make the gate block unconditionally."""
    repo.rows.append(_closed_txn(days_ago=40))
    cond = _days_since_close()
    assert cond.evaluate() is True
    assert cond.calculated_value == 40.0


def test_cooldown_uses_the_most_recent_close(repo):
    """Ordering is applied by the repository, so newest-close-first must hold."""
    repo.rows += [_closed_txn(days_ago=40), _closed_txn(days_ago=2),
                          _closed_txn(days_ago=25)]
    cond = _days_since_close()
    cond.evaluate()
    assert cond.calculated_value == 2.0


def test_cooldown_ignores_other_experts_and_symbols(repo):
    repo.rows += [_closed_txn(days_ago=3, expert_id=999),
                          _closed_txn(days_ago=3, symbol="MSFT")]
    cond = _days_since_close()
    assert cond.evaluate() is True
    assert cond.calculated_value == 1e9, "another expert's/symbol's close must not count"


def test_buy_and_sell_position_conditions_discriminate_side(repo):
    from ba2_common.core.TradeConditions import (
        HasBuyPositionCondition, HasSellPositionCondition,
    )
    repo.rows.append(_open_txn(side=OrderDirection.BUY))
    buy = HasBuyPositionCondition(account=None, instrument_name=SYMBOL,
                                  expert_recommendation=_Rec(), existing_order=None)
    sell = HasSellPositionCondition(account=None, instrument_name=SYMBOL,
                                    expert_recommendation=_Rec(), existing_order=None)
    assert buy.evaluate() is True
    assert sell.evaluate() is False


def test_option_position_condition_resolves_the_transaction_to_order_link(repo):
    """The old SQL join had no in-memory equivalent; the repository expresses it as
    open-transactions -> their orders, so it must resolve without a join."""
    from ba2_common.core.TradeConditions import HasOptionPositionCondition
    repo.rows.append(_open_txn(txn_id=7))
    repo.orders.append(_option_order(txn_id=7))
    cond = HasOptionPositionCondition(account=None, instrument_name=SYMBOL,
                                      expert_recommendation=_Rec(), existing_order=None)
    assert cond.evaluate() is True


def test_option_position_condition_ignores_terminal_orders(repo):
    from ba2_common.core.TradeConditions import HasOptionPositionCondition
    repo.rows.append(_open_txn(txn_id=7))
    repo.orders.append(_option_order(txn_id=7, status=OrderStatus.CANCELED))
    cond = HasOptionPositionCondition(account=None, instrument_name=SYMBOL,
                                      expert_recommendation=_Rec(), existing_order=None)
    assert cond.evaluate() is False


def test_covered_call_condition_requires_the_matching_strategy(repo):
    from ba2_common.core.TradeConditions import HasCoveredCallCondition
    repo.rows.append(_open_txn(txn_id=7))
    repo.orders.append(_option_order(txn_id=7, option_type=OptionRight.CALL,
                                     side=OrderDirection.SELL, strategy="protective_put"))
    cond = HasCoveredCallCondition(account=None, instrument_name=SYMBOL,
                                   expert_recommendation=_Rec(), existing_order=None)
    assert cond.evaluate() is False, "a non-covered-call option leg must not satisfy it"

    repo.orders.append(_option_order(txn_id=7, option_type=OptionRight.CALL,
                                     side=OrderDirection.SELL, strategy="covered_call"))
    cond2 = HasCoveredCallCondition(account=None, instrument_name=SYMBOL,
                                    expert_recommendation=_Rec(), existing_order=None)
    assert cond2.evaluate() is True


# ----------------------------------------------------- backend parity (only
# place in this file that names a storage backend)
def _days_ago(dt) -> int:
    """Age in days, tolerating SQLite's naive round-trip.

    Worth knowing: the in-memory store hands back the tz-AWARE datetime it was given, while
    SQLite returns it NAIVE. DaysSinceLastCloseCondition already normalises this
    (``close_date.replace(tzinfo=utc)`` when naive); the test must do the same or it would
    fail on a backend difference that the production code handles.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (NOW - dt).days


@contextmanager
def _sqlite_backend(tmp_path):
    """A private, schema'd SQLite for the SQL arm.

    Deliberately self-contained: the suite has no globally-migrated database, and other tests
    leave their own thread-local DB overrides behind, so relying on ambient state made these
    tests pass alone and fail in a full run ("no such table: transaction").
    """
    from ba2_common.core import db as common_db
    common_db.configure_db_threadlocal(str(tmp_path / "parity.sqlite"))
    common_db.init_db()
    try:
        yield
    finally:
        common_db.clear_threadlocal_db()


@contextmanager
def _inmem_backend():
    """The backtest in-memory store."""
    from ba2_common.core import trade_store as ts
    with ts.inmem_trades():
        yield


def test_both_backends_return_the_same_closed_transactions(tmp_path):
    """The two implementations must agree — otherwise the fake-repository condition tests
    above only prove one execution mode."""
    from ba2_common.core.db import add_instance

    expert_id = 8101
    ages = (40, 2, 25)

    with _sqlite_backend(tmp_path):
        for days in ages:
            add_instance(_closed_txn(days_ago=days, expert_id=expert_id))
        sql_days = [_days_ago(t.close_date)
                    for t in SqlTradeRepository().closed_transactions(expert_id=expert_id,
                                                                      symbol=SYMBOL)]

    with _inmem_backend():
        for days in ages:
            add_instance(_closed_txn(days_ago=days, expert_id=expert_id))
        mem_days = [_days_ago(t.close_date)
                    for t in InMemoryTradeRepository().closed_transactions(
                        expert_id=expert_id, symbol=SYMBOL)]

    assert sql_days == [2, 25, 40], "newest close must come first"
    assert mem_days == sql_days, "both backends must agree, ordering included"


def test_both_backends_resolve_open_option_orders_identically(tmp_path):
    """The transaction -> order link must resolve on both backends (the old SQL join had no
    in-memory equivalent)."""
    from ba2_common.core.db import add_instance

    expert_id = 8201

    with _sqlite_backend(tmp_path):
        txn_id = add_instance(_open_txn(expert_id=expert_id))
        add_instance(_option_order(txn_id=txn_id))
        sql_hits = SqlTradeRepository().open_option_orders(expert_id=expert_id,
                                                           underlying=SYMBOL)

    with _inmem_backend():
        txn_id = add_instance(_open_txn(expert_id=expert_id))
        add_instance(_option_order(txn_id=txn_id))
        mem_hits = InMemoryTradeRepository().open_option_orders(expert_id=expert_id,
                                                                underlying=SYMBOL)

    assert len(sql_hits) == 1
    assert len(mem_hits) == len(sql_hits), "both backends must resolve the txn->order link"


# ---------------------------------------------------------------------------
# "Never closed" and "could not determine" are DIFFERENT answers.
#
# ``last_closed_transaction`` skipped any close whose P&L it could not compute and
# then returned None -- the same None it returns when the expert has genuinely never
# closed the symbol. ``DaysSinceLastCloseCondition`` turned that None into the 1e9
# "infinitely long ago" sentinel, so a ">N day" re-entry cooldown PASSED. The sentinel
# is defensible for "never closed" (there is nothing to wait for); it is a fabricated
# measurement for "we could not tell whether the last close was a profit".
#
# The repository now reports WHY it found nothing, and the condition goes unevaluable
# (``calculated_value = None``) rather than inventing a number.
#
# Related: ``calculate_transaction_pnl`` used truthiness on ``close_price``, so EVERY
# option that expired worthless (close_price == 0.0) was "unclassifiable". With that
# fixed, a worthless expiry is an ordinary measured LOSS again -- pinned below.
# ---------------------------------------------------------------------------

def _reasons():
    """The repository's "why did you find nothing" vocabulary.

    Imported lazily so that a missing constant fails the ONE test that asserts on it
    rather than the whole module's collection.
    """
    from ba2_common.core import trade_repository as tr
    return tr.LAST_CLOSE_FOUND, tr.LAST_CLOSE_NONE, tr.LAST_CLOSE_UNCLASSIFIABLE


def _unclassifiable_close(days_ago, **kw):
    """A CLOSED transaction whose P&L cannot be computed: no close price was recorded."""
    txn = _closed_txn(days_ago, **kw)
    txn.close_price = None
    return txn


def _worthless_expiry(days_ago, side=OrderDirection.BUY, **kw):
    """A long option that expired worthless: closed at a MEASURED 0.00."""
    txn = _closed_txn(days_ago, **kw)
    txn.side = side
    txn.open_price = 2.50
    txn.close_price = 0.0
    txn.multiplier = 100
    return txn


def _losing_close(days_ago, **kw):
    txn = _closed_txn(days_ago, **kw)
    txn.open_price, txn.close_price = 110.0, 100.0
    return txn


def _days_since_profitable_close(value=15.0):
    from ba2_common.core.TradeConditions import DaysSinceLastProfitableCloseCondition
    return DaysSinceLastProfitableCloseCondition(
        account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
        operator_str=">", value=value, existing_order=None,
    )


def _days_since_losing_close(value=15.0):
    from ba2_common.core.TradeConditions import DaysSinceLastLosingCloseCondition
    return DaysSinceLastLosingCloseCondition(
        account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
        operator_str=">", value=value, existing_order=None,
    )


def _capture_condition_errors(monkeypatch):
    """Collect ``logger.error`` from TradeConditions.

    NOT caplog: ``ba2_common/logger.py`` sets ``propagate = False``, so caplog's root
    handler never sees these records.
    """
    import sys
    module = sys.modules["ba2_common.core.TradeConditions"]
    messages = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


class TestLastClosedTransactionReportsWhyItFoundNothing:
    def test_no_close_at_all_is_reported_as_such(self, repo):
        _found, _none, _unclass = _reasons()
        assert repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1) == (None, _none)

    def test_a_qualifying_close_is_reported_as_found(self, repo):
        _found, _none, _unclass = _reasons()
        repo.rows.append(_closed_txn(days_ago=3))
        txn, reason = repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1)
        assert reason == _found
        assert txn is not None

    def test_a_close_whose_pnl_cannot_be_computed_is_unclassifiable(self, repo):
        _found, _none, _unclass = _reasons()
        repo.rows.append(_unclassifiable_close(days_ago=3))
        assert repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1
        ) == (None, _unclass)

    def test_an_unclassifiable_close_is_irrelevant_when_no_sign_is_requested(self, repo):
        """profit_sign=0 never consults the P&L, so nothing is unclassifiable."""
        _found, _none, _unclass = _reasons()
        repo.rows.append(_unclassifiable_close(days_ago=3))
        txn, reason = repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=0)
        assert reason == _found
        assert txn is not None

    def test_a_newer_unclassifiable_close_hides_an_older_qualifying_one(self, repo):
        """"Days since the LAST profitable close" is 3 or 20 -- unknowable. Returning
        20 would be a fabricated measurement."""
        _found, _none, _unclass = _reasons()
        repo.rows += [_unclassifiable_close(days_ago=3), _closed_txn(days_ago=20)]
        assert repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1
        ) == (None, _unclass)

    def test_an_older_unclassifiable_close_does_not_hide_a_newer_qualifying_one(self, repo):
        """THE INVERSE: the newest close already answers the question; anything older
        cannot change it."""
        _found, _none, _unclass = _reasons()
        repo.rows += [_closed_txn(days_ago=3), _unclassifiable_close(days_ago=20)]
        txn, reason = repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1)
        assert reason == _found
        assert txn.close_date == NOW - timedelta(days=3)

    def test_a_classifiable_non_qualifying_close_is_not_unclassifiable(self, repo):
        """THE INVERSE: a close we CAN price and that simply is not a profit leaves
        'never had a profitable close' intact -- a knowable answer."""
        _found, _none, _unclass = _reasons()
        repo.rows.append(_losing_close(days_ago=3))
        assert repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1) == (None, _none)

    def test_a_close_with_no_close_date_is_unclassifiable_not_absent(self, repo):
        """A CLOSED row with no close_date cannot be DATED, so 'days since' is
        unknowable. It used to be filtered out silently and read as 'never closed'."""
        dateless = _closed_txn(days_ago=3)
        dateless.close_date = None
        _found, _none, _unclass = _reasons()
        repo.rows.append(dateless)
        assert repo.last_closed_transaction_with_reason(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=0
        ) == (None, _unclass)

    def test_the_original_single_value_api_still_works(self, repo):
        repo.rows.append(_closed_txn(days_ago=3))
        assert repo.last_closed_transaction(
            expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1) is not None
        assert repo.last_closed_transaction(
            expert_id=EXPERT_ID, symbol=SYMBOL + "X", profit_sign=1) is None


class TestProfitCooldownGoesUnevaluableRatherThanInventing1e9:
    def test_an_unclassifiable_close_makes_the_gate_unevaluable(self, repo, monkeypatch):
        """THE DEFECT: a close exists 3 days ago but its profit sign is unknown. The
        1e9 sentinel made a '>15 day' profitable-close cooldown PASS."""
        repo.rows.append(_unclassifiable_close(days_ago=3))
        errors = _capture_condition_errors(monkeypatch)
        cond = _days_since_profitable_close()

        allowed = cond.evaluate()

        assert cond.calculated_value is None, (
            "an undeterminable cooldown must be unevaluable, not 1e9")
        assert allowed is False
        assert errors, "going unevaluable must be reported"
        assert cond.get_actual_value_display() is None

    def test_never_closed_still_uses_the_sentinel(self, repo):
        """THE INVERSE #1: 'this expert has never closed the symbol' is KNOWABLE, and
        the sentinel is the right answer -- the cooldown must still allow entry."""
        cond = _days_since_profitable_close()
        assert cond.evaluate() is True
        assert cond.calculated_value == 1e9
        assert cond.get_actual_value_display() == "no prior close"

    def test_a_measured_losing_close_still_means_no_profitable_close(self, repo):
        """THE INVERSE #2: a close we CAN classify, that simply was not a profit,
        leaves the gate on the knowable 'never' branch."""
        repo.rows.append(_losing_close(days_ago=3))
        cond = _days_since_profitable_close()
        assert cond.evaluate() is True
        assert cond.calculated_value == 1e9

    def test_a_recent_profitable_close_still_blocks(self, repo):
        """THE INVERSE #3: the ordinary path is untouched."""
        repo.rows.append(_closed_txn(days_ago=3))
        cond = _days_since_profitable_close()
        assert cond.evaluate() is False
        assert cond.calculated_value == 3.0

    def test_the_any_close_gate_is_unaffected_by_an_unpriceable_close(self, repo):
        """THE INVERSE #4: with profit_sign=0 the P&L is never consulted, so a close
        with no close_price is still perfectly datable. This must NOT become
        unevaluable -- that would be the fix refusing a knowable answer."""
        repo.rows.append(_unclassifiable_close(days_ago=3))
        cond = _days_since_close()
        assert cond.evaluate() is False
        assert cond.calculated_value == 3.0

    def test_a_worthless_expiry_is_a_measured_loss_not_an_unknown(self, repo):
        """The item-4 link: an option that expired worthless closes at a MEASURED
        0.00. While ``calculate_transaction_pnl`` used truthiness on close_price it
        was 'unclassifiable' and poisoned BOTH sign-narrowed gates. It is a LOSS:
        the losing-close cooldown sees it, the profitable one does not."""
        repo.rows.append(_worthless_expiry(days_ago=3))

        losing = _days_since_losing_close()
        assert losing.evaluate() is False
        assert losing.calculated_value == 3.0

        profitable = _days_since_profitable_close()
        assert profitable.evaluate() is True
        assert profitable.calculated_value == 1e9

    def test_a_worthless_expiry_of_a_SHORT_option_is_a_measured_profit(self, repo):
        """And the other side of it: the seller kept the whole premium."""
        repo.rows.append(_worthless_expiry(days_ago=3, side=OrderDirection.SELL))

        profitable = _days_since_profitable_close()
        assert profitable.evaluate() is False
        assert profitable.calculated_value == 3.0


def _breakeven_close(days_ago, **kw):
    """A SCRATCH: closed at exactly the open price. P&L is a MEASURED 0.0."""
    txn = _closed_txn(days_ago, **kw)
    txn.open_price = txn.close_price = 100.0
    return txn


def test_a_breakeven_close_is_knowable_not_unclassifiable(repo):
    """THE INVERSE that guards the ``pnl is None`` test against becoming truthiness.

    A scratch has a P&L of exactly 0.0 -- a MEASUREMENT. It qualifies as neither a
    profit nor a loss, which leaves 'never had a profitable close': knowable, so the
    1e9 sentinel is right and the cooldown must still allow entry. Reading it as
    'unclassifiable' would be the fix refusing a legitimate zero and jamming the gate
    shut on every break-even trade."""
    _found, _none, _unclass = _reasons()
    repo.rows.append(_breakeven_close(days_ago=3))

    assert repo.last_closed_transaction_with_reason(
        expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=1) == (None, _none)
    assert repo.last_closed_transaction_with_reason(
        expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=-1) == (None, _none)

    cond = _days_since_profitable_close()
    assert cond.evaluate() is True
    assert cond.calculated_value == 1e9

    # And with no sign requested it is simply the last close, 3 days ago.
    any_close = _days_since_close()
    assert any_close.evaluate() is False
    assert any_close.calculated_value == 3.0


def test_a_dateless_close_hides_an_otherwise_qualifying_one(repo):
    """A CLOSED row with no close_date is invisible to the ordering, so it could be
    NEWER than the close we matched -- "days since" is then unknowable even though a
    perfectly good qualifying close exists. Without a datable close alongside it the
    match path is never reached, so this needs both."""
    _found, _none, _unclass = _reasons()
    dateless = _closed_txn(days_ago=1)
    dateless.close_date = None
    repo.rows += [dateless, _closed_txn(days_ago=3)]

    assert repo.last_closed_transaction_with_reason(
        expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=0) == (None, _unclass)

    cond = _days_since_close()
    assert cond.evaluate() is False
    assert cond.calculated_value is None


def test_every_close_dated_is_still_an_ordinary_answer(repo):
    """THE INVERSE: nothing about the dateless check may disturb dated closes."""
    _found, _none, _unclass = _reasons()
    repo.rows += [_closed_txn(days_ago=3), _closed_txn(days_ago=30)]
    txn, reason = repo.last_closed_transaction_with_reason(
        expert_id=EXPERT_ID, symbol=SYMBOL, profit_sign=0)
    assert reason == _found
    assert txn.close_date == NOW - timedelta(days=3)
