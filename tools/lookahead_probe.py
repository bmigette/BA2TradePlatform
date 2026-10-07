#!/usr/bin/env python
"""Look-ahead probe: re-run a stored backtest row (see rerun_stored_row.py; NO DB row is written)
and report, per reader, whether a DAILY OHLCV read at a decision returned a bar whose session is
not finished at that instant.

    PYTHONPATH=<worktree packages> .venv/Scripts/python.exe tools/lookahead_probe.py 1330 \
        --window 2024-03-01 2024-04-30 --out probe.json

On an INTRADAY clock (the classic 5-minute GA runs: decision 09:30 New York, fill at the next bar)
the newest daily bar any reader gets must be the PRIOR session's: ``SAME_SESSION`` and ``FUTURE``
must both be 0 for every reader. On a daily clock (``--interval 1d``) a same-session bar is the
convention (decide on D's close, fill at D+1's open) and is reported but not an error.

A read that passes the run's own clock and no end (``+bulk``) is a whole-series read whose caller
slices it per decision (DeterministicScorer caches it for the run); that caller's RESULT is
reported separately under ``DS.data.fetch_ohlcv(post-slice)`` and is the number that matters.

Also reports the price each expert's ``price_at_date`` returned against the decision bar's open
and close on the intraday series (it must equal the OPEN).

The regression test is ``testplatform/backend/tests/backtest/test_intraday_daily_knowability.py``;
this tool is the same measurement on a real stored row.
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
        if not (fn.endswith("price_source.py") or fn.endswith("lookahead_probe.py")
                or fn.endswith("backtest_context.py") or "/pandas/" in fn):
            return f"{fn.split('/')[-1]}:{f.f_code.co_name}"
        f = f.f_back
        n += 1
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backtest_id", type=int)
    ap.add_argument("--window", nargs=2, metavar=("START", "END"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", help="override the row's execution_interval (e.g. 1d)")
    ns = ap.parse_args()

    import pandas as pd
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest import price_source as PS
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.backtest.daily_engine import _BacktestProviderBundle
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest

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
          flush=True)

    clock = {"v": None}
    by = defaultdict(Counter)
    samples = {}
    orig_set = PS.AsOfPriceSource.set_clock

    def set_clock(self, as_of):
        clock["v"] = as_of
        return orig_set(self, as_of)

    PS.AsOfPriceSource.set_clock = set_clock

    def classify(last, asof):
        if last is None:
            return "empty"
        ld = pd.Timestamp(last)
        ld = ld.tz_convert("UTC").tz_localize(None) if ld.tzinfo else ld
        return ("SAME_SESSION" if ld.date() == asof.date()
                else "FUTURE" if ld.date() > asof.date() else "prior")

    orig_get = PS.MemoizedOHLCVProvider.get_ohlcv_data

    def get(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        df = orig_get(self, symbol, start_date=start_date, end_date=end_date, interval=interval, **kw)
        if interval == "1d" and clock["v"] is not None:
            asof = pd.Timestamp(clock["v"])
            asof = asof.tz_convert("UTC").tz_localize(None) if asof.tzinfo else asof
            last = None if df is None or not len(df) else df["Date"].iloc[-1]
            kind = classify(last, asof)
            if end_date is None or pd.Timestamp(end_date).tz_localize(None) > asof:
                kind += "+bulk"
            tag = _tag()
            by[tag][kind] += 1
            samples.setdefault((tag, kind), {"sym": symbol, "asof": str(asof), "last": str(last)})
        return df

    PS.MemoizedOHLCVProvider.get_ohlcv_data = get

    try:
        from ba2_experts.DeterministicScorer import data as dsd
        orig_dsf = dsd.fetch_ohlcv

        def dsf(providers, symbol, as_of, *a, **kw):
            out = orig_dsf(providers, symbol, as_of, *a, **kw)
            if as_of is not None:
                ao = pd.Timestamp(as_of)
                ao = ao.tz_convert("UTC").tz_localize(None) if ao.tzinfo else ao
                last = None if out is None or not len(out) else out["Date"].iloc[-1]
                tag = "DS.data.fetch_ohlcv(post-slice)"
                kind = classify(last, ao)
                by[tag][kind] += 1
                samples.setdefault((tag, kind), {"sym": symbol, "asof": str(ao), "last": str(last)})
            return out

        dsd.fetch_ohlcv = dsf
    except ImportError:
        pass

    # price_at_date versus the decision bar of the run's own series.
    px = Counter()
    px_samples = []
    orig_pad = _BacktestProviderBundle.price_at_date

    def pad(self, symbol, as_of):
        v = orig_pad(self, symbol, as_of)
        ps = self._price_source
        if ps.is_intraday and v is not None and as_of is not None:
            bar = ps.bar_at(symbol, as_of)
            if bar is None:
                px["no_exact_bar(forward_fill)"] += 1
            elif abs(v - bar["open"]) < 1e-9:
                px["== bar open"] += 1
            elif abs(v - bar["close"]) < 1e-9:
                px["== bar CLOSE (look-ahead)"] += 1
            else:
                px["other"] += 1
            if len(px_samples) < 5 and bar is not None:
                px_samples.append({"sym": symbol, "asof": str(as_of), "price": v,
                                   "open": bar["open"], "close": bar["close"]})
        elif v is None:
            px["None"] += 1
        return v

    _BacktestProviderBundle.price_at_date = pad

    prior = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        res = run_daily_backtest(config)
    finally:
        logging.disable(prior)

    bad = 0
    print("daily OHLCV reads by reader (SAME_SESSION / FUTURE must be 0 on an intraday clock):")
    for tag, c in sorted(by.items()):
        flagged = sum(v for k, v in c.items() if k.split("+")[0] in ("SAME_SESSION", "FUTURE")
                      and not k.endswith("+bulk"))
        bad += flagged
        print(f"  {tag:60s} total={sum(c.values())} {dict(c)}")
    for (tag, kind), r in samples.items():
        if kind.split("+")[0] in ("SAME_SESSION", "FUTURE") and not kind.endswith("+bulk"):
            print("  LEAK sample", tag, kind, r)
    print("price_at_date vs decision bar:", dict(px))
    for r in px_samples:
        print("  ", r)
    summ = {k: res.get(k) for k in ("total_trades", "total_return", "max_drawdown",
                                    "annualized_return", "win_rate", "calmar_ratio", "final_equity")}
    print(summ, "| non-bulk same-session/future daily reads:", bad)
    Path(ns.out).write_text(json.dumps({
        "row": ns.backtest_id, "window": ns.window, "interval": config.get("execution_interval"),
        "by": {t: dict(c) for t, c in by.items()}, "price_at_date": dict(px),
        "leaks": bad, "summary": summ, "trades": res.get("trades")}, indent=0, default=str))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
