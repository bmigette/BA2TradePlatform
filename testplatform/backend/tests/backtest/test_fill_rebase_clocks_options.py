"""Fill-time re-base: the measurement hook, the trade rows, the INTRADAY clock, and options.

Companion of test_fill_rebase_engine.py (daily clock, long/short, floor).

    python -m pytest tests/backtest/test_fill_rebase_clocks_options.py -q
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.backtest.test_fill_rebase_engine import LONG_GAP_UP, _go
from tests.backtest.test_max_loss_stop_engine import _store_mode
from tests.backtest.test_short_selling_engine import BUY


# --------------------------------------------------------------------------- the measurement hook
def test_without_the_rebase_the_old_behaviour_is_unchanged(monkeypatch):
    """The measurement hook (tools/measure_variant.py ``+norebase``) restores the pre-fix backtest:
    the stop stays at the level built from the decision price and the 94-low bar does NOT stop it
    out of the long that filled 3% higher. This is also the 'fails before' proof of the tests in
    test_fill_rebase_engine.py (same fixture, opposite outcome)."""
    from app.services.backtest.backtest_account import BacktestAccount
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    outcome, [txn] = _go(monkeypatch, LONG_GAP_UP, BUY, run_id=1310)
    assert txn["stop_loss"] == pytest.approx(92.0)
    assert [t for t in outcome["trades"] if t["exit_reason"] == "stop_loss"] == []


# --------------------------------------------------------------------------- the trade rows
def test_the_trade_row_carries_the_levels_after_and_before_the_fill(monkeypatch):
    outcome, _ = _go(monkeypatch, LONG_GAP_UP, BUY, run_id=1320)
    [t] = outcome["trades"]
    assert t["stop_loss_at_fill"] == pytest.approx(94.76) and t["stop_loss_pre_fill"] == pytest.approx(92.0)
    assert t["take_profit_at_fill"] == pytest.approx(110.0) and t["take_profit_pre_fill"] == pytest.approx(110.0)
    assert t["fill_reference_price"] == pytest.approx(100.0)
    from app.services.backtest.results import _trade_row
    row = _trade_row(t)
    assert row["stop_loss_at_fill"] == pytest.approx(94.76) and row["stop_loss_pre_fill"] == pytest.approx(92.0)


# --------------------------------------------------------------------------- the intraday clock
def _intraday_bars():
    """5-minute bars. The decision at 09:40 prices at the close of the bar that ENDED at 09:40
    (stamped 09:35: 100); the order fills at the OPEN of the next bar (09:45: 103, +3%); the fill bar
    is not tested; the 09:50 bar trades down to 94 (below the re-based 94.76, above the pre-fill 92)."""
    prior = [(datetime(2023, 12, 29, 9, 30 + 5 * i), 100, 100.5, 99.5, 100) for i in range(4)]
    day = [
        (datetime(2024, 1, 2, 9, 30), 100, 100.5, 99.5, 100),
        (datetime(2024, 1, 2, 9, 35), 100, 100.5, 99.5, 100),
        (datetime(2024, 1, 2, 9, 40), 100, 100.5, 99.5, 100),
        (datetime(2024, 1, 2, 9, 45), 103, 104, 102, 103),
        (datetime(2024, 1, 2, 9, 50), 103, 103.5, 94, 95),
        (datetime(2024, 1, 2, 9, 55), 95, 96, 94.5, 95),
        (datetime(2024, 1, 2, 10, 0), 95, 96, 94.5, 95),
    ]
    return prior + day


def _run_intraday(run_id):
    """Full engine.run() on a 5-minute clock; one BUY decision at 09:40 on 2024-01-02."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_instance
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderRecommendation, Recommendation
    from tests.backtest.test_max_loss_stop_engine import CFG
    from tests.backtest.test_short_selling_engine import BRACKET as BR

    class _IntradayBuy(MarketExpertInterface):
        def __init__(self, id, ps):
            super().__init__(id)
            self._ps = ps

        @classmethod
        def description(cls):
            return "intraday fill-rebase stub"

        def render_market_analysis(self, market_analysis):
            return ""

        def run_analysis(self, symbol, market_analysis):
            return None

        def analyze_as_of(self, as_of, context):
            sig = (OrderRecommendation.BUY if (as_of.month, as_of.day, as_of.hour, as_of.minute) == (1, 2, 9, 40)
                   else OrderRecommendation.HOLD)
            px = self._ps.decision_price("AAPL", as_of)
            return Recommendation(signal=sig, confidence=80.0, current_price=px, details="stub",
                                  expected_profit_percent=0.0 if sig == OrderRecommendation.HOLD else 10.0)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"fill-rebase-intraday-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(run_id, CFG)
        enter_id = seed_ruleset_from_tree(None, name=f"fr-intraday-{run_id}", enable_short=False,
                                          entry_actions=BR)
        seed_expert_instance(account_id=run_id, expert_class_name="_IntradayBuy",
                             enter_market_ruleset_id=enter_id, instance_id=run_id)
        ps = AsOfPriceSource(ohlcv_provider=None, interval="5min")
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1000}
                              for d, o, h, lo, c in _intraday_bars()])
        account = BacktestAccount(run_id, ps, CFG)
        resolver.register_account(run_id, account)
        expert = _IntradayBuy(run_id, ps)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"), "allow_automated_trade_modification": (True, "bool"),
            "enable_buy": (True, "bool"), "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"), "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool")})
        resolver.register_expert(run_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, run_id, {}, enter_id)], price_source=ps,
            config={"start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 2, 23, 59),
                    "enabled_instruments": ["AAPL"], "seed": 42,
                    "run_schedule_override": {"days": {d: True for d in (
                        "monday", "tuesday", "wednesday", "thursday", "friday")}, "times": ["09:40"]}},
            indicator_provider=None)
        engine._indicator_provider = None
        engine.run()
        entries = [o for o in account.get_orders() if o.depends_on_order is None and o.transaction_id]
        txns = [get_instance(Transaction, o.transaction_id) for o in entries]
        return [(t.stop_loss, t.take_profit) for t in txns], account.get_round_trip_trades()
    finally:
        ctx.__exit__(None, None, None)


def test_intraday_clock_fill_is_the_next_bars_open_and_the_rebased_stop_is_tested(monkeypatch):
    _store_mode(monkeypatch, "1")
    levels, trades = _run_intraday(1330)
    [(sl, tp)] = levels
    assert sl == pytest.approx(103.0 * 0.92)         # 94.76: the 09:45 open x the 8% stop distance
    assert tp == pytest.approx(110.0)
    [t] = [t for t in trades if t["exit_reason"] == "stop_loss"]
    assert t["entry_price"] == pytest.approx(103.0) and t["exit_price"] == pytest.approx(94.76)
    assert t["fill_reference_price"] == pytest.approx(100.0)   # the decision price, not a daily bar


def test_intraday_clock_without_the_rebase_keeps_the_decision_price_stop(monkeypatch):
    from app.services.backtest.backtest_account import BacktestAccount
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    _store_mode(monkeypatch, "1")
    levels, trades = _run_intraday(1331)
    assert levels[0][0] == pytest.approx(92.0)
    assert [t for t in trades if t["exit_reason"] == "stop_loss"] == []


# --------------------------------------------------------------------------- options
def test_a_covered_call_run_is_identical_with_and_without_the_rebase(monkeypatch):
    """O_CC buys SHARES (the equity entry path) and writes a call. On its fixture the shares fill
    at the decision close, so the re-base is a no-op and every order/trade is byte-identical
    with the measurement hook on and off; the option legs never reach the re-base at all."""
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_covered_call_engine import _run as cc_run

    def shape(res):
        _acct, orders, _calls, trips = res
        new_keys = ("stop_loss_at_fill", "take_profit_at_fill", "stop_loss_pre_fill",
                    "take_profit_pre_fill", "fill_reference_price")
        return ([(o.symbol, o.side.value, o.order_type.value, o.status.value, o.quantity,
                  o.filled_qty, o.open_price, o.stop_price, o.limit_price) for o in orders],
                # the new level-record keys are the ONLY difference allowed on the equity rows
                [{k: v for k, v in t.items() if k not in new_keys} for t in trips])

    with_rebase = cc_run(871)
    seen = dict(with_rebase[0].__dict__.get("_fill_rebase_records") or {})
    monkeypatch.setattr(BacktestAccount, "_MEASURE_NO_FILL_REBASE", True)
    without = cc_run(871)      # same id: the rule names carry it
    (o1, t1), (o2, t2) = shape(with_rebase), shape(without)
    assert o1 == o2
    assert len(t1) == len(t2)
    diffs = [(i, k, a.get(k), b.get(k)) for i, (a, b) in enumerate(zip(t1, t2))
             for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    assert diffs == []
    assert all(not (r["stop_rebased"] or r["tp_floored"]) for r in seen.values()), seen
