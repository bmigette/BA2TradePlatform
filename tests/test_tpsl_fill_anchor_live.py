"""LIVE: the fill-time re-base reads the STAMPED anchor (the price the stop was computed from).

Before 2026-10-07 the anchor was whatever ``_tpsl_reference_price`` resolved when the exit order was
built (the entry's limit price, else ``rec.price_at_date``), recorded once on the exit order. A stop
built from the market price on a LIMIT entry was therefore re-based against the limit price. Now the
level's setter stamps ``Transaction.meta_data["tpsl_anchor"]`` and the re-base reads that first; the
order's ``data["tpsl_reference_price"]`` is only the fallback for orders created before the stamp.
"""
import pytest

from ba2_common.core.tpsl_fill_rebase import ANCHOR_KEY, stamp_anchor
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from tests.factories import (
    create_account_definition, create_recommendation, create_trading_order, create_transaction,
)

A = pytest.approx


def _drive(*, meta, ref_in_data, fill=100.0, tp=140.0, sl=104.5, long=True, capture=None, monkeypatch=None):
    acct = create_account_definition(provider="MockAccount")
    rec = create_recommendation(instance_id=1, symbol="XYZ", price_at_date=100.0)
    side = OrderDirection.BUY if long else OrderDirection.SELL
    txn = create_transaction(symbol="XYZ", quantity=3.0, side=side, open_price=fill,
                             take_profit=tp, stop_loss=sl, meta_data=meta)
    parent = create_trading_order(
        account_id=acct.id, symbol="XYZ", quantity=3.0, side=side, order_type=(OrderType.BUY_LIMIT if long else OrderType.SELL_LIMIT),
        limit_price=100.0, status=OrderStatus.FILLED, open_price=fill,
        expert_recommendation_id=rec.id, transaction_id=txn.id)
    data = {"tp_percent_target": 0.0}
    if ref_in_data is not None:
        data["tpsl_reference_price"] = ref_in_data
    dep = create_trading_order(
        account_id=acct.id, symbol="XYZ", quantity=3.0,
        side=OrderDirection.SELL if long else OrderDirection.BUY, order_type=OrderType.OCO,
        status=OrderStatus.WAITING_TRIGGER, limit_price=tp, stop_price=sl, transaction_id=txn.id,
        depends_on_order=parent.id, depends_order_status_trigger=OrderStatus.FILLED, data=data)
    TradeManager()._check_all_waiting_trigger_orders()
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder, Transaction
    return get_instance(TradingOrder, dep.id), get_instance(Transaction, txn.id)


def test_the_stamped_anchor_beats_the_reference_recorded_on_the_exit_order():
    """Stop 104.5 = 5% under the MARKET price 110; the limit entry filled at 100; the exit order's
    own reference is the limit price (100, the legacy chain). Stamped: 100 * 104.5 / 110 = 95.0."""
    meta = stamp_anchor({}, stop=110.0)
    leg, txn = _drive(meta=meta, ref_in_data=100.0)
    assert leg.stop_price == A(95.0) and txn.stop_loss == A(95.0)
    assert leg.data["sl_rebased_to_fill"] is True


def test_without_a_stamp_the_legacy_reference_still_decides():
    """An order created before the stamp existed: its recorded reference is used, exactly as before
    (here that reference IS the flaw the stamp fixes: the stop stays above the fill)."""
    leg, txn = _drive(meta=None, ref_in_data=100.0)
    assert leg.stop_price == A(104.5) and txn.stop_loss == A(104.5)


def test_after_the_rebase_the_stamp_is_the_fill_so_a_second_pass_is_a_noop():
    meta = stamp_anchor({}, stop=110.0)
    _leg, txn = _drive(meta=meta, ref_in_data=100.0)
    assert txn.meta_data[ANCHOR_KEY]["stop"] == A(100.0)


def test_each_level_keeps_its_own_anchor_and_only_the_stop_anchor_moves_the_stop():
    meta = stamp_anchor({}, stop=110.0, tp=100.0)
    leg, txn = _drive(meta=meta, ref_in_data=None)
    assert leg.stop_price == A(95.0)
    assert txn.meta_data[ANCHOR_KEY]["tp"] == A(100.0)          # the target's anchor is untouched
    assert leg.limit_price == A(140.0)


def test_a_short_with_a_stamped_anchor():
    meta = stamp_anchor({}, stop=90.0)      # short: stop 5% ABOVE the market 90 = 94.5; limit entry fills 100
    leg, _ = _drive(meta=meta, ref_in_data=100.0, long=False, tp=60.0, sl=94.5)
    assert leg.stop_price == A(105.0)       # 100 * 94.5 / 90


def test_the_rebased_stop_is_what_reaches_the_broker_oco_stop_limit_leg(monkeypatch):
    """The OCO leg is sent as stop + limit (live Alpaca derives the stop-LIMIT leg's limit from the
    stop at submit, with the OCO_STOP_LIMIT_CUSHION). The re-base runs BEFORE that submit, so the
    broker is handed the re-based stop, not the pre-fill one."""
    sent = []

    class _Broker:
        supports_trading = True

        def __init__(self, account_id):
            pass

        def get_available_position_quantity(self, symbol):
            return 1000.0

        def submit_order(self, order, *a, **k):
            sent.append((order.order_type, order.stop_price, order.limit_price))
            return order

    import ba2_trade_platform.modules.accounts as accounts_mod
    monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _Broker)
    meta = stamp_anchor({}, stop=110.0)
    _drive(meta=meta, ref_in_data=100.0)
    [(otype, stop, limit)] = [s for s in sent if s[0] == OrderType.OCO]
    assert stop == A(95.0) and limit == A(140.0)


def test_manual_override_flags_are_not_consulted_KNOWN_OPEN_QUESTION():
    """PINS TODAY'S BEHAVIOUR, and it is a KNOWN OPEN QUESTION for the owner: a position whose
    TP/SL the operator pinned by hand (``tp_manual_override`` / ``sl_manual_override``) is still
    re-based to the fill by this pass, because only ``_adjust_tpsl_internal`` honours the flags.
    If the owner decides the lock must hold at the fill, this test changes with that decision."""
    meta = stamp_anchor({}, stop=110.0)
    acct_leg, txn = _drive(meta=meta, ref_in_data=100.0)
    assert acct_leg.stop_price == A(95.0)
    # same inputs, flags set on the transaction:
    from tests.test_tpsl_fill_rebase_characterization import _drive as char_drive
    _l, stop, _t, tsl, _d = char_drive(ref=100.0, fill=103.0, tp=110.0, sl=94.0,
                                       txn_flags={"tp_manual_override": True, "sl_manual_override": True})
    assert stop == A(96.82) and tsl == A(96.82)
