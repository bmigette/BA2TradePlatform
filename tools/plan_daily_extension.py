r"""Plan a daily-bar extension (read-only): which symbols to extend, which are cold, which have a split.

Builds the symbol files for ``ba2-test fetch-cache --timeframes 1d`` from the universe(s):

    extend_all.txt     symbols that HAVE a daily file (safe to extend with --start <recent>)
    cold_no_file.txt   symbols WITHOUT a daily file: ``--start`` would create a file holding only
                       the recent bars (no warmup history) -- fetch these separately with a long start
    split_risk.txt     symbols whose LOCAL split calendar (<cache root>/fmp_history/mc_stock_split__SYM.json)
                       lists a split dated after the file's effective last bar and <= ``--end``: the
                       guarded top-up would REPLACE their whole history (rescaling old prices).
                       ``--repair-report`` supplies the symbols a truncation will cut back to the cutoff.
    extend_pass1.txt   extend_all minus split_risk: the strictly additive first pass
    plan.json / split_risk.csv   the numbers and the per-symbol split evidence

The local calendar covers only the symbols that have a file, and a file fetched before a split does not
list it, so ``split_risk`` is a LOWER BOUND. The guarantee is ``BA2_OHLCV_TOPUP_FULL_REFETCH=0`` on the
fetch-cache process: a split the list missed is then REFUSED (loudly, nothing written), never replaced.

    python tools/plan_daily_extension.py --cache-dir <cache>\FMPOHLCVProvider --universe u.txt \
        --symbols-file live.txt --senate-csv senate_universe.csv --add SPY --end 2026-10-06 \
        --cutoff 2026-09-11 --repair-report repair.json --out-dir D:\scratch\plan
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from typing import Dict, List, Optional, Set

SUFFIX = "_1d.parquet"


def read_tokens(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [t.upper() for line in f for t in line.replace(",", " ").split() if t and not t.startswith("#")]


def last_bar_of(path: str) -> Optional[dt.date]:
    import check_daily_extension as chk          # sibling tool (same folder)
    return chk.last_bar_of(path)


def local_splits(cache_root: str, symbol: str) -> Optional[List[dict]]:
    """``[{date, numerator, denominator}]`` from the local calendar file, ``None`` when there is none."""
    p = os.path.join(cache_root, "fmp_history", f"mc_stock_split__{symbol}.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        j = json.load(f)
    rows = j.get("historical") if isinstance(j, dict) else j
    return list(rows or [])


def plan(cache_dir: str, symbols: List[str], end: dt.date, cutoff: Optional[dt.date],
         truncated: Set[str], cache_root: str) -> Dict:
    extend_all, cold, risk_rows = [], [], []
    no_calendar = 0
    cal_mtimes = []
    for s in symbols:
        path = os.path.join(cache_dir, f"{s}{SUFFIX}")
        if not os.path.exists(path):
            cold.append(s)
            continue
        extend_all.append(s)
        last = last_bar_of(path)
        eff = min(last, cutoff) if (s in truncated and cutoff is not None and last is not None) else last
        cal = local_splits(cache_root, s)
        if cal is None:
            no_calendar += 1
            continue
        cal_mtimes.append(os.path.getmtime(os.path.join(cache_root, "fmp_history", f"mc_stock_split__{s}.json")))
        for ev in cal:
            d = ev.get("date", "")[:10]
            if d and eff is not None and eff < dt.date.fromisoformat(d) <= end:
                risk_rows.append({"symbol": s, "effective_last_bar": eff.isoformat(), "split_date": d,
                                  "ratio": f"{ev.get('numerator')}:{ev.get('denominator')}",
                                  "will_be_truncated": s in truncated})
    risk = sorted({r["symbol"] for r in risk_rows})
    return {"extend_all": extend_all, "cold": cold, "split_risk": risk, "risk_rows": risk_rows,
            "pass1": [s for s in extend_all if s not in set(risk)], "no_local_calendar": no_calendar,
            "calendar_files_oldest_utc": None if not cal_mtimes else dt.datetime.fromtimestamp(min(cal_mtimes), dt.timezone.utc).isoformat(),
            "calendar_files_newest_utc": None if not cal_mtimes else dt.datetime.fromtimestamp(max(cal_mtimes), dt.timezone.utc).isoformat()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", required=True, help="the provider folder holding <SYM>_1d.parquet")
    ap.add_argument("--cache-root", help="default: the parent of --cache-dir")
    ap.add_argument("--universe", action="append", default=[], help="symbol file (repeatable)")
    ap.add_argument("--symbols-file", action="append", default=[], help="extra symbol file (repeatable)")
    ap.add_argument("--senate-csv", help="comma-separated symbol file")
    ap.add_argument("--add", nargs="*", default=[], help="extra symbols, e.g. SPY")
    ap.add_argument("--end", required=True, help="the extension's --end (the last FINAL session), YYYY-MM-DD")
    ap.add_argument("--cutoff", help="the repair cutoff (with --repair-report)")
    ap.add_argument("--repair-report", help="JSON of tools/repair_partial_daily_bars.py")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)
    cache_dir = os.path.abspath(args.cache_dir)
    cache_root = os.path.abspath(args.cache_root) if args.cache_root else os.path.dirname(cache_dir)
    out_dir = os.path.abspath(args.out_dir)
    rr = os.path.normcase(os.path.realpath(cache_root))
    if os.path.normcase(os.path.realpath(out_dir)).startswith(rr):
        print(f"REFUSED: --out-dir {out_dir} is inside the cache root", file=sys.stderr)
        return 2
    symbols: Set[str] = {s.upper() for s in args.add}
    for f in args.universe + args.symbols_file + ([args.senate_csv] if args.senate_csv else []):
        symbols.update(read_tokens(f))
    truncated: Set[str] = set()
    if args.repair_report:
        rep = json.load(open(args.repair_report, encoding="utf-8"))
        truncated = set(rep["truncated_symbols"] or rep["truncated_symbols_planned"])
    res = plan(cache_dir, sorted(symbols), dt.date.fromisoformat(args.end),
               dt.date.fromisoformat(args.cutoff) if args.cutoff else None, truncated, cache_root)
    os.makedirs(out_dir, exist_ok=True)
    for name, key in (("extend_all.txt", "extend_all"), ("cold_no_file.txt", "cold"), ("split_risk.txt", "split_risk"),
                      ("extend_pass1.txt", "pass1")):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write("\n".join(res[key]) + ("\n" if res[key] else ""))
    with open(os.path.join(out_dir, "split_risk.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "effective_last_bar", "split_date", "ratio", "will_be_truncated"])
        w.writeheader()
        w.writerows(res["risk_rows"])
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in res.items() if k != "risk_rows"}
    summary.update({"symbols_total": len(symbols), "end": args.end, "cutoff": args.cutoff})
    with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
