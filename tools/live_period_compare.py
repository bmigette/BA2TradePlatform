#!/usr/bin/env python
"""Row 1088 (FMPEarningsDrift mid-cap, prod/dev instance 11) re-run with the look-ahead fix over the LIVE
period, compared trade by trade with what the prod and dev instances actually did.

    # 1. is the data there? (refuses, exit 2, listing every gap; changes nothing)
    python tools/live_period_compare.py check --window-end 2026-10-06
    # 2. run row 1088's stored config in memory (no DB row) at the live time 09:30 and at 10:00
    python tools/live_period_compare.py run --window-end 2026-10-06 --out-dir <dir>
    # 3. compare with the live tracks (read-only sqlite)
    python tools/live_period_compare.py compare --out-dir <dir>

``--window-end`` is the LAST FINAL SESSION the data covers. Nothing is guessed: a screener store whose
newest scan is older than the window, a symbol whose daily/5-minute bars stop before it, an earnings
history file written before it, or a calendar that does not span it REFUSES the run (the readers have no
staleness guard of their own: a September window would silently trade June's universe, a symbol with
no 5-minute bars is skipped, and a missing fmp_history file disables that symbol).

DATA THE RUN NEEDS (verified by ``check``):
  * screener metric store (CACHE/screener/metric_store): newest scan date >= window_end - 8 days (weekly
    scans); the store is what picks the daily candidate universe.
  * FMP OHLCV parquet cache (CACHE/FMPOHLCVProvider): for EVERY symbol of row 1088's universe (627 at
    the time of writing) the 1d file ends >= window_end and the 5min file ends >= window_end.
  * fmp_history earnings (CACHE/fmp_history/past_earnings_quarterly__<SYM>.json, rows carry the report
    ``time`` bmo/amc): file written (mtime) on/after window_end's date and newest row dated within a
    quarter of window_end. Hermetic runs ignore file age, so an old file would silently serve old data.
  * the NYSE market calendar spans the window (``regular_session_dates``).
  * 5-minute bars carry NO pre-market (verified): the decision price at 09:30 is the PRIOR session's last
    bar (the run logs the first-bar warning); at 10:00 it is the 09:55-ended bar.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))

ROW = 1088
WINDOW_START = "2026-09-08"
PROD_DB = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"
DEV_DB = r"C:\Users\basti\Documents\ba2\trade\db.sqlite"
OHLCV_CACHE = Path(os.path.expanduser(r"~\Documents\ba2\common\cache\FMPOHLCVProvider"))
FMP_HISTORY = Path(os.path.expanduser(r"~\Documents\ba2\common\cache\fmp_history"))
EARNINGS_NAMESPACE = "past_earnings_quarterly"
TIMES = ("09:30", "10:00")


def _entry():
    from ba2test_launcher import _enter_backend
    _enter_backend()


def _config(window):
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == ROW).first()
        return rebuild_config_for_backtest(bt, db, window=window)
    finally:
        db.close()


# ----------------------------------------------------------------------------- check
def check(window_end: str) -> list:
    """Every reason the run must NOT start. Empty list = the data is present."""
    import pandas as pd

    _entry()
    problems = []
    end = pd.Timestamp(window_end)
    cfg = _config((WINDOW_START, window_end))
    syms = list(cfg["enabled_instruments"])
    store = (cfg.get("screener_runtime") or {}).get("store")

    # screener store
    try:
        from ba2_providers.screener import metric_store as ms
        df = ms.load_store(store)
        newest = pd.Timestamp(ms.scan_dates(df, store_key=store)[-1])
        if newest < end - pd.Timedelta(days=8):
            problems.append(f"screener store {store}: newest scan {newest.date()} is older than "
                            f"window_end {end.date()} - 8d (the run would trade a stale universe)")
    except Exception as e:  # noqa: BLE001
        problems.append(f"screener store {store} unreadable: {type(e).__name__}: {e}")

    # OHLCV bars
    short = {"1d": [], "5min": []}
    for s in syms:
        for iv in ("1d", "5min"):
            p = OHLCV_CACHE / f"{s}_{iv}.parquet"
            if not p.exists():
                short[iv].append((s, "missing"))
                continue
            last = pd.read_parquet(p, columns=["Date"]).Date.max()
            if pd.Timestamp(last).normalize() < end:
                short[iv].append((s, str(pd.Timestamp(last).date())))
    for iv, lst in short.items():
        if lst:
            problems.append(f"{iv} bars end before {end.date()} (or are missing) for {len(lst)}/{len(syms)} "
                            f"symbols, e.g. {lst[:8]}")

    # earnings history (report time bmo/amc rows)
    stale_files = []
    for s in syms:
        p = FMP_HISTORY / f"{EARNINGS_NAMESPACE}__{s}.json"
        if not p.exists():
            stale_files.append((s, "missing"))
            continue
        if datetime.fromtimestamp(p.stat().st_mtime).date() < end.date():
            stale_files.append((s, "written " + datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()))
    if stale_files:
        problems.append(f"fmp_history {EARNINGS_NAMESPACE}: {len(stale_files)}/{len(syms)} symbols have a "
                        f"missing file or one written before {end.date()}, e.g. {stale_files[:8]}")

    # market calendar
    try:
        from ba2_common.core.market_calendar import regular_session_close_utc, regular_session_dates
        days = regular_session_dates(date.fromisoformat(WINDOW_START), end.date())
        if not days:
            problems.append("market calendar has no sessions in the window")
        else:
            regular_session_close_utc(days[-1])
    except Exception as e:  # noqa: BLE001
        problems.append(f"market calendar does not span the window: {type(e).__name__}: {e}")
    return problems


# ----------------------------------------------------------------------------- run
def run(window_end: str, out_dir: Path, times=TIMES):
    problems = check(window_end)
    if problems:
        print("REFUSING TO RUN -- the data is not there:")
        for p in problems:
            print("  -", p)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    from app.services.backtest import daily_backtest_handler as H
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import AssetClass

    entries = {}
    orig_fill = BacktestAccount._apply_fill

    def cap(self, order, fill_px, as_of):
        try:
            if (order.depends_on_order is None and getattr(order, "asset_class", None) != AssetClass.OPTION
                    and order.transaction_id):
                tx = get_instance(Transaction, order.transaction_id)
                entries[order.transaction_id] = {
                    "fill_px": float(fill_px), "sl": tx.stop_loss, "tp": tx.take_profit,
                    "qty": float(order.quantity), "decision": str(as_of),
                    "dec_price": self._price.decision_price(order.symbol, as_of)}
        except Exception as e:  # noqa: BLE001 - never perturb the run
            entries[-1] = {"err": repr(e)}
        return orig_fill(self, order, fill_px, as_of)

    BacktestAccount._apply_fill = cap
    for t in times:
        cfg = _config((WINDOW_START, window_end))
        sched = dict(cfg["run_schedule_override"])
        sched["times"] = [t]
        cfg["run_schedule_override"] = sched
        cfg["backtest_id"] = 900_000_000 + ROW
        cfg["name"] = f"LIVEPERIOD-{t}"
        cfg["persist_trading_db"] = False
        entries.clear()
        prior = logging.root.manager.disable
        logging.disable(logging.INFO)
        try:
            res = H.run_daily_backtest(cfg)
        finally:
            logging.disable(prior)
        trades = res.get("trades") or []
        (out_dir / f"bt_{t.replace(':', '')}.json").write_text(json.dumps(
            {"time": t, "window": [WINDOW_START, window_end], "trades": trades,
             "entries": {str(k): v for k, v in entries.items()}}, default=str, indent=0))
        print(f"{t}: {len(trades)} trades, return {res.get('total_return')}")
    return 0


# ----------------------------------------------------------------------------- compare
def _live(db_path, expert_id=11):
    import pandas as pd
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tx = pd.read_sql('select id,symbol,quantity,open_price,close_price,stop_loss,take_profit,open_date,'
                     'close_date,status,close_reason from "transaction" where expert_id=? and open_date>=?',
                     c, params=(expert_id, WINDOW_START))
    rec = pd.read_sql("select id,symbol,recommended_action a,price_at_date,created_at,subtype,market_analysis_id "
                      "from expertrecommendation where instance_id=? and created_at>=?", c,
                      params=(expert_id, WINDOW_START))
    orders = pd.read_sql("select o.id,o.symbol,o.status,o.comment,o.expert_recommendation_id rid,o.created_at "
                         "from tradingorder o join expertrecommendation r on r.id=o.expert_recommendation_id "
                         "where r.instance_id=? and r.created_at>=?", c, params=(expert_id, WINDOW_START))
    for df, col in ((tx, "open_date"), (rec, "created_at"), (orders, "created_at")):
        df["D"] = df[col].astype(str).str[:10]
    return tx, rec, orders


def _classify_unmatched_live(row, rec, orders):
    """Why live opened (or did not open) a symbol on a day."""
    r = rec[(rec.symbol == row.symbol) & (rec.D == row.D) & (rec.a == "BUY")]
    if r.empty:
        return "no BUY recommendation that day"
    o = orders[orders.rid.isin(r.id)]
    if o.empty:
        return "BUY recommendation but NO order: skipped for lack of cash / by the RM (not opened)"
    return "order " + "/".join(sorted(set(o.status)))


def compare(out_dir: Path):
    import pandas as pd
    pd.set_option("display.width", 260)
    for name, path in (("PROD instance 11", PROD_DB), ("DEV instance 11", DEV_DB)):
        tx, rec, orders = _live(path)
        print(f"\n{'=' * 24} {name}: {len(tx)} transactions, {len(rec)} recommendations since {WINDOW_START}")
        buys = rec[rec.a == "BUY"]
        print(f"BUY recommendations {len(buys)}, with an order {buys.id.isin(orders.rid).sum()}")
        for t in TIMES:
            f = out_dir / f"bt_{t.replace(':', '')}.json"
            if not f.exists():
                print(f"(no backtest output for {t}: run the 'run' phase)")
                continue
            bt = json.loads(f.read_text())
            trades = pd.DataFrame(bt["trades"])
            if trades.empty:
                print(f"{t}: the backtest opened nothing")
                continue
            trades["D"] = trades.entry_time.astype(str).str[:10]
            ent = bt["entries"]
            trades["tp"] = [ent.get(str(i), {}).get("tp") for i in trades.transaction_id]
            trades["sl"] = [ent.get(str(i), {}).get("sl") for i in trades.transaction_id]
            m = trades.merge(tx, on=["symbol"], how="outer", suffixes=("_bt", "_live"), indicator=True)
            m = m[(m.D_bt == m.D_live) | (m._merge != "both")]
            print(f"\n--- backtest at {t} vs {name}")
            both = m[(m._merge == "both")]
            print(f"matched (same symbol and entry day): {len(both)} of bt {len(trades)} / live {len(tx)}")
            if len(both):
                cols = ["symbol", "D_bt", "entry_price", "open_price", "tp", "take_profit", "sl", "stop_loss",
                        "exit_time", "close_date", "exit_reason", "close_reason", "exit_price", "close_price"]
                print(both[[c for c in cols if c in both.columns]].to_string(index=False))
            bt_only = trades[~trades.set_index(["symbol", "D"]).index.isin(tx.set_index(["symbol", "D"]).index)]
            print(f"backtest-only: {len(bt_only)}")
            for _, r in bt_only.iterrows():
                print(f"   {r.symbol} {r.D}: live: {_classify_unmatched_live(r, rec, orders)}")
            live_only = tx[~tx.set_index(["symbol", "D"]).index.isin(trades.set_index(["symbol", "D"]).index)]
            print(f"live-only: {len(live_only)}")
            for _, r in live_only.iterrows():
                print(f"   {r.symbol} {r.D}: the backtest produced no entry that day")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("check", "run", "compare"))
    ap.add_argument("--window-end", help="last FINAL session the data covers (YYYY-MM-DD)")
    ap.add_argument("--out-dir", default="live_period_out")
    ns = ap.parse_args()
    if ns.phase == "compare":
        return compare(Path(ns.out_dir))
    if not ns.window_end:
        ap.error("--window-end is required")
    if ns.phase == "check":
        problems = check(ns.window_end)
        for p in problems:
            print("MISSING:", p)
        print("DATA PRESENT" if not problems else f"{len(problems)} problem(s): do NOT run")
        return 2 if problems else 0
    return run(ns.window_end, Path(ns.out_dir))


if __name__ == "__main__":
    sys.exit(main())
