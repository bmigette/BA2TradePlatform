"""Prereq 2 / S1 fidelity: the initial TP bracket can REFERENCE the expert's target price.

The default bracket (``initial_tp_reference`` unset / not ``expert_target_price``) is the
legacy percent-off-entry behaviour and MUST stay byte-identical. When the run config sets
``initial_tp_reference="expert_target_price"`` the engine instead anchors the take-profit on
the recommendation that opened the position:

  * use the linked ``ExpertRecommendation.target_price`` (FMPRating surfaces it);
  * if that is None, FALL BACK to ``entry_px * (1 + expected_profit_percent/100)`` (works for
    EVERY expert — they all populate expected_profit_percent);
  * if THAT is also unavailable, fall back to the configured ``initial_tp_percent``.

The ``initial_tp_percent`` value is reused as the OPTIMIZABLE offset-from-target in this mode
(TP = target * (1 + offset/100) for a long, mirrored for a short); offset 0 -> TP exactly at
the target. SL keeps the existing configured ``initial_sl_percent`` behaviour.

These are focused unit tests over ``_apply_initial_brackets`` (and the persist helper) against
a fresh per-run backtest DB — no network, no provider, no full engine loop. The opened
transaction + its filled entry order (linked to a seeded ExpertRecommendation) are wired
directly, exactly like the round-trip-trades harness.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_expert_target_bracket.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ba2_common.core.types import (
    OrderDirection,
    OrderRecommendation,
    OrderStatus,
    OrderType,
    Recommendation,
    RiskLevel,
    TimeHorizon,
    TransactionStatus,
)

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

D1 = datetime(2024, 1, 2)
D2 = datetime(2024, 1, 3)


def _bars(symbol):
    return [
        {"Date": d, "Open": 100, "High": 200, "Low": 50, "Close": 100, "Volume": 1000}
        for d in (D1, D2)
    ]


def _acct(account_id, expert_id):
    """Wired BacktestAccount + seeded ExpertInstance over a fresh per-run backtest DB."""
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"target-bracket-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, CFG)
    rid = seed_enter_long_ruleset()
    seed_expert_instance(
        account_id=account_id, expert_class_name="_StubExpert",
        enter_market_ruleset_id=rid, instance_id=expert_id,
    )
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bars("AAPL"))
    acct = BacktestAccount(account_id, ps, CFG)
    resolver.register_account(account_id, acct)
    ps.set_clock(D1)
    return acct, ctx, ps


def _seed_recommendation(expert_id, *, target_price, expected_profit_percent=10.0,
                         action=OrderRecommendation.BUY):
    """Persist an ExpertRecommendation row; return its id."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertRecommendation

    row = ExpertRecommendation(
        instance_id=expert_id,
        symbol="AAPL",
        recommended_action=action,
        expected_profit_percent=float(expected_profit_percent),
        price_at_date=100.0,
        target_price=target_price,
        risk_level=RiskLevel.MEDIUM,
        time_horizon=TimeHorizon.MEDIUM_TERM,
        created_at=D1,
    )
    return add_instance(row)


def _open_position(acct, expert_id, rec_id, *, entry_px=100.0,
                   side=OrderDirection.BUY):
    """Create an OPENED transaction + its FILLED entry order linked to ``rec_id``.

    Returns the transaction (DB-attached). The entry order carries
    ``expert_recommendation_id`` so the bracket can resolve the expert target; the
    transaction has no TP/SL yet so ``_open_transactions_without_brackets`` finds it.
    """
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import Transaction, TradingOrder

    txn = Transaction(
        symbol="AAPL", quantity=10, side=side, open_price=entry_px,
        status=TransactionStatus.OPENED, expert_id=expert_id, open_date=D1,
    )
    txn_id = add_instance(txn)

    entry = TradingOrder(
        account_id=acct.id, symbol="AAPL", quantity=10, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        open_price=entry_px, transaction_id=txn_id,
        expert_recommendation_id=rec_id, broker_order_id=acct._next_broker_id(),
        comment="entry",
    )
    add_instance(entry)
    return get_instance(Transaction, txn_id)


def _engine(acct, expert_id, config):
    from app.services.backtest.daily_engine import DailyBacktestEngine

    base = {"start_date": D1, "end_date": D2, "enabled_instruments": ["AAPL"], "seed": 1}
    base.update(config)
    return DailyBacktestEngine(
        account=acct,
        experts=[(object(), expert_id, {}, 1)],
        price_source=acct._price,
        config=base,
        indicator_provider=object(),
    )


# ---------------------------------------------------------------------------
# Persist: rec.target_price -> ExpertRecommendation.target_price
# ---------------------------------------------------------------------------
def test_persist_carries_target_price():
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertRecommendation

    acct, ctx, ps = _acct(account_id=310, expert_id=310)
    try:
        rec = Recommendation(
            signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
            expected_profit_percent=12.0, target_price=130.0, details="d",
        )
        rec_id = _recommendation_to_expert_recommendation(
            rec, expert_instance_id=310, symbol="AAPL", as_of=D1)
        row = get_instance(ExpertRecommendation, rec_id)
        assert row.target_price == 130.0

        rec_none = Recommendation(
            signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
            expected_profit_percent=12.0, details="d")
        rid2 = _recommendation_to_expert_recommendation(
            rec_none, expert_instance_id=310, symbol="AAPL", as_of=D1)
        assert get_instance(ExpertRecommendation, rid2).target_price is None
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# expert_target_price reference
# ---------------------------------------------------------------------------
def test_tp_at_expert_target_offset_zero():
    """TP anchored EXACTLY at the recommendation's target_price (offset 0)."""
    acct, ctx, ps = _acct(account_id=311, expert_id=311)
    try:
        rec_id = _seed_recommendation(311, target_price=130.0)
        txn = _open_position(acct, 311, rec_id, entry_px=100.0)
        eng = _engine(acct, 311, {
            "initial_tp_reference": "expert_target_price",
            "initial_tp_percent": 0.0,   # offset 0 -> TP at target
            "initial_sl_percent": 5.0,
        })
        eng._apply_initial_brackets()

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(130.0)        # at the expert target
        assert t.stop_loss == pytest.approx(100.0 * 0.95)   # SL keeps percent-off-entry
    finally:
        ctx.__exit__(None, None, None)


def test_tp_offset_above_expert_target():
    """The reused tp gene is the offset-from-target: TP = target*(1+offset/100)."""
    acct, ctx, ps = _acct(account_id=312, expert_id=312)
    try:
        rec_id = _seed_recommendation(312, target_price=130.0)
        txn = _open_position(acct, 312, rec_id, entry_px=100.0)
        eng = _engine(acct, 312, {
            "initial_tp_reference": "expert_target_price",
            "initial_tp_percent": 10.0,   # +10% above the 130 target -> 143
            "initial_sl_percent": 5.0,
        })
        eng._apply_initial_brackets()

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(143.0)
    finally:
        ctx.__exit__(None, None, None)


def test_tp_short_target_below_entry():
    """For a SHORT the offset is applied BELOW the target (mirrors the long handling)."""
    acct, ctx, ps = _acct(account_id=313, expert_id=313)
    try:
        # short rec: target below entry (take profit when price falls to 80)
        rec_id = _seed_recommendation(
            313, target_price=80.0, action=OrderRecommendation.SELL)
        txn = _open_position(acct, 313, rec_id, entry_px=100.0, side=OrderDirection.SELL)
        eng = _engine(acct, 313, {
            "initial_tp_reference": "expert_target_price",
            "initial_tp_percent": 10.0,   # short: 80*(1-0.10)=72
            "initial_sl_percent": 5.0,
        })
        eng._apply_initial_brackets()

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(72.0)
        assert t.stop_loss == pytest.approx(100.0 * 1.05)  # short SL above entry
    finally:
        ctx.__exit__(None, None, None)


def test_tp_falls_back_to_expected_profit_when_no_target():
    """target_price=None -> TP = entry*(1+expected_profit_percent/100) (every expert path)."""
    acct, ctx, ps = _acct(account_id=314, expert_id=314)
    try:
        rec_id = _seed_recommendation(314, target_price=None, expected_profit_percent=8.0)
        txn = _open_position(acct, 314, rec_id, entry_px=100.0)
        eng = _engine(acct, 314, {
            "initial_tp_reference": "expert_target_price",
            "initial_tp_percent": 0.0,   # offset 0 -> at the derived target (108)
            "initial_sl_percent": 5.0,
        })
        eng._apply_initial_brackets()

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(108.0)  # 100*(1+8/100)
    finally:
        ctx.__exit__(None, None, None)


def test_tp_falls_back_to_percent_when_no_target_and_no_expected_profit():
    """No target AND no expected_profit -> the configured initial_tp_percent off entry."""
    acct, ctx, ps = _acct(account_id=315, expert_id=315)
    try:
        rec_id = _seed_recommendation(315, target_price=None, expected_profit_percent=0.0)
        txn = _open_position(acct, 315, rec_id, entry_px=100.0)
        eng = _engine(acct, 315, {
            "initial_tp_reference": "expert_target_price",
            "initial_tp_percent": 6.0,   # no target/profit -> percent off entry (106)
            "initial_sl_percent": 5.0,
        })
        eng._apply_initial_brackets()

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(106.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# DEFAULT (percent) path unchanged
# ---------------------------------------------------------------------------
def test_default_percent_path_unchanged():
    """No initial_tp_reference (or any non-expert value) -> legacy percent-off-entry TP.

    The recommendation HAS a target_price, but the default path must IGNORE it.
    """
    acct, ctx, ps = _acct(account_id=316, expert_id=316)
    try:
        rec_id = _seed_recommendation(316, target_price=130.0)
        txn = _open_position(acct, 316, rec_id, entry_px=100.0)
        eng = _engine(acct, 316, {
            "initial_tp_percent": 5.0,   # legacy: 100*(1+5/100)=105 (NOT the 130 target)
            "initial_sl_percent": 2.0,
        })
        eng._apply_initial_brackets()

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(105.0)
        assert t.stop_loss == pytest.approx(98.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Moving ONE protective leg must KEEP the other (the inflated open_at_end bug):
# a break-even-lock adjust_sl was cancelling the OCO and re-issuing an SL-only
# stop, silently dropping the take-profit so winners rode past +TP% unbounded.
# ---------------------------------------------------------------------------
def _active_legs(acct, txn):
    """Non-terminal protective legs for a transaction (TP carries limit_price, SL stop_price)."""
    return list(acct._existing_legs(txn))


def test_adjust_sl_preserves_existing_tp():
    """Break-even lock (adjust_sl) must NOT drop the take-profit leg."""
    acct, ctx, ps = _acct(7101, 7101)
    try:
        rec = _seed_recommendation(7101, target_price=150.0)
        txn = _open_position(acct, 7101, rec, entry_px=100.0)
        assert acct.adjust_tp_sl(txn, new_tp_price=122.0, new_sl_price=96.0, source="init")
        # Move the stop up to break-even — the TP (122) must survive.
        assert acct.adjust_sl(txn, new_sl_price=100.0, source="belock")

        legs = _active_legs(acct, txn)
        assert any((o.limit_price or 0) > 0 for o in legs), "TP leg was dropped when SL moved"
        assert any(abs((o.stop_price or 0) - 100.0) < 1e-6 for o in legs), "new BE stop missing"

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(122.0)   # TP preserved
        assert t.stop_loss == pytest.approx(100.0)     # SL moved to BE
    finally:
        ctx.__exit__(None, None, None)


def test_adjust_tp_preserves_existing_sl():
    """Symmetric: moving the take-profit must NOT drop the stop-loss leg."""
    acct, ctx, ps = _acct(7102, 7102)
    try:
        rec = _seed_recommendation(7102, target_price=150.0)
        txn = _open_position(acct, 7102, rec, entry_px=100.0)
        assert acct.adjust_tp_sl(txn, new_tp_price=122.0, new_sl_price=96.0, source="init")
        assert acct.adjust_tp(txn, new_tp_price=130.0, source="raise-tp")

        legs = _active_legs(acct, txn)
        assert any(abs((o.stop_price or 0) - 96.0) < 1e-6 for o in legs), "SL leg was dropped when TP moved"

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == pytest.approx(130.0)
        assert t.stop_loss == pytest.approx(96.0)
    finally:
        ctx.__exit__(None, None, None)
