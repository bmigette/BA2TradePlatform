"""Repeatable sample-backtest PERF harness (deterministic, hermetic — no network/data).

Runs a fixed synthetic daily backtest (N syms x M bars) with a stub expert that BUYs periodically,
an enter ruleset (bullish & flat -> buy) and an OPEN_POSITIONS exit ruleset (close at +profit% +
protective stop). Exercises the enter path (temp-list order flow) + the fill engine (bracket / TP-SL
fills) so the wall-clock reflects the order-simulator cost. Prints best-of-K wall time + per-sym-bar.

VERSION-SAFE: uses only APIs present in both pre-session (v869) and current code
(seed_enter_long_ruleset / seed_open_positions_ruleset / DailyBacktestEngine), so the SAME script
(kept in /tmp across a `git checkout`) measures both. Deterministic: no RNG, prices vary by index.

Run:  cd testplatform/backend && ~/ba2-venvs/test/bin/python ../../tools/perf_sample_bt.py
Env:  PERF_SYMS (default 20)  PERF_BARS (default 250)  PERF_RUNS (default 3)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

logging.disable(logging.CRITICAL)

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "testplatform", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

NSYMS = int(os.environ.get("PERF_SYMS", "20"))
NBARS = int(os.environ.get("PERF_BARS", "250"))
NRUNS = int(os.environ.get("PERF_RUNS", "3"))


def _bars(seed):
    d0 = date(2024, 1, 1)
    rows = []
    for k in range(NBARS):
        dd = d0 + timedelta(days=k)
        # deterministic oscillation so TP/SL brackets actually trigger + positions cycle
        p = 100.0 + (9 if (k + seed) % 7 < 3 else -7) * (((seed % 5) + 1) / 5.0) + (k % 13)
        rows.append({"Date": datetime(dd.year, dd.month, dd.day), "Open": p, "High": p * 1.03,
                     "Low": p * 0.97, "Close": p, "Volume": 1000})
    return rows


def _run_once():
    from app.services.backtest.backtest_db import (backtest_trading_db, seed_account_definition,
                                                   seed_expert_instance)
    from app.services.backtest.default_rulesets import (seed_enter_long_ruleset,
                                                       seed_open_positions_ruleset)
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.db import activity_logging_disabled, get_db
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderRecommendation, Recommendation
    from sqlmodel import Session, select, func

    syms = [f"S{i}" for i in range(NSYMS)]
    d0 = date(2024, 1, 1)
    days = [d0 + timedelta(days=k) for k in range(NBARS)]
    cfg = {"starting_cash": 5_000_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
           "fill_model": "next_bar_open"}

    class E(MarketExpertInterface):
        def __init__(self, i, ps):
            self.id = i; self._ps = ps; self._settings_cache = None
        @classmethod
        def description(cls):
            return "perf stub"
        def render_market_analysis(self, m):
            return ""
        def run_analysis(self, s, m):
            return None
        def analyze_as_of(self, as_of, ctx):
            sym = getattr(self, "_gather_symbol", "S0")
            close = self._ps.close_at(sym, as_of) or 100.0
            sig = OrderRecommendation.BUY if (as_of.day % 6 == 2) else OrderRecommendation.HOLD
            return Recommendation(signal=sig, confidence=90.0, current_price=float(close),
                                  details="x", expected_profit_percent=20.0)

    # exit ruleset: close at +profit% + a protective stop (exercises the bracket / fill machinery)
    exit_rules = [
        {"id": "tp", "action_type": "close",
         "conditions": {"type": "AND", "conditions": [
             {"id": "p", "field": "profit_loss_percent", "op": ">", "value": 5}]}},
        {"id": "sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -5.0,
         "conditions": {"type": "AND", "conditions": [{"id": "h", "field": "has_position"}]}},
    ]

    wire = wire_backtest_seams()
    with backtest_trading_db("perf-sample"), activity_logging_disabled():
        seed_account_definition(1, cfg)
        rid = seed_enter_long_ruleset(name="perf-enter")
        oid = seed_open_positions_ruleset(exit_rules, name="perf-exit")
        seed_expert_instance(account_id=1, expert_class_name="E", enter_market_ruleset_id=rid,
                             open_positions_ruleset_id=oid, instance_id=1)
        ps = AsOfPriceSource(ohlcv_provider=None)
        for i, sym in enumerate(syms):
            ps.load_bars(sym, _bars(i))
        acct = BacktestAccount(1, ps, cfg)
        wire.register_account(1, acct)
        exp = E(1, ps)
        exp.save_settings({"allow_automated_trade_opening": (True, "bool"),
                           "enable_buy": (True, "bool")})
        wire.register_expert(1, exp)
        eng = DailyBacktestEngine(
            account=acct, experts=[(exp, 1, {}, rid)], price_source=ps,
            config={"start_date": days[0], "end_date": days[-1], "enabled_instruments": syms,
                    "seed": 42}, indicator_provider=None)
        eng._indicator_provider = object()
        t = time.perf_counter()
        eng.run()
        dt = time.perf_counter() - t
        with Session(get_db().bind) as s:
            norders = s.exec(select(func.count()).select_from(TradingOrder)).one()
        return dt, int(norders)


def main() -> int:
    # warm (imports + seam wiring + first-touch), then timed runs
    _run_once()
    times = []
    orders = 0
    for _ in range(NRUNS):
        dt, orders = _run_once()
        times.append(dt)
    best = min(times)
    n = NSYMS * NBARS
    import statistics
    print(f"sample BT: {NSYMS} syms x {NBARS} bars = {n} sym-bars | orders/run={orders} | runs={NRUNS}")
    print(f"  wall best/median: {best:.3f}s / {statistics.median(times):.3f}s")
    print(f"  per-sym-bar best:  {best / n * 1000:.4f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
