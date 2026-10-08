"""Characterization of the LIVE post-fill TP/SL adjustment (TradeManager._check_all_waiting_trigger_orders).

These tests PIN what live does today, case by case, through the real TradeManager pass over
real DB rows (MockAccount). They were written BEFORE the adjustment was extracted into
``ba2_common.core.tpsl_fill_rebase`` and must pass UNCHANGED before and after that refactor: the
refactor may not move a level by a cent.

The rules pinned (see TradeManager, "Re-base the STOP-LOSS" ... "Legacy: percent-based recalc"):

 1. SL: ``fill * (stop / reference)`` rounded to 4 dp, when the dependent order carries
    ``data["tpsl_reference_price"]`` AND a stop AND the parent has a fill. Same formula for long
    and short (sign-agnostic).
 2. TP: untouched, unless ``data`` has ``tp_percent_target`` and the TP is closer to the REAL fill
    than the minimum take-profit % (the recommendation's ``min_take_profit_percent``, default 2.0):
    then it is raised (long) / lowered (short) to ``fill * (1 +/- min/100)`` (not rounded).
 3. Legacy: ``data["TP_SL"]["tp_percent"]`` -> TP = round(fill * (1 + pct/100), 4); ELSE
    ``["sl_percent"]`` -> SL = round(fill * (1 + pct/100), 4). Runs after 1 and 2 and overrides them.
 4. Manual-override flags on the transaction are NOT consulted.
"""
import pytest

from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from tests.factories import (
    create_account_definition, create_recommendation, create_trading_order, create_transaction,
)

_UNSET = object()


def _drive(*, long=True, ref=_UNSET, fill=103.0, tp=110.0, sl=94.0, kind="oco",
           data_extra=None, tp_target_key=True, min_pct=None, txn_flags=None, rec=True):
    """Run one waiting-trigger pass; returns (limit, stop, txn_tp, txn_sl, order.data)."""
    acct = create_account_definition(provider="MockAccount")
    rec_row = None
    if rec:
        kw = {} if min_pct is None else {"min_take_profit_percent": min_pct}
        rec_row = create_recommendation(instance_id=1, symbol="XYZ", price_at_date=100.0, **kw)
    entry_side = OrderDirection.BUY if long else OrderDirection.SELL
    exit_side = OrderDirection.SELL if long else OrderDirection.BUY
    txn = create_transaction(symbol="XYZ", quantity=3.0, side=entry_side, open_price=fill,
                             take_profit=tp, stop_loss=sl, **(txn_flags or {}))
    parent = create_trading_order(
        account_id=acct.id, symbol="XYZ", quantity=3.0, side=entry_side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=fill,
        expert_recommendation_id=rec_row.id if rec_row else None, transaction_id=txn.id)
    data = dict(data_extra or {})
    if tp_target_key:
        data["tp_percent_target"] = 0.0
    if ref is not _UNSET:
        data["tpsl_reference_price"] = ref
    order_type, limit, stop = {
        "oco": (OrderType.OCO, tp, sl),
        "tp": (OrderType.SELL_LIMIT if long else OrderType.BUY_LIMIT, tp, None),
        "sl": (OrderType.SELL_STOP if long else OrderType.BUY_STOP, None, sl),
    }[kind]
    dep = create_trading_order(
        account_id=acct.id, symbol="XYZ", quantity=3.0, side=exit_side, order_type=order_type,
        status=OrderStatus.WAITING_TRIGGER, limit_price=limit, stop_price=stop,
        transaction_id=txn.id, depends_on_order=parent.id,
        depends_order_status_trigger=OrderStatus.FILLED, data=data)

    TradeManager()._check_all_waiting_trigger_orders()

    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder, Transaction
    o = get_instance(TradingOrder, dep.id)
    t = get_instance(Transaction, txn.id)
    return o.limit_price, o.stop_price, t.take_profit, t.stop_loss, (o.data or {})


A = pytest.approx


class TestStopRebase:
    def test_long_fill_above_reference(self):
        limit, stop, ttp, tsl, data = _drive(ref=100.0, fill=103.0, tp=110.0, sl=94.0)
        assert stop == A(96.82) and tsl == A(96.82)
        assert limit == A(110.0) and ttp == A(110.0)
        assert data["sl_rebased_to_fill"] is True and data["parent_filled_price"] == 103.0
        assert "tp_floor_rechecked_at_fill" not in data

    def test_long_fill_below_reference(self):
        limit, stop, ttp, tsl, _ = _drive(ref=100.0, fill=97.0, tp=110.0, sl=94.0)
        assert stop == A(91.18) and tsl == A(91.18)
        assert limit == A(110.0) and ttp == A(110.0)

    def test_short_fill_above_reference(self):
        limit, stop, ttp, tsl, _ = _drive(long=False, ref=100.0, fill=103.0, tp=90.0, sl=106.0)
        assert stop == A(109.18) and tsl == A(109.18)
        assert limit == A(90.0) and ttp == A(90.0)

    def test_short_fill_below_reference(self):
        limit, stop, ttp, tsl, _ = _drive(long=False, ref=100.0, fill=97.0, tp=90.0, sl=106.0)
        assert stop == A(102.82) and tsl == A(102.82)
        assert limit == A(90.0) and ttp == A(90.0)

    def test_stop_only_order(self):
        limit, stop, ttp, tsl, _ = _drive(kind="sl", tp_target_key=False, ref=100.0, fill=103.0)
        assert limit is None and stop == A(96.82) and tsl == A(96.82)

    def test_rounds_to_four_decimals(self):
        _, stop, _, tsl, _ = _drive(ref=10.79, fill=10.66, tp=12.0, sl=10.1426)
        assert stop == 10.0204 and tsl == 10.0204

    def test_reference_equal_to_fill_is_a_noop(self):
        limit, stop, ttp, tsl, data = _drive(ref=103.0, fill=103.0, tp=110.0, sl=94.0)
        assert stop == A(94.0) and tsl == A(94.0) and limit == A(110.0)
        assert "sl_rebased_to_fill" not in data


class TestTakeProfitFloor:
    def test_long_tp_raised_to_floor_from_the_real_fill(self):
        # TP +4% of the reference (104) is only +0.97% above the 103 fill: raised to 103 * 1.02.
        limit, stop, ttp, _, data = _drive(ref=100.0, fill=103.0, tp=104.0, sl=94.0)
        assert limit == A(105.06) and ttp == A(105.06)
        assert data["tp_floor_rechecked_at_fill"] is True

    def test_short_tp_lowered_to_floor_from_the_real_fill(self):
        limit, stop, ttp, _, data = _drive(long=False, ref=100.0, fill=97.0, tp=96.0, sl=106.0)
        assert limit == A(95.06) and ttp == A(95.06)
        assert data["tp_floor_rechecked_at_fill"] is True

    def test_tp_never_lowered_for_a_long_when_above_the_floor(self):
        limit, _, ttp, _, data = _drive(ref=100.0, fill=103.0, tp=140.0, sl=94.0)
        assert limit == A(140.0) and ttp == A(140.0)
        assert "tp_floor_rechecked_at_fill" not in data

    def test_floor_needs_the_tp_percent_target_key(self):
        limit, _, ttp, _, _ = _drive(ref=100.0, fill=103.0, tp=104.0, sl=94.0, tp_target_key=False)
        assert limit == A(104.0) and ttp == A(104.0)

    def test_tp_only_order_floor(self):
        limit, stop, ttp, _, _ = _drive(kind="tp", ref=100.0, fill=103.0, tp=104.0)
        assert limit == A(105.06) and stop is None and ttp == A(105.06)

    def test_custom_min_take_profit_percent(self):
        limit, _, ttp, _, _ = _drive(ref=100.0, fill=100.0, tp=101.0, sl=94.0, min_pct=6.0)
        assert limit == A(106.0) and ttp == A(106.0)

    def test_no_recommendation_defaults_to_two_percent(self):
        limit, _, _, _, _ = _drive(ref=100.0, fill=100.0, tp=101.0, sl=94.0, rec=False)
        assert limit == A(102.0)


class TestMissingOrBadInputs:
    def test_reference_key_absent_skips_the_stop_rebase_but_not_the_floor(self):
        limit, stop, _, tsl, data = _drive(fill=103.0, tp=104.0, sl=94.0)
        assert stop == A(94.0) and tsl == A(94.0)
        assert limit == A(105.06)
        assert "sl_rebased_to_fill" not in data

    @pytest.mark.parametrize("ref", [None, 0, 0.0])
    def test_reference_none_or_zero_is_silently_skipped(self, ref):
        # FINDING: live neither raises nor logs here -- the stop is just left where it was.
        limit, stop, _, tsl, _ = _drive(ref=ref, fill=103.0, tp=104.0, sl=94.0)
        assert stop == A(94.0) and tsl == A(94.0)
        assert limit == A(105.06)

    def test_negative_reference_leaves_the_stop_untouched(self):
        _, stop, _, tsl, data = _drive(ref=-5.0, fill=103.0, tp=110.0, sl=94.0)
        assert stop == A(94.0) and tsl == A(94.0)
        assert "sl_rebased_to_fill" not in data

    def test_parent_without_a_fill_price_changes_nothing(self):
        acct = create_account_definition(provider="MockAccount")
        txn = create_transaction(symbol="XYZ", quantity=3.0, side=OrderDirection.BUY,
                                 open_price=None, take_profit=104.0, stop_loss=94.0)
        parent = create_trading_order(
            account_id=acct.id, symbol="XYZ", quantity=3.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=None,
            transaction_id=txn.id)
        dep = create_trading_order(
            account_id=acct.id, symbol="XYZ", quantity=3.0, side=OrderDirection.SELL,
            order_type=OrderType.OCO, status=OrderStatus.WAITING_TRIGGER, limit_price=104.0,
            stop_price=94.0, transaction_id=txn.id, depends_on_order=parent.id,
            depends_order_status_trigger=OrderStatus.FILLED,
            data={"tp_percent_target": 0.0, "tpsl_reference_price": 100.0})
        TradeManager()._check_all_waiting_trigger_orders()
        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder
        o = get_instance(TradingOrder, dep.id)
        assert o.stop_price == A(94.0) and o.limit_price == A(104.0)

    def test_manual_override_flags_are_not_consulted(self):
        limit, stop, _, tsl, _ = _drive(ref=100.0, fill=103.0, tp=104.0, sl=94.0,
                                        txn_flags={"tp_manual_override": True,
                                                   "sl_manual_override": True})
        assert stop == A(96.82) and tsl == A(96.82)
        assert limit == A(105.06)


class TestLegacyPercentRecalc:
    def test_tp_percent_recomputes_the_target_from_the_fill(self):
        limit, stop, ttp, tsl, data = _drive(
            ref=100.0, fill=103.0, tp=110.0, sl=94.0, data_extra={"TP_SL": {"tp_percent": 5.0}})
        assert limit == A(108.15) and ttp == A(108.15)
        assert stop == A(96.82) and tsl == A(96.82)        # the rebase ran first
        assert data["recalculated_at_trigger"] is True

    def test_sl_percent_recomputes_the_stop_and_overrides_the_rebase(self):
        limit, stop, ttp, tsl, data = _drive(
            ref=100.0, fill=103.0, tp=110.0, sl=94.0, data_extra={"TP_SL": {"sl_percent": -5.0}})
        assert stop == A(97.85) and tsl == A(97.85)
        assert limit == A(110.0)
        assert data["TP_SL"]["recalculated_at_trigger"] is True

    def test_tp_percent_wins_over_sl_percent(self):
        limit, stop, _, tsl, _ = _drive(
            ref=100.0, fill=103.0, tp=110.0, sl=94.0,
            data_extra={"TP_SL": {"tp_percent": 5.0, "sl_percent": -5.0}})
        assert limit == A(108.15) and stop == A(96.82) and tsl == A(96.82)

    def test_tp_percent_overrides_the_floor(self):
        limit, _, ttp, _, _ = _drive(
            ref=100.0, fill=103.0, tp=104.0, sl=94.0, data_extra={"TP_SL": {"tp_percent": 1.0}})
        assert limit == A(104.03) and ttp == A(104.03)

    def test_short_percents(self):
        limit, stop, _, tsl, _ = _drive(
            long=False, ref=100.0, fill=103.0, tp=90.0, sl=106.0,
            data_extra={"TP_SL": {"tp_percent": -10.0}})
        assert limit == A(92.7) and stop == A(109.18)
