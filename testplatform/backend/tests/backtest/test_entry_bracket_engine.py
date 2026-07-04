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

Task 3 of the same plan (below): the classic RM's safeguard stop (``order.stop_price``,
synthesized by ``TradeRiskManagement`` in ``risk_atr`` sizing mode when the strategy sets
none) and an entry-bracket ruleset SL (``Transaction.stop_loss``, attached by Phase 2's
``adjust_stop_loss`` action BEFORE the RM sizes the order) can now legitimately coexist —
``daily_engine._size_and_submit`` must make the TIGHTER of the two win rather than letting
the safeguard silently clobber a tighter strategy stop (or vice versa).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_entry_bracket_engine.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


# ---------------------------------------------------------------------------
# Task 3: RM safeguard vs ruleset entry-bracket SL — tighter-wins at submit.
#
# NOTE ON TEST STYLE (deviation from the plan's literal fixture-reuse suggestion): the
# plan's example fixture (``bt_engine_fixture``) doesn't exist; the closest reusable
# thing is ``test_daily_engine_unit.py``'s ``_build_run`` (drives a FULL bar-by-bar
# ``engine.run()`` through the real ``TradeActionEvaluator`` + stub ``analyze_as_of``).
# Reusing that verbatim would require the entry-bracket ruleset to ALSO be wired through
# ``seed_ruleset_from_tree(..., entry_sl_percent=...)`` and would leave the RM's safeguard
# stop price to a chain of as_of/fill-timing-dependent references (ORDER_OPEN_PRICE for the
# bracket vs the RM's own current-price lookup) that are hard to pin down deterministically
# without duplicating a lot of the engine's own machinery in the test.
#
# Instead these tests build the exact PENDING-entry + Transaction state Phase 1.5 leaves
# behind (mirroring ``test_waiting_leg_quantity_syncs_to_parent_fill`` above: manually
# ``add_instance`` the PENDING qty=0 entry order, call the REAL
# ``account._create_transaction_for_order``, then call the REAL ``account.adjust_sl`` to
# attach the ruleset's bracket SL to the transaction — exactly what the entry rule's
# ``adjust_stop_loss`` action does), configure the expert for deterministic ``risk_atr``
# sizing (ATR disabled -> the safeguard is purely ``risk_per_trade_pct`` floored at
# ``min_stop_loss_pct``, both set to 8% here so the safeguard is EXACTLY -8% of the $100
# current price = $92), and then drive the REAL ``DailyBacktestEngine._size_and_submit``
# (the method under test, unchanged) end-to-end. This proves the actual precedence code
# added to ``daily_engine.py``, not a reimplementation of it, while sidestepping the
# fragile timing/reference-price wiring a full ``engine.run()`` would need.
# ---------------------------------------------------------------------------

def _precedence_setup(account_id: int, expert_id: int, ruleset_sl_price: "float | None"):
    """Wire an engine + a PENDING qty=0 entry order with a REAL Transaction, optionally
    carrying a pre-attached entry-bracket SL (``Transaction.stop_loss``) — the exact state
    Phase 2's ``adjust_stop_loss`` action leaves behind BEFORE the classic RM sizes the
    order. The expert is configured for ``risk_atr`` sizing with ATR disabled and
    risk_per_trade_pct == min_stop_loss_pct == 8%, so ``synthesize_safeguard_stop`` always
    produces a deterministic safeguard of exactly -8% off the $100 current price ($92 for a
    long). Returns (engine, account, transaction_id, ctx); caller MUST close ctx.
    """
    from app.services.backtest.backtest_db import seed_expert_instance
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.daily_engine import (
        DailyBacktestEngine,
        _recommendation_to_expert_recommendation,
    )
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.types import (
        OrderDirection, OrderType, OrderStatus, OrderOpenType,
        OrderRecommendation, Recommendation,
    )
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.db import add_instance, get_instance, update_instance

    cfg = {
        "starting_cash": 100_000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }
    as_of = datetime(2024, 1, 2)

    # Reuse _acct's bootstrap (wire_backtest_seams -> backtest_trading_db ->
    # seed_account_definition -> AsOfPriceSource -> BacktestAccount -> register_account)
    # instead of re-implementing it here.
    account, ctx, ps = _acct([(as_of, 100, 101, 99, 100)], cfg=cfg, account_id=account_id)
    ps.set_clock(as_of)
    resolver = wire_backtest_seams()  # idempotent, same resolver _acct already registered on

    ruleset_id = seed_enter_long_ruleset()

    class _PrecedenceStubExpert(MarketExpertInterface):
        @classmethod
        def description(cls) -> str:
            return "Stub expert for the SL-precedence tests."

        def render_market_analysis(self, market_analysis) -> str:
            return ""

        def run_analysis(self, symbol: str, market_analysis) -> None:
            return None

    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_PrecedenceStubExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    expert = _PrecedenceStubExpert(expert_id)
    expert.save_settings(
        {
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
        }
    )
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": as_of,
        "end_date": as_of,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, {}, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=None,
    )

    # Persist the BUY recommendation + PENDING qty=0 entry order — the Phase-1/1.5 analog
    # (TradeActionEvaluator's BUY action creates exactly this row before the RM sizes it).
    rec = Recommendation(
        signal=OrderRecommendation.BUY,
        confidence=80.0,
        current_price=100.0,
        details="sl-precedence test",
        expected_profit_percent=10.0,
    )
    rec_id = _recommendation_to_expert_recommendation(
        rec, expert_instance_id=expert_id, symbol="AAPL", as_of=as_of,
    )
    entry = TradingOrder(
        account_id=account_id,
        symbol="AAPL",
        side=OrderDirection.BUY,
        quantity=0.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        open_type=OrderOpenType.AUTOMATIC,
        expert_recommendation_id=rec_id,
        comment="entry-bracket-precedence-test",
        created_at=as_of,
    )
    entry_id = add_instance(entry)
    entry = get_instance(TradingOrder, entry_id)
    account._create_transaction_for_order(entry)
    update_instance(entry)

    # Simulate the entry-bracket rule's adjust_stop_loss action (Phase 2) having already
    # attached the ruleset SL to the transaction, via the REAL adjust_sl (creates the
    # WAITING_TRIGGER SL leg + sets Transaction.stop_loss — exactly what the rule's action
    # does), when a bracket SL is given for this scenario.
    if ruleset_sl_price is not None:
        txn = get_instance(Transaction, entry.transaction_id)
        assert account.adjust_sl(txn, ruleset_sl_price, source="entry_bracket") is True

    return engine, account, entry.transaction_id, ctx


def test_ruleset_sl_tighter_than_safeguard_wins():
    """Entry bracket SL at -3% (tighter) vs RM safeguard at -8%: the submitted
    protective stop must be the -3% one (transaction.stop_loss unchanged)."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    ruleset_sl = 97.0  # -3% off the $100 current price -> tighter than the -8% safeguard.
    engine, account, txn_id, ctx = _precedence_setup(
        account_id=201, expert_id=201, ruleset_sl_price=ruleset_sl,
    )
    try:
        engine._size_and_submit(201, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        txn = get_instance(Transaction, txn_id)
        assert txn.stop_loss == pytest.approx(ruleset_sl)
    finally:
        ctx.__exit__(None, None, None)


def test_safeguard_tighter_than_ruleset_sl_wins():
    """Entry bracket SL at -15% (looser) vs RM safeguard at -8%: the safeguard
    replaces it (long: max of the two stop prices)."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    ruleset_sl = 85.0  # -15% off the $100 current price -> looser than the -8% safeguard.
    engine, account, txn_id, ctx = _precedence_setup(
        account_id=202, expert_id=202, ruleset_sl_price=ruleset_sl,
    )
    try:
        engine._size_and_submit(202, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        txn = get_instance(Transaction, txn_id)
        assert txn.stop_loss == pytest.approx(92.0)  # $100 - 8% RM safeguard
    finally:
        ctx.__exit__(None, None, None)
