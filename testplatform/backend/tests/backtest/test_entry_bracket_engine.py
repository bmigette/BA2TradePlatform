"""Entry-bracket engine tests (Task 2 of the entry-time TP/SL bracket plan).

An entry-rule bracket (``adjust_take_profit``/``adjust_stop_loss`` actions on the
ENTER_MARKET rule) attaches its protective leg via ``TradeActionEvaluator``'s
Phase 1.5, which runs BEFORE the risk manager sizes the entry order — so at leg
creation time the entry order is still PENDING with ``quantity == 0`` and
``BacktestAccount._replace_leg`` naively copies that 0 onto the leg. This file
proves the leg is re-synced to the entry's REAL sized quantity at the
WAITING_TRIGGER -> ACCEPTED promotion (which runs exactly when the parent entry
order reaches FILLED), so the bracket closes the whole position instead of 0
shares.

This file grows in later tasks of the same plan (RM-safeguard vs ruleset-SL
precedence, end-to-end config wiring) — see
``docs/plans/2026-07-03-entry-tp-sl-bracket-actions.md``.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_entry_bracket_engine.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

# No slippage / no commission so price/quantity assertions are exact.
CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

D1 = datetime(2024, 1, 2)
D2 = datetime(2024, 1, 3)
D3 = datetime(2024, 1, 4)


def _bars(rows):
    """rows: list of (date, open, high, low, close) -> OHLCV row dicts."""
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


def _acct(rows, cfg=CFG, symbol="AAPL", account_id=1):
    """Build a wired BacktestAccount over a fresh per-run backtest DB + hand-built bars.

    Mirrors ``test_backtest_account_fills.py``'s ``_acct`` helper (the existing
    OCO/fill-test fixture pattern). Returns (account, db_context, price_source);
    the caller MUST close the context.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db(f"entry-bracket-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, _bars(rows))
    acct = BacktestAccount(account_id, ps, cfg)
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ctx, ps


def test_waiting_leg_quantity_syncs_to_parent_fill():
    """A protective leg created while the entry was PENDING qty=0 (entry-rule bracket)
    must inherit the parent's SIZED quantity when promoted at fill — otherwise the
    bracket closes 0 shares."""
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus, OrderOpenType
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.db import add_instance, get_instance, update_instance

    acct, ctx, ps = _acct(
        [
            (D1, 100, 101, 99, 100),
            (D2, 102, 103, 101, 102),
            (D3, 104, 107, 103, 106),
        ]
    )
    try:
        ps.set_clock(D1)

        # 1. Entry order PENDING qty=0 + its transaction — the Phase 1 / Phase 1.5
        #    analog (BuyAction.execute() creates the order via add_instance with
        #    quantity=0.0, status=PENDING; TradeActionEvaluator's Phase 1.5 then calls
        #    account._create_transaction_for_order(order) BEFORE the RM sizes it).
        entry = TradingOrder(
            account_id=1,
            symbol="AAPL",
            side=OrderDirection.BUY,
            quantity=0.0,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment="entry-bracket-test",
            created_at=datetime.now(timezone.utc),
        )
        entry_id = add_instance(entry)
        entry = get_instance(TradingOrder, entry_id)
        acct._create_transaction_for_order(entry)
        update_instance(entry)
        txn = get_instance(Transaction, entry.transaction_id)

        # 2. adjust_tp_sl(txn, tp, sl) -> OCO leg, quantity copied from the still-unsized
        #    entry (quantity == 0) — this is the bug this test guards against.
        assert acct.adjust_tp_sl(txn, new_tp_price=120.0, new_sl_price=90.0) is True
        legs = [o for o in acct.get_orders() if o.depends_on_order == entry.id]
        assert len(legs) == 1
        leg = legs[0]
        assert leg.status == OrderStatus.WAITING_TRIGGER
        assert leg.quantity == 0

        # 3. Size the entry (risk-manager analog: quantity=7), submit + fill it on the
        #    next bar (D1 -> D2 open).
        entry.quantity = 7.0
        update_instance(entry)
        acct.submit_order(entry)
        ps.set_clock(D1)
        acct.refresh_orders()  # entry MARKET fills at D2 open
        filled_entry = acct.get_order(entry.broker_order_id)
        assert filled_entry.status == OrderStatus.FILLED
        assert filled_entry.filled_qty == 7

        # 4. Run the promotion pass: WAITING_TRIGGER -> ACCEPTED runs inside
        #    refresh_orders() (_activate_triggered_dependents), exactly when the parent
        #    entry reaches its trigger status (FILLED).
        ps.set_clock(D2)
        acct.refresh_orders()

        # 5. The leg is ACCEPTED and picked up the parent's REAL filled quantity.
        promoted = acct.get_order(leg.broker_order_id)
        assert promoted.status in (OrderStatus.ACCEPTED, OrderStatus.FILLED)
        assert promoted.quantity == 7
    finally:
        ctx.__exit__(None, None, None)
