"""The max-loss stop: the stop each equity position was SIZED on, recorded once at entry.

Plan task B3 (docs/plans/2026-09-24-pullback-and-market-exits.md). B4 lets rules loosen a
stop-loss but never past this value, so it has to be the stop the risk budget was spent against
(the RM safeguard when there is one), it has to survive every later write to the transaction,
and it must never be invented for a transaction that does not carry it.
"""
import math
from datetime import datetime, timezone

import pytest

from ba2_common.core.position_sizing import (
    MAX_LOSS_STOP_KEY,
    max_loss_stop_of,
    reconcile_protective_stop,
    sized_on_stop,
    with_max_loss_stop,
)


class _Txn:
    def __init__(self, meta_data):
        self.meta_data = meta_data


# --------------------------------------------------------------------------- #
# max_loss_stop_of
# --------------------------------------------------------------------------- #

def test_absent_key_reads_as_none_not_a_price():
    """An older transaction, opened before this was recorded, has no max-loss stop."""
    assert max_loss_stop_of(_Txn(None)) is None
    assert max_loss_stop_of(_Txn({})) is None
    assert max_loss_stop_of(_Txn({"TradeConditionsData": {"current_target_price": 120.0}})) is None
    assert max_loss_stop_of(object()) is None, "no meta_data attribute at all"


@pytest.mark.parametrize("bad", [None, 0, 0.0, -5.0, math.nan, math.inf, -math.inf, "abc", True, [], {}])
def test_an_unusable_stored_value_reads_as_none(bad):
    assert max_loss_stop_of(_Txn({MAX_LOSS_STOP_KEY: bad})) is None


def test_a_recorded_stop_reads_back_as_a_float():
    assert max_loss_stop_of(_Txn({MAX_LOSS_STOP_KEY: 92.0})) == 92.0
    assert max_loss_stop_of(_Txn({MAX_LOSS_STOP_KEY: 92})) == 92.0
    assert isinstance(max_loss_stop_of(_Txn({MAX_LOSS_STOP_KEY: 92})), float)


def test_meta_data_that_is_not_a_dict_reads_as_none():
    assert max_loss_stop_of(_Txn("not-a-dict")) is None
    assert max_loss_stop_of(_Txn([MAX_LOSS_STOP_KEY])) is None


# --------------------------------------------------------------------------- #
# sized_on_stop -- WHICH stop is the max-loss stop
# --------------------------------------------------------------------------- #

def test_the_safeguard_is_recorded_even_when_a_tighter_ruleset_stop_is_submitted():
    """The size was keyed off the safeguard; the tighter ruleset stop only protects. The value a
    rule may loosen back to is therefore the safeguard, NOT the submitted stop."""
    ruleset, safeguard = 97.0, 92.0
    assert reconcile_protective_stop(ruleset, safeguard, is_long=True) == 97.0   # submitted
    assert sized_on_stop(ruleset_sl=ruleset, safeguard_sl=safeguard) == 92.0     # sized on


def test_the_safeguard_is_recorded_when_it_is_the_tighter_one():
    ruleset, safeguard = 85.0, 92.0
    assert reconcile_protective_stop(ruleset, safeguard, is_long=True) == 92.0
    assert sized_on_stop(ruleset_sl=ruleset, safeguard_sl=safeguard) == 92.0


def test_short_entry_records_the_safeguard_too():
    assert sized_on_stop(ruleset_sl=103.0, safeguard_sl=108.0) == 108.0


def test_without_a_safeguard_the_ruleset_stop_is_the_only_stop_and_is_recorded():
    assert sized_on_stop(ruleset_sl=95.0, safeguard_sl=None) == 95.0
    assert sized_on_stop(ruleset_sl=95.0, safeguard_sl=0.0) == 95.0
    assert sized_on_stop(ruleset_sl=95.0, safeguard_sl=math.nan) == 95.0


def test_no_stop_at_all_records_nothing():
    assert sized_on_stop(ruleset_sl=None, safeguard_sl=None) is None
    assert sized_on_stop(ruleset_sl=0.0, safeguard_sl=-1.0) is None


# --------------------------------------------------------------------------- #
# with_max_loss_stop -- new dict, write once
# --------------------------------------------------------------------------- #

def test_the_write_is_a_new_dict_and_carries_every_other_key():
    meta = {"TradeConditionsData": {"current_target_price": 120.0}}
    out = with_max_loss_stop(meta, 92.0)
    assert out is not meta, "a JSON column only persists a NEW object assigned to it"
    assert meta == {"TradeConditionsData": {"current_target_price": 120.0}}, "input mutated"
    assert out == {"TradeConditionsData": {"current_target_price": 120.0}, MAX_LOSS_STOP_KEY: 92.0}


def test_the_write_starts_from_nothing_when_there_is_no_meta():
    assert with_max_loss_stop(None, 92.0) == {MAX_LOSS_STOP_KEY: 92.0}


def test_the_value_is_written_once_and_never_replaced():
    assert with_max_loss_stop({MAX_LOSS_STOP_KEY: 92.0}, 97.0) is None
    # Even a stored value that is unusable is not overwritten: the first write is the record.
    assert with_max_loss_stop({MAX_LOSS_STOP_KEY: None}, 97.0) is None


def test_no_usable_stop_means_nothing_to_write():
    assert with_max_loss_stop({}, None) is None
    assert with_max_loss_stop({}, 0.0) is None
    assert with_max_loss_stop({}, math.inf) is None


# --------------------------------------------------------------------------- #
# record_max_loss_stop -- the write at the entry tail, against a real DB row
# --------------------------------------------------------------------------- #

def _entry(*, stop_loss=None, meta_data=None, asset_class=None, order_type=None, depends_on=None):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus

    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=10.0, side=OrderDirection.BUY, open_price=100.0,
        stop_loss=stop_loss, status=TransactionStatus.WAITING, meta_data=meta_data,
        asset_class=asset_class or AssetClass.EQUITY, created_at=datetime.now(timezone.utc)))
    # The order is NOT persisted: the helper reads only its transaction link, type and parent,
    # and the transaction row is what it writes.
    order = TradingOrder(
        symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=order_type or OrderType.MARKET, status=OrderStatus.PENDING,
        transaction_id=txn_id, depends_on_order=depends_on,
        created_at=datetime.now(timezone.utc))
    return order, txn_id


def _meta(txn_id):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    return get_instance(Transaction, txn_id).meta_data


def test_record_persists_the_safeguard_and_keeps_the_other_meta_keys():
    from ba2_common.core.trade_cycle import record_max_loss_stop

    order, txn_id = _entry(stop_loss=97.0, meta_data={"TradeConditionsData": {"x": 1}})
    assert record_max_loss_stop(order, 92.0) == 92.0
    assert _meta(txn_id) == {"TradeConditionsData": {"x": 1}, MAX_LOSS_STOP_KEY: 92.0}


def test_record_falls_back_to_the_ruleset_stop_on_the_transaction():
    from ba2_common.core.trade_cycle import record_max_loss_stop

    order, txn_id = _entry(stop_loss=95.0)
    assert record_max_loss_stop(order, None) == 95.0
    assert _meta(txn_id) == {MAX_LOSS_STOP_KEY: 95.0}


def test_record_writes_nothing_when_the_entry_has_no_stop():
    from ba2_common.core.trade_cycle import record_max_loss_stop

    order, txn_id = _entry(stop_loss=None)
    assert record_max_loss_stop(order, None) is None
    assert _meta(txn_id) is None


def test_record_is_write_once_across_a_resubmit_or_retry():
    from ba2_common.core.trade_cycle import record_max_loss_stop

    order, txn_id = _entry()
    assert record_max_loss_stop(order, 92.0) == 92.0
    assert record_max_loss_stop(order, 80.0) is None
    assert _meta(txn_id)[MAX_LOSS_STOP_KEY] == 92.0


def test_record_skips_option_transactions():
    from ba2_common.core.trade_cycle import record_max_loss_stop
    from ba2_common.core.types import AssetClass

    order, txn_id = _entry(asset_class=AssetClass.OPTION)
    assert record_max_loss_stop(order, 92.0) is None
    assert _meta(txn_id) is None


def test_record_skips_non_market_orders_whose_stop_price_is_a_trigger():
    from ba2_common.core.trade_cycle import record_max_loss_stop
    from ba2_common.core.types import OrderType

    order, txn_id = _entry(order_type=OrderType.BUY_STOP)
    assert record_max_loss_stop(order, 92.0) is None
    assert _meta(txn_id) is None


def test_record_skips_protective_legs():
    from ba2_common.core.trade_cycle import record_max_loss_stop

    order, txn_id = _entry(depends_on=12345)
    assert record_max_loss_stop(order, 92.0) is None
    assert _meta(txn_id) is None


def test_record_without_a_transaction_is_a_no_op():
    from ba2_common.core.trade_cycle import record_max_loss_stop

    class _Order:
        transaction_id = None

    assert record_max_loss_stop(_Order(), 92.0) is None
    assert record_max_loss_stop(None, 92.0) is None


def test_record_never_raises_into_the_entry_path(monkeypatch):
    """The entry is already submitted when this runs. A failing metadata write is logged at
    ERROR and swallowed: live, propagating it would reach the funded loop's compensation."""
    from ba2_common.core import db as _db
    from ba2_common.core import trade_cycle
    from ba2_common.core.trade_cycle import record_max_loss_stop

    order, txn_id = _entry()
    errors = []

    def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(_db, "update_instance", _boom)
    monkeypatch.setattr(trade_cycle.logger, "error", lambda msg, *a, **k: errors.append(msg))
    assert record_max_loss_stop(order, 92.0) is None
    assert len(errors) == 1 and "max-loss stop" in errors[0], "the failure must be loud"


def test_record_is_visible_on_the_same_object_in_the_backtest_trade_store():
    """Backtest (sql-less store): the adjust path gets the SAME Transaction object by identity,
    so the recorded value must be readable from it without any re-read."""
    from ba2_common.core import trade_store
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.trade_cycle import record_max_loss_stop

    with trade_store.inmem_trades():
        order, txn_id = _entry(meta_data={"k": "v"})
        held = get_instance(Transaction, txn_id)
        assert record_max_loss_stop(order, 92.0) == 92.0
        assert max_loss_stop_of(held) == 92.0
        assert held.meta_data == {"k": "v", MAX_LOSS_STOP_KEY: 92.0}
