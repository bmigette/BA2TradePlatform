"""Build OUR split table over the RAW Alpaca daily store (see ba2_providers.ohlcv.alpaca_history.split_table).

Every candidate split (Alpaca corporate actions + FMP's split calendar) is judged by the raw price jump across it;
the verdicts and every review row are written next to the table.  Task "apply the table" (the cache build) reads
``<out>/_meta/split_table.json`` only.

Outputs (under --out, default the AlpacaHistoryOHLCVProvider cache folder)
  _meta/split_table.json    {"asof", "inputs_sha256", "overrides_sha256", "splits": {symbol: [[date, factor], ...]}}
  _meta/split_review.csv    one row per event/jump a human should look at (applied or not)
  _meta/split_vs_fmp.csv    our table against FMP's list, one row per event, with a class
  _meta/split_accepted.csv  every accepted event with its source and verdict (audit)
  _meta/fmp_split_calendar/ the raw FMP calendar responses, one file per window (reruns are offline)

Refuses (exit 2) while an entry of split_overrides.json is pending review.  The FMP key is the AppSetting
FMP_API_KEY of --db (read-only), never printed.

Example
  python tools/build_alpaca_split_table.py
  python tools/build_alpaca_split_table.py --symbols AAPL,NVDA,TSLA --fmp-to 2026-10-13
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from ba2_providers.ohlcv.alpaca_history import split_table as st

DEFAULT_STORE = os.path.expanduser("~/Documents/ba2/alpaca_store_daily2016")
DEFAULT_OUT = os.path.expanduser("~/Documents/ba2/common/cache/AlpacaHistoryOHLCVProvider")
DEFAULT_DB = os.path.expanduser("~/Documents/ba2/trade/db.sqlite")
FMP_URL = "https://financialmodelingprep.com/api/v3/stock_split_calendar"
FMP_WINDOW_DAYS = 30            # the endpoint clamps a range to its last ~90 days: stay far inside it
FMP_ROW_CAP = 1000              # a window returning this many rows may be truncated: split it (1 day is the floor)


def atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def read_fmp_key(db: str) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("select value_str from appsetting where key = 'FMP_API_KEY'").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise SystemExit(f"no FMP_API_KEY in {db}")
    return row[0]


def fetch_fmp_calendar(start: date, end: date, cache_dir: str,
                       get: Callable[[date, date], List[Dict[str, Any]]],
                       stats: Counter) -> List[Dict[str, Any]]:
    """Every row of FMP's split calendar over [start, end], in windows of FMP_WINDOW_DAYS; a window that returns
    FMP_ROW_CAP rows or more may be truncated and is split in halves (down to one day, where it is refused).  The
    raw response of each window is cached in ``cache_dir`` and read back on a rerun."""
    rows: List[Dict[str, Any]] = []

    def window(a: date, b: date) -> None:
        path = os.path.join(cache_dir, f"{a.isoformat()}_{b.isoformat()}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
            stats["fmp_windows_cached"] += 1
        else:
            got = get(a, b)
            atomic_write(path, json.dumps(got).encode("utf-8"))
            stats["fmp_windows_fetched"] += 1
        # measured: the endpoint's `to` can spill ONE day past the window (a 2024-06-17 row for ..06-16); that row
        # belongs to the next window, which asks for it itself.  Anything further out is a change we refuse.
        lo, hi = (a - timedelta(days=1)).isoformat(), (b + timedelta(days=1)).isoformat()
        far = [r for r in got if not (lo <= r["date"][:10] <= hi)]
        if far:
            raise SystemExit(f"FMP split calendar {a}..{b} returned {len(far)} row(s) outside the window "
                             f"(first: {far[0]['date']}): the endpoint's range handling changed")
        spill = [r for r in got if not (a.isoformat() <= r["date"][:10] <= b.isoformat())]
        stats["fmp_rows_spilled_to_next_window_dropped"] += len(spill)
        got_in = [r for r in got if a.isoformat() <= r["date"][:10] <= b.isoformat()]
        if len(got) >= FMP_ROW_CAP:
            if a == b:
                raise SystemExit(f"FMP split calendar for the single day {a} returned {len(got)} rows (>= {FMP_ROW_CAP}): "
                                 f"cannot rule out truncation")
            mid = a + (b - a) // 2
            stats["fmp_windows_split"] += 1
            window(a, mid)
            window(mid + timedelta(days=1), b)
            return
        rows.extend(got_in)

    a = start
    while a <= end:
        b = min(a + timedelta(days=FMP_WINDOW_DAYS - 1), end)
        window(a, b)
        a = b + timedelta(days=1)
    return rows


def make_fmp_getter(key: str) -> Callable[[date, date], List[Dict[str, Any]]]:
    from ba2_providers.fmp_common import fmp_http_get

    def get(a: date, b: date) -> List[Dict[str, Any]]:
        r = fmp_http_get(FMP_URL, params={"from": a.isoformat(), "to": b.isoformat(), "apikey": key},
                         endpoint="stock_split_calendar", timeout=120)
        j = r.json()
        if not isinstance(j, list):
            raise SystemExit(f"FMP split calendar {a}..{b}: unexpected payload {str(j)[:200]!r}")
        return j
    return get


def load_store_bars(store: str, symbols: Optional[List[str]]) -> Tuple[Dict[str, pd.DataFrame], str]:
    """symbol -> frame(t, o, c) and a sha256 over every bar file's bytes (symbol-ordered)."""
    root = os.path.join(store, "bars", "1Day")
    names = sorted(d[len("symbol="):] for d in os.listdir(root) if d.startswith("symbol="))
    if symbols is not None:
        missing = sorted(set(symbols) - set(names))
        if missing:
            raise SystemExit(f"--symbols not in the store: {missing}")
        names = sorted(symbols)

    def read(sym: str) -> Tuple[str, pd.DataFrame, str]:
        with open(os.path.join(root, f"symbol={sym}", "all.parquet"), "rb") as f:
            raw = f.read()
        df = pd.read_parquet(io.BytesIO(raw), columns=["t", "o", "c"])
        return sym, df, hashlib.sha256(raw).hexdigest()

    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(read, names))
    h = hashlib.sha256()
    for sym, _, d in res:
        h.update(f"{sym}:{d}\n".encode())
    return {sym: df for sym, df, _ in res}, h.hexdigest()


def sha256_files(paths: List[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(os.path.basename(p).encode() + b":" + hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=DEFAULT_STORE, help="raw Alpaca daily store (default: %(default)s)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="cache folder; writes <out>/_meta/ (default: %(default)s)")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite DB holding the AppSetting FMP_API_KEY (read-only)")
    ap.add_argument("--symbols", help="comma-separated subset to judge (quick run)")
    ap.add_argument("--fmp-from", default="2016-01-01")
    ap.add_argument("--fmp-to", default="2026-10-13", help="last day of the FMP calendar fetch")
    ap.add_argument("--overrides", default=st.OVERRIDES_PATH)
    args = ap.parse_args(argv)

    try:
        overrides = st.load_overrides(args.overrides)
    except st.SplitOverridesPending as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    with open(args.overrides, "rb") as f:
        overrides_sha = hashlib.sha256(f.read()).hexdigest()

    with open(os.path.join(args.store, "STORE_INFO.json"), encoding="utf-8") as f:
        info = json.load(f)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    stats: Counter = Counter()
    t0 = time.time()

    months = st.read_corporate_actions(args.store)
    renames = st.build_rename_map(months)
    alpaca = st.alpaca_events_from_actions(months, renames, stats)
    meta = os.path.join(args.out, "_meta")
    cal_dir = os.path.join(meta, "fmp_split_calendar")
    cal_rows = fetch_fmp_calendar(date.fromisoformat(args.fmp_from), date.fromisoformat(args.fmp_to), cal_dir,
                                  _lazy_getter(args.db), stats)
    fmp = st.fmp_events_from_rows(cal_rows, renames, stats)
    bars, bars_sha = load_store_bars(args.store, symbols)
    print(f"loaded {len(bars)} symbols, {len(alpaca)} Alpaca and {len(fmp)} FMP listings in {time.time() - t0:.0f}s")

    accepted, review = st.decide_splits(bars, alpaca, fmp, overrides, stats)

    cal_files = [os.path.join(cal_dir, n) for n in os.listdir(cal_dir)]
    ca_files = [os.path.join(args.store, "corporate_actions", n) for n in os.listdir(os.path.join(args.store, "corporate_actions"))]
    h = hashlib.sha256()
    for part in (bars_sha, sha256_files(ca_files), sha256_files(cal_files), f"{args.fmp_from}|{args.fmp_to}"):
        h.update(part.encode())
    table = {"asof": info["asof"], "inputs_sha256": h.hexdigest(), "overrides_sha256": overrides_sha,
             "splits": st.splits_by_symbol(accepted)}
    atomic_write(os.path.join(meta, "split_table.json"), json.dumps(table, indent=1).encode("utf-8"))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=st.REVIEW_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(review)
    atomic_write(os.path.join(meta, "split_review.csv"), buf.getvalue().encode("utf-8"))

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["symbol", "date", "factor", "source", "verdict"], lineterminator="\n")
    w.writeheader()
    w.writerows(accepted)
    atomic_write(os.path.join(meta, "split_accepted.csv"), buf.getvalue().encode("utf-8"))
    cmp_rows = st.compare_with_fmp(bars, accepted, review, alpaca, fmp)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=st.COMPARE_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(cmp_rows)
    atomic_write(os.path.join(meta, "split_vs_fmp.csv"), buf.getvalue().encode("utf-8"))
    for (src, verdict), k in sorted(Counter((a["source"], a["verdict"]) for a in accepted).items()):
        stats[f"accepted_{verdict}_from_{src}"] += k

    print(f"asof {info['asof']}  symbols judged {len(bars)}  symbols with splits {len(table['splits'])}  "
          f"accepted events {len(accepted)}  review rows {len(review)} (applied {sum(r['applied'] for r in review)})")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    print("vs FMP's calendar:")
    for cls in ("agree", "ratio_differs", "ours_only", "fmp_only_accepted", "fmp_only_rejected"):
        sel = [r for r in cmp_rows if r["class"] == cls]
        print(f"  {cls}: {len(sel)}")
        if cls != "agree":
            for r in sel[:10]:
                print("     " + "  ".join(f"{c}={r[c]}" for c in st.COMPARE_COLUMNS[:-1]))
    print(f"wrote {meta}")
    return 0


def _lazy_getter(db: str) -> Callable[[date, date], List[Dict[str, Any]]]:
    """The key is only read (and requests only built) when a window is NOT in the cache: a rerun is offline."""
    state: Dict[str, Any] = {}

    def get(a: date, b: date) -> List[Dict[str, Any]]:
        if "get" not in state:
            state["get"] = make_fmp_getter(read_fmp_key(db))
        return state["get"](a, b)
    return get


if __name__ == "__main__":
    sys.exit(main())
