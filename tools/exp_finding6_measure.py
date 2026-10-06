#!/usr/bin/env python
"""EXPERIMENT (exp/finding6-measure): re-run a stored row (see rerun_stored_row.py) and record the
deployment figures finding 6 would move. Writes no DB row. Switch: BA2_EXP_FINDING6=1.

    tools/exp_finding6_measure.py 1107 --window 2026-01-01 2026-06-30 --out r.json
"""
import argparse
import bisect
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))

from ba2test_launcher import _enter_backend  # noqa: E402

_enter_backend()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backtest_id", type=int)
    ap.add_argument("--window", nargs=2, metavar=("START", "END"))
    ap.add_argument("--out", required=True)
    ns = ap.parse_args()

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == ns.backtest_id).first()
        config = rebuild_config_for_backtest(bt, db, window=ns.window)
        name = bt.name
    finally:
        db.close()
    config["backtest_id"] = 900_000_000 + ns.backtest_id
    config["name"] = f"F6-{name}"[:255]
    config["persist_trading_db"] = False

    # ---- instrumentation (read-only wrappers) -------------------------------------------
    snaps = []
    orig_snap = BacktestAccount.snapshot_equity

    def snap(self, as_of):
        r = orig_snap(self, as_of)
        cost = sum(abs(p.qty) * p.avg_price for p in self._positions.values() if p.qty != 0)
        n = sum(1 for p in self._positions.values() if p.qty != 0)
        snaps.append((str(as_of)[:10], float(self._cash), float(r["net_liquidating_value"]),
                      float(cost), n))
        return r

    BacktestAccount.snapshot_equity = snap

    traces = []
    orig_note = TradeRiskManagement._trace_note

    def note(self, trace, **f):
        if trace is not None and not any(t is trace for t in traces[-50:]):
            traces.append(trace)
        return orig_note(self, trace, **f)

    TradeRiskManagement._trace_note = note

    class Count(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.c = Counter()

        def emit(self, rec):
            m = rec.getMessage()
            for key in ("EXPERT BALANCE EXCEEDED", "POSITION SIZE LIMIT EXCEEDED", "EARLY SKIP",
                        "FINAL: Set quantity to 0"):
                if key in m:
                    self.c[key] += 1

    h = Count()
    logging.getLogger().addHandler(h)
    logging.getLogger("ba2_common").addHandler(h)

    prior = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        res = run_daily_backtest(config)
    finally:
        logging.disable(prior)

    trades = res.get("trades") or []
    dates = [s[0] for s in snaps]
    # one snapshot per day (the last of the day)
    daily = {}
    for s in snaps:
        daily[s[0]] = s
    days = sorted(daily)
    dep = [daily[d][3] / daily[d][2] * 100 if daily[d][2] > 0 else 0.0 for d in days]
    npos = [daily[d][4] for d in days]
    min_cash = min(s[1] for s in snaps)
    # entry size as % of equity at entry
    eq_by_day = [daily[d][2] for d in days]
    pct = []
    for t in trades:
        d = (t.get("entry_time") or "")[:10]
        i = bisect.bisect_right(days, d) - 1
        if i < 0 or not t.get("size") or not t.get("entry_price"):
            continue
        pct.append(abs(t["size"]) * t["entry_price"] / eq_by_day[i] * 100)
    final_traces = Counter()
    zero = 0
    for tr in traces:
        b = tr.get("binding")
        q = tr.get("quantity")
        if q == 0:
            zero += 1
            final_traces["ZERO:" + str(b)] += 1
        else:
            final_traces["sized:" + str(b)] += 1
    out = {
        "row": ns.backtest_id, "name": name, "window": ns.window, "switch": os.environ.get("BA2_EXP_FINDING6"),
        "trades": res.get("total_trades"), "total_return": res.get("total_return"),
        "annualized_return": res.get("annualized_return"), "max_drawdown": res.get("max_drawdown"),
        "final_equity": res.get("final_equity"), "win_rate": res.get("win_rate"),
        "symbols": len({t.get("symbol") for t in trades}),
        "deploy_avg_pct": sum(dep) / len(dep) if dep else None, "deploy_peak_pct": max(dep) if dep else None,
        "avg_positions": sum(npos) / len(npos) if npos else None, "max_positions": max(npos) if npos else None,
        "entry_size_avg_pct_equity": sum(pct) / len(pct) if pct else None,
        "min_cash": min_cash, "min_equity": min(eq_by_day) if eq_by_day else None, "days": len(days),
        "sizing_traces": len(traces), "sizing_zero_qty": zero, "sizing_bindings": dict(final_traces),
        "log_counts": dict(h.c),
        "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
        "snap_count": len(snaps), "curve_points": len(res.get("equity_curve") or []),
        "daily": [daily[d] for d in days],
        "open_positions_end": len(res.get("open_positions") or []),
    }
    Path(ns.out).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
