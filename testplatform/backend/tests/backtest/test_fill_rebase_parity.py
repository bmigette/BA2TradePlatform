"""Backtest and live adjust an entry's protective levels to the fill IDENTICALLY (one function, two callers).

For the same entry (the SAME seeded ruleset bracket, the same recommendation at 100) and the same
fill, three numbers must agree to the cent:

  * the PURE shared function ``rebase_levels_at_fill`` (the oracle, fed the inputs by hand),
  * LIVE: the real ``TradeManager`` entry pass builds the exit leg; the entry is marked FILLED at the
    fill price; ``_check_all_waiting_trigger_orders`` re-bases the leg (and the transaction),
  * BACKTEST: the real ``DailyBacktestEngine`` over a ``BacktestAccount`` whose next bar opens at the
    fill price; the transaction's levels after the fill are what the bracket test enforces.

Cases: long and short, fill above and below the decision price, and a take-profit that the fill
leaves under the minimum-distance floor.

    python -m pytest tests/backtest/test_fill_rebase_parity.py -q
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core.tpsl_fill_rebase import rebase_levels_at_fill
from tests.backtest.test_short_selling_bt_live_parity import (
    PRICE, SYMBOL, _live_world, _write_recommendation, get_db_rows)
from tests.backtest.test_short_selling_engine import BUY, DAY1, SELL, _adjust, _run

BRACKET = [_adjust("adjust_stop_loss", -8.0), _adjust("adjust_take_profit", 10.0)]
TIGHT_TP = [_adjust("adjust_stop_loss", -8.0), _adjust("adjust_take_profit", 4.0)]


def _bars(fill):
    """Decision bar closes at PRICE (100); the next bar opens at ``fill``; two quiet bars after."""
    return [(date(2024, 1, 2), 100, 101, 99, 100),
            (date(2024, 1, 3), fill, fill + 1, fill - 1, fill),
            (date(2024, 1, 4), fill, fill + 1, fill - 1, fill),
            (date(2024, 1, 5), fill, fill + 1, fill - 1, fill)]


def _bt_levels(monkeypatch, signal, fill, actions, run_id):
    from tests.backtest.test_max_loss_stop_engine import _store_mode
    _store_mode(monkeypatch, "1")
    _, [txn] = _run(_bars(fill), {DAY1: signal}, run_id=run_id, inmem="1",
                    enable_short=(signal == SELL), entry_actions=actions)
    return txn["stop_loss"], txn["take_profit"]


def _live_levels(monkeypatch, signal, fill, actions, run_id):
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from ba2_common.core.db import get_instance, update_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.types import AnalysisUseCase, OrderStatus

    enter = lambda: seed_ruleset_from_tree(  # noqa: E731
        None, name=f"fill-rebase-live-{run_id}", enable_short=(signal == SELL), entry_actions=actions)
    with _live_world(monkeypatch, run_id, enter_id=enter) as w:
        _write_recommendation(w.id, signal, AnalysisUseCase.ENTER_MARKET)
        w.tm.process_expert_recommendations_after_analysis(w.id, lookback_days=1)
        orders = get_db_rows(TradingOrder)
        [entry] = [o for o in orders if o.depends_on_order is None]
        [leg] = [o for o in orders if o.depends_on_order == entry.id]
        pre = (leg.stop_price, leg.limit_price)
        entry = get_instance(TradingOrder, entry.id)
        entry.status = OrderStatus.FILLED
        entry.open_price = fill
        entry.filled_qty = entry.quantity
        update_instance(entry)
        w.tm._check_all_waiting_trigger_orders()
        leg = get_instance(TradingOrder, leg.id)
        txn = get_instance(Transaction, entry.transaction_id)
        # the exit leg and the transaction agree with each other
        assert (leg.stop_price, leg.limit_price) == pytest.approx((txn.stop_loss, txn.take_profit))
        return txn.stop_loss, txn.take_profit, pre


@pytest.mark.parametrize("signal,fill,actions", [
    (BUY, 103.0, BRACKET),
    (BUY, 97.0, BRACKET),
    (SELL, 97.0, BRACKET),
    (SELL, 103.0, BRACKET),
    (BUY, 103.0, TIGHT_TP),      # TP 104 is +0.97% over the 103 fill: floored to 105.06
    (SELL, 97.0, TIGHT_TP),      # short mirror: TP 96 is +1.03% under 97: floored to 95.06
], ids=["long-up", "long-down", "short-down", "short-up", "long-floor", "short-floor"])
def test_pure_function_live_and_backtest_agree(monkeypatch, signal, fill, actions):
    is_long = signal == BUY
    sl_pct, tp_pct = -8.0, actions[1]["action_value"]
    sl0 = PRICE * (1 + sl_pct / 100) if is_long else PRICE * (1 - sl_pct / 100)
    tp0 = PRICE * (1 + tp_pct / 100) if is_long else PRICE * (1 - tp_pct / 100)
    oracle = rebase_levels_at_fill(
        is_long=is_long, fill_price=fill, reference_price=PRICE, take_profit=tp0, stop_loss=sl0,
        min_take_profit_pct=2.0)

    tag = 1400 + int(fill) + (10 if is_long else 0) + len(actions[1]["id"])
    live_sl, live_tp, pre = _live_levels(monkeypatch, signal, fill, actions, tag)
    bt_sl, bt_tp = _bt_levels(monkeypatch, signal, fill, actions, tag + 500)

    # the pre-fill leg is what both start from
    assert pre[0] == pytest.approx(sl0) and pre[1] == pytest.approx(tp0)
    assert live_sl == pytest.approx(oracle.stop_loss, abs=1e-4)
    assert live_tp == pytest.approx(oracle.take_profit, abs=1e-4)
    assert bt_sl == pytest.approx(oracle.stop_loss, abs=1e-4)
    assert bt_tp == pytest.approx(oracle.take_profit, abs=1e-4)
    assert (bt_sl, bt_tp) == pytest.approx((live_sl, live_tp), abs=1e-4)
    if actions is TIGHT_TP:
        assert oracle.tp_floored
