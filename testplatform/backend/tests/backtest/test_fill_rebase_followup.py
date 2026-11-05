"""Fill-time re-base, review follow-up: the stamped anchor, the counters, the hook guard.

    python -m pytest tests/backtest/test_fill_rebase_followup.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ba2_common.core.tpsl_fill_rebase import ANCHOR_KEY, read_anchor
from tests.backtest.test_entry_bracket_engine import CFG, D1, D2, D3, _acct

A = pytest.approx


# --------------------------------------------------------------------------------------- limit entry
def _limit_entry_run(*, clear_stamp: bool):
    """Market 110 at the decision; stop = CURRENT_PRICE - 5% = 104.5; the entry is a LIMIT BUY at
    100 that fills at 100 on D2 (low 99). Returns (stop after fill, exit reasons, anchor)."""
    from ba2_common.core.db import add_instance, get_instance, update_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.TradeActions import AdjustStopLossAction
    from ba2_common.core.trade_cycle import record_level_anchor
    from ba2_common.core.types import (
        OrderDirection, OrderOpenType, OrderRecommendation, OrderStatus, OrderType)

    acct, ctx, ps = _acct([(D1, 110, 111, 109, 110), (D2, 101, 102, 99, 100), (D3, 100, 101, 98, 100)],
                          account_id=7)
    try:
        ps.set_clock(D1)
        entry = TradingOrder(
            account_id=7, symbol="AAPL", side=OrderDirection.BUY, quantity=7.0,
            order_type=OrderType.BUY_LIMIT, limit_price=100.0, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC, comment="limit-entry", created_at=datetime.now(timezone.utc))
        entry = get_instance(TradingOrder, add_instance(entry))
        acct._create_transaction_for_order(entry)
        update_instance(entry)
        from ba2_common.core.models import ExpertRecommendation
        from ba2_common.core.types import RiskLevel, TimeHorizon
        rec = get_instance(ExpertRecommendation, add_instance(ExpertRecommendation(
            instance_id=1, symbol="AAPL", recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=5.0, price_at_date=110.0, confidence=70.0,
            risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM, details="limit-entry")))
        entry.expert_recommendation_id = rec.id
        update_instance(entry)
        action = AdjustStopLossAction(
            "AAPL", acct, OrderRecommendation.BUY, existing_order=entry, expert_recommendation=rec,
            reference_value="current_price", percent=-5.0)
        res = action.execute()
        txn = get_instance(Transaction, entry.transaction_id)
        assert txn.stop_loss == A(104.5), res.message
        assert read_anchor(txn.meta_data, "stop") == A(110.0)        # stamped by the action
        if clear_stamp:
            record_level_anchor(txn.id, stop=None)                  # an old-style stop: no anchor
        acct.submit_order(get_instance(TradingOrder, entry.id))
        ps.set_clock(D1)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert get_instance(TradingOrder, entry.id).open_price == A(100.0)
        txn = get_instance(Transaction, txn.id)
        stop = txn.stop_loss
        ps.set_clock(D2)
        acct.refresh_orders()
        acct.refresh_transactions()
        reasons = [t["exit_reason"] for t in acct.get_round_trip_trades()]
        return stop, reasons, acct.fill_rebase_record()
    finally:
        ctx.__exit__(None, None, None)


def test_a_limit_entry_stop_built_from_the_market_price_is_rebased_against_that_price():
    stop, reasons, rec = _limit_entry_run(clear_stamp=False)
    assert stop == A(100.0 * 104.5 / 110.0)          # 95.0, below the 100 fill
    assert "stop_loss" not in reasons                # D2/D3 lows (98, 99) stay above 95
    assert rec["stop_rebased"] == 1 and rec["fallback_reference"] == 0 and rec["no_reference"] == 0


def test_without_the_stamp_the_limit_price_chain_leaves_the_stop_above_the_fill():
    """The flaw the stamp removes: the fallback chain takes the limit price (100) as the reference,
    nothing moves, and the stop sits ABOVE the fill so the position is stopped out at once."""
    stop, reasons, rec = _limit_entry_run(clear_stamp=True)
    assert stop == A(104.5)
    assert reasons == ["stop_loss"]
    assert rec["fallback_reference"] == 1


# --------------------------------------------------------------------------------------- safeguard
@pytest.mark.parametrize("account_id, ruleset_sl, stamped", [
    (411, None, 100.0),      # the RM safeguard (92) governs: anchor = the price it was sized on
    (412, 97.0, None),       # a tighter ruleset stop governs: its own action stamps it, not the tail
])
def test_the_safeguard_tail_stamps_its_anchor_only_when_it_governs(account_id, ruleset_sl, stamped):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from tests.backtest.test_entry_bracket_engine import _precedence_setup

    engine, account, txn_id, ctx = _precedence_setup(
        account_id=account_id, expert_id=account_id, ruleset_sl_price=ruleset_sl)
    try:
        engine._size_and_submit(account_id, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        txn = get_instance(Transaction, txn_id)
        assert read_anchor(txn.meta_data, "stop") == (A(stamped) if stamped else None)
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------------------- counters
class _Acct:
    def __init__(self, **c):
        self._c = {"enabled": True, "entries_with_levels": 0, "entries_with_stop": 0, "rebased": 0,
                   "stop_rebased": 0, "tp_floored": 0, "fallback_reference": 0, "no_reference": 0, **c}

    def fill_rebase_record(self):
        return dict(self._c)


def _engine(acct):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    e = DailyBacktestEngine.__new__(DailyBacktestEngine)
    e.account = acct
    return e


def test_unanchored_stops_beyond_the_share_refuse_the_run():
    from app.services.backtest.daily_engine import (
        FillRebaseRefusal, MAX_UNANCHORED_STOP_SHARE, MIN_STOP_ENTRIES_FOR_REBASE_REFUSAL)
    n = MIN_STOP_ENTRIES_FOR_REBASE_REFUSAL * 2
    bad = int(n * MAX_UNANCHORED_STOP_SHARE) + 1
    with pytest.raises(FillRebaseRefusal):
        _engine(_Acct(entries_with_stop=n, no_reference=bad)).refuse_if_rebase_unanchored()


def test_a_small_share_or_a_tiny_sample_does_not_refuse_but_is_reported(monkeypatch):
    from app.services.backtest import daily_engine as DE
    seen = []
    monkeypatch.setattr(DE.logger, "warning", lambda msg, *a, **k: seen.append(msg))
    n = DE.MIN_STOP_ENTRIES_FOR_REBASE_REFUSAL * 10
    rec = _engine(_Acct(entries_with_stop=n, no_reference=1)).refuse_if_rebase_unanchored()
    _engine(_Acct(entries_with_stop=3, no_reference=3)).refuse_if_rebase_unanchored()
    assert rec["no_reference"] == 1
    assert len(seen) == 2 and all("NO usable reference" in m or "no_reference" in m or "usable" in m for m in seen)


def test_the_refusal_is_job_fatal():
    from app.services.job_fatal import job_fatal
    assert job_fatal("FillRebaseRefusal") and job_fatal("FillRebaseDisabled")


# --------------------------------------------------------------------------------------- hook guard
def test_the_record_says_when_the_hook_is_on(monkeypatch):
    from app.services.backtest.backtest_account import BacktestAccount
    acct = BacktestAccount.__new__(BacktestAccount)
    assert acct.fill_rebase_record()["enabled"] is True
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    assert acct.fill_rebase_record()["enabled"] is False


def test_the_assertion_refuses_when_the_hook_is_on(monkeypatch):
    from app.services.backtest.backtest_account import (
        BacktestAccount, FillRebaseDisabled, assert_fill_rebase_enabled)
    assert_fill_rebase_enabled()                       # off by default: fine
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    with pytest.raises(FillRebaseDisabled):
        assert_fill_rebase_enabled()


def test_a_grid_trial_worker_refuses_with_the_hook_on(monkeypatch):
    from app.services import strategy_optimization_handler as H
    from app.services.backtest.backtest_account import BacktestAccount
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    out = H._trial_worker({"symbols": ["NFLX"]}, "calmar")
    assert out["ok"] is False and out["fatal"] is True and out["error_type"] == "FillRebaseDisabled"
    with pytest.raises(Exception) as ei:
        H._persist_trial_worker({"symbols": ["NFLX"]})
    assert type(ei.value).__name__ == "FillRebaseDisabled"


def test_the_ga_master_refuses_to_start_with_the_hook_on(monkeypatch):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import test_strategy_optimization_handler as T
    from app.models.database import Base, engine
    from app.services import strategy_optimization_handler as H
    from app.services.backtest.backtest_account import BacktestAccount
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=4, generations=2))
    out = H.handle_strategy_optimization("t-hook-on", {"optimization_id": opt_id})
    assert out["status"] == "failed" and "_MEASURE_NO_FILL_REBASE" in out["error"]


# --------------------------------------------------------------------------------------- results blob
def test_the_results_blob_carries_the_flag_and_the_counters():
    from tests.backtest.fixtures.e2e_support import (
        earnings_drift_payload, ensure_host_schema, load_backtest, new_backtest_row, run_daily_backtest)
    ensure_host_schema()
    bt_id = new_backtest_row("fill-rebase-results-blob")
    result = run_daily_backtest(earnings_drift_payload(bt_id, seed=42), task_id="fr-blob")
    assert result["status"] == "completed", result.get("error")
    res = load_backtest(bt_id).results
    assert res["fill_rebase_enabled"] is True
    fr = res["fill_rebase"]
    assert fr["enabled"] is True and fr["no_reference"] == 0
    assert fr["entries_with_levels"] >= 1
    assert fr["fallback_reference"] == 0, "every stop in a normal run carries its stamped anchor"
