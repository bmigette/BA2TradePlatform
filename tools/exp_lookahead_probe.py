#!/usr/bin/env python
"""EXPERIMENT (exp/verify-lookahead-and-entry-bar): re-run a stored row (see rerun_stored_row.py), writing
NO DB row, while instrumenting (read-only wrappers):

  * every daily (interval "1d") OHLCV read through MemoizedOHLCVProvider: per caller, is the newest
    bar returned the decision day's own session (SAME_SESSION) or an earlier one (prior)?
  * MetricStoreATRProvider reads (date of the metric row used vs the decision day)
  * every EQUITY entry fill: the transaction's SL/TP at fill time and the ENTRY BAR (the bar the
    market order fills at the open of) so entry-bar-vs-stop can be compared with the realised exit.

    tools/exp_lookahead_probe.py 1330 --window 2024-03-01 2024-04-30 --out probe.json
    BA2_EXP_PRIOR_CLOSE=1 tools/exp_lookahead_probe.py 1107 --out on.json      # the clamp switch
"""
import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))

from ba2test_launcher import _enter_backend  # noqa: E402

_enter_backend()


def _tag():
    f = sys._getframe(2)
    n = 0
    while f is not None and n < 40:
        fn = f.f_code.co_filename.replace("\\", "/")
        if not (fn.endswith("price_source.py") or fn.endswith("exp_lookahead_probe.py")
                or fn.endswith("backtest_context.py") or "/pandas/" in fn):
            parts = fn.split("/")
            return f"{parts[-1]}:{f.f_code.co_name}"
        f = f.f_back
        n += 1
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backtest_id", type=int)
    ap.add_argument("--window", nargs=2, metavar=("START", "END"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", help="override the row's execution_interval (e.g. 1d) -- experiment only")
    ap.add_argument("--show", type=int, default=0, help="print the first N entry fills")
    ns = ap.parse_args()

    import os
    import pandas as pd
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest import price_source as PS
    from app.services.backtest import seam_wiring as SW
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import AssetClass

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == ns.backtest_id).first()
        config = rebuild_config_for_backtest(bt, db, window=ns.window)
        name = bt.name
    finally:
        db.close()
    config["backtest_id"] = 900_000_000 + ns.backtest_id
    config["name"] = f"LA-{name}"[:255]
    config["persist_trading_db"] = False
    if ns.interval:
        config["execution_interval"] = ns.interval
    print("row", ns.backtest_id, name, "| execution_interval:", config.get("execution_interval"),
          "| BA2_EXP_PRIOR_CLOSE =", os.environ.get("BA2_EXP_PRIOR_CLOSE"), flush=True)

    clock = {"v": None}
    by = defaultdict(Counter)
    samples = {}
    orig_set = PS.AsOfPriceSource.set_clock

    def set_clock(self, as_of):
        clock["v"] = as_of
        return orig_set(self, as_of)

    PS.AsOfPriceSource.set_clock = set_clock

    orig_get = PS.MemoizedOHLCVProvider.get_ohlcv_data

    def get(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        df = orig_get(self, symbol, start_date=start_date, end_date=end_date, interval=interval, **kw)
        if interval == "1d" and clock["v"] is not None:
            asof = pd.Timestamp(clock["v"])
            asof = asof.tz_convert("UTC").tz_localize(None) if asof.tzinfo else asof
            if df is None or not len(df):
                kind = "empty"
                ld = None
            else:
                ld = pd.Timestamp(df["Date"].iloc[-1])
                ld = ld.tz_convert("UTC").tz_localize(None) if ld.tzinfo else ld
                kind = ("SAME_SESSION" if ld.date() == asof.date()
                        else "FUTURE" if ld.date() > asof.date() else "prior")
            if end_date is None:
                kind += "+end_None"
            tag = _tag()
            by[tag][kind] += 1
            if (tag, kind) not in samples:
                samples[(tag, kind)] = {"sym": symbol, "asof": str(asof), "end": str(end_date),
                                        "n": 0 if df is None else len(df), "last_date": str(ld),
                                        "last_close": None if df is None or not len(df) else float(df["Close"].iloc[-1])}
        return df

    PS.MemoizedOHLCVProvider.get_ohlcv_data = get

    try:
        from ba2_experts.DeterministicScorer import data as dsd
        orig_dsf = dsd.fetch_ohlcv

        def dsf(providers, symbol, as_of, *a, **kw):
            out = orig_dsf(providers, symbol, as_of, *a, **kw)
            if as_of is not None and PS.exp_prior_close_clock() is not None:
                # SWITCH ON: DS slices its own cached full series to <= as_of; re-slice to the bars
                # strictly before the decision session (the only DS-side read of daily bars).
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                _a = pd.Timestamp(as_of)
                _a = _a.tz_convert("UTC").tz_localize(None) if _a.tzinfo else _a
                _cut = pd.Timestamp(_dt(_a.year, _a.month, _a.day)) - pd.Timedelta(microseconds=1)
                if out is not None and len(out):
                    _d = pd.to_datetime(out["Date"])
                    _d = _d.dt.tz_convert("UTC").dt.tz_localize(None) if _d.dt.tz is not None else _d
                    out = out[_d <= _cut].reset_index(drop=True)
                    if not len(out):
                        out = None
            if as_of is not None:
                ao = pd.Timestamp(as_of)
                ao = ao.tz_convert("UTC").tz_localize(None) if ao.tzinfo else ao
                if out is None or not len(out):
                    kind = "empty"
                else:
                    ld = pd.Timestamp(out["Date"].iloc[-1])
                    ld = ld.tz_convert("UTC").tz_localize(None) if ld.tzinfo else ld
                    kind = ("SAME_SESSION" if ld.date() == ao.date()
                            else "FUTURE" if ld.date() > ao.date() else "prior")
                tag = "DS.data.fetch_ohlcv(post-slice)"
                by[tag][kind] += 1
                if (tag, kind) not in samples and out is not None and len(out):
                    samples[(tag, kind)] = {"sym": symbol, "asof": str(ao), "n": len(out),
                                            "last_date": str(ld), "last_close": float(out["Close"].iloc[-1])}
            return out

        dsd.fetch_ohlcv = dsf
    except ImportError:
        pass

    atr = Counter()
    atr_sample = {}
    orig_atr = SW.MetricStoreATRProvider.get_indicator

    def get_atr(self, symbol, indicator, start_date=None, end_date=None, **kw):
        r = orig_atr(self, symbol, indicator, start_date=start_date, end_date=end_date, **kw)
        if clock["v"] is not None:
            atr["calls"] += 1
        return r

    SW.MetricStoreATRProvider.get_indicator = get_atr

    entries = []
    orig_fill = BacktestAccount._apply_fill

    def apply_fill(self, order, fill_px, as_of):
        try:
            if (order.depends_on_order is None and getattr(order, "asset_class", None) != AssetClass.OPTION
                    and order.transaction_id):
                txn = get_instance(Transaction, order.transaction_id)
                bar = self._price.next_bar(order.symbol, as_of)
                entries.append({
                    "txn": order.transaction_id, "sym": order.symbol, "side": str(order.side),
                    "decision": str(as_of), "fill_px": float(fill_px),
                    "sl": txn.stop_loss, "tp": txn.take_profit,
                    "bar": None if bar is None else {k: (float(v) if isinstance(v, (int, float)) else str(v))
                                                      for k, v in bar.items()}})
        except Exception as e:  # noqa: BLE001 - never perturb the run
            entries.append({"err": repr(e)})
        return orig_fill(self, order, fill_px, as_of)

    BacktestAccount._apply_fill = apply_fill

    prior = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        res = run_daily_backtest(config)
    finally:
        logging.disable(prior)

    print("daily-read calls by reader:")
    for tag, c in sorted(by.items()):
        print(f"  {tag:60s} total={sum(c.values())} {dict(c)}")
    for k, r in samples.items():
        print("  sample", k, r)
    print("MetricStoreATR calls:", atr["calls"])
    trades = res.get("trades") or []
    summ = {k: res.get(k) for k in ("total_trades", "total_return", "max_drawdown", "annualized_return",
                                    "win_rate", "calmar_ratio", "final_equity")}
    print(summ, "| trades", len(trades), "| entry fills recorded", len(entries))
    for e in entries[:ns.show]:
        print("  ENTRY", e)
    for t in trades[:ns.show]:
        print("  TRADE", {k: t[k] for k in ("symbol", "entry_time", "exit_time", "entry_price", "exit_price", "exit_reason")})
    Path(ns.out).write_text(json.dumps({
        "row": ns.backtest_id, "window": ns.window, "switch": os.environ.get("BA2_EXP_PRIOR_CLOSE"),
        "by": {t: dict(c) for t, c in by.items()},
        "samples": {f"{a}|{b}": v for (a, b), v in samples.items()},
        "atr_calls": atr["calls"], "summary": summ, "trades": trades, "entries": entries}, indent=0, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
