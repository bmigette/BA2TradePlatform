"""A resting OCO is protection, not a pending close: the exit rules still run, BT and live alike.

THE LIVE DEFECT (2026-09-26). Alpaca writes an OCO's stop leg as a HELD root row
(``parent_order_id`` -> the OCO, no ``depends_on_order``). ``has_pending_closing_order`` read it
as "a close is working", so ``TradeManager.process_open_positions_recommendations`` dropped the
position and its open-positions ruleset never ran: 13 of 23 prod expert positions, the oldest
for 16 days. The backtest keeps TP/SL on the transaction and never writes such rows, so it
kept evaluating the same positions: live and backtest disagreed on whether an exit rule runs.

Both arms here are the real passes (harness of test_short_selling_bt_live_parity.py):

* BACKTEST: the real ``DailyBacktestEngine``; a long opened with the TP/SL bracket, then a
  bearish recommendation fires the ``sell`` exit rule -> ``close_transaction``.
* LIVE: the real ``process_open_positions_recommendations`` over a real ``AlpacaAccount``
  (MagicMock client) holding the SAME long in the shape prod writes it: FILLED entry, its
  cancelled dependent SL, the OCO chained on that cancel, and the OCO's HELD stop leg.

The decision recorded at the shared account seam must be the same: the position is evaluated
and closed. A genuinely working close next to the OCO still blocks it (the 2026-07-21 guard).

Run from the backend dir:
    python -m pytest tests/backtest/test_pending_close_protection_parity.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace


from tests.backtest.test_short_selling_bt_live_parity import (
    BUY, SELL, SYMBOL, PRICE, _as_decision, _bt_netting, _bt_txn_id, _live_world,
    _spy_account_seam, _write_recommendation)
from tests.backtest.test_short_selling_engine import DAY1, _signal_rule

RULE = _signal_rule("bearish", "sell")


def _live_protected_long(monkeypatch, run_id, held, *, working_close=False):
    """One OPENED long of ``held`` shares with prod's OCO shape resting on it, then the live
    open-positions pass on a bearish recommendation."""
    from app.services.backtest.default_rulesets import seed_exit_ruleset_from_rules
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.types import (AnalysisUseCase, OrderDirection, OrderStatus, OrderType,
                                       TransactionStatus)

    calls = []
    with _live_world(monkeypatch, run_id,
                     open_id=lambda: seed_exit_ruleset_from_rules(
                         [RULE], name=f"protected-open-live-{run_id}"),
                     positions=[SimpleNamespace(symbol=SYMBOL, qty=held)]) as w:
        _spy_account_seam(monkeypatch, type(w.account), calls)
        now = datetime.now(timezone.utc)
        txn_id = add_instance(Transaction(
            symbol=SYMBOL, quantity=held, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=PRICE, open_date=now,
            take_profit=PRICE * 1.10, stop_loss=PRICE * 0.92, expert_id=w.id, created_at=now))

        def order(**kw):
            base = dict(account_id=w.id, symbol=SYMBOL, quantity=held, side=OrderDirection.SELL,
                        transaction_id=txn_id, filled_qty=0.0)
            return add_instance(TradingOrder(**{**base, **kw}))

        entry = order(side=OrderDirection.BUY, order_type=OrderType.MARKET,
                      status=OrderStatus.FILLED, filled_qty=held, open_price=PRICE,
                      broker_order_id="brk-entry", created_at=now)
        sl = order(order_type=OrderType.SELL_STOP, status=OrderStatus.CANCELED,
                   stop_price=PRICE * 0.92, depends_on_order=entry,
                   depends_order_status_trigger=OrderStatus.FILLED, broker_order_id="brk-sl",
                   comment=f"20260910133600-SL-[ACC:{w.id}/TR:{txn_id}/PORD:{entry}]",
                   created_at=now + timedelta(seconds=1))
        oco = order(order_type=OrderType.OCO, status=OrderStatus.NEW, limit_price=PRICE * 1.10,
                    stop_price=PRICE * 0.92, depends_on_order=sl,
                    depends_order_status_trigger=OrderStatus.CANCELED, broker_order_id="brk-oco",
                    comment=f"20260910144853-TPSL-[ACC:{w.id}/TR:{txn_id}/PORD:{entry}]",
                    created_at=now + timedelta(minutes=70))
        order(order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.HELD,
              limit_price=PRICE * 0.91, stop_price=PRICE * 0.92, parent_order_id=oco,
              broker_order_id="brk-oco-sl",
              comment=f"1789051776-OCO-SL-[PARENT:{oco}/BROKER:brk-oco]",
              created_at=now + timedelta(minutes=71))
        if working_close:
            order(order_type=OrderType.MARKET, status=OrderStatus.NEW, broker_order_id="brk-close",
                  comment=f"Closing position for transaction {txn_id}",
                  created_at=now + timedelta(minutes=90))

        assert w.account.has_pending_closing_order(txn_id) is working_close
        _write_recommendation(w.id, SELL, AnalysisUseCase.OPEN_POSITIONS)
        w.tm.process_open_positions_recommendations(w.id, lookback_days=1)
        return calls, txn_id


def test_an_oco_protected_long_is_evaluated_and_closed_in_both(monkeypatch):
    bt_calls, _outcome, [bt] = _bt_netting(
        monkeypatch, {DAY1: BUY, date(2024, 1, 4): SELL}, RULE, 1210)
    assert bt["side"] == "BUY" and bt["status"] == "CLOSED"
    assert bt["take_profit"] and bt["stop_loss"], "the backtest long carried a TP/SL bracket too"
    bt_txn = _bt_txn_id(bt_calls)

    live_calls, live_txn = _live_protected_long(monkeypatch, 1211, held=bt["quantity"])

    assert _as_decision(bt_calls, bt_txn) == _as_decision(live_calls, live_txn) == [
        ("close_transaction", "the position", None)]


def test_a_working_close_beside_the_oco_still_blocks_a_second_one(monkeypatch):
    live_calls, _txn = _live_protected_long(monkeypatch, 1221, held=100.0, working_close=True)
    assert live_calls == [], "a second close was submitted over a working one"
