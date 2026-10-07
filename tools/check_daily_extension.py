r"""Completion check of a daily OHLCV extension (read-only): is every universe symbol on the last FINAL session?

For each symbol of ``--symbols-file`` reads ``<cache-dir>/<SYM>_1d.parquet`` (the Date column's footer
statistics only) and requires ``last bar >= the last final session``, OR the symbol on an explicit
exception list WITH a reason (the reason must be established by asking the vendor, not assumed). The last
final session is derived from the NYSE calendar and the finality rule (``ba2_common.core.ohlcv_final_bars``:
session close + 4 h) at ``--now`` (default: the clock), or given with ``--last-session``.

Also fails when a file holds a bar dated AFTER the last final session (an unfinished or future bar that
a pre-fix writer left), unless ``--tolerate-unfinished-today`` (an old-code app that re-contaminated a
file after the repair: reported, not fatal, repaired by re-running tools/repair_partial_daily_bars.py).
``--repair-report`` (the JSON of tools/repair_partial_daily_bars.py) additionally requires every truncated
symbol to have been re-extended.

Exit code 0 = PASS (unreached == explained), 1 = FAIL, 2 = bad arguments. ``--report-json`` writes the result.

    python tools/check_daily_extension.py --cache-dir <cache>\FMPOHLCVProvider --symbols-file universe.txt \
        --exceptions CTBI:delisted OIMAW:delisted --repair-report repair.json --report-json check.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Dict, List, Optional

#: symbols known (2026-10-07 inventory) to have no newer vendor bar than their cached last bar
KNOWN_NON_EXTENDABLE = {
    "CTBI": "no vendor bar after 2025-12-31 (last cached bar)",
    "OIMAW": "last bar 2026-03-24 (warrant, ended)",
    "BSP": "ended long ago",
    "ITG": "ended long ago",
    "OSPRU": "ended long ago",
    "PTAC": "ended long ago",
}
SUFFIX = "_1d.parquet"


def last_final_session(now: Optional[dt.datetime] = None) -> dt.date:
    """The newest NYSE session whose bar is final at ``now`` (tz-aware UTC)."""
    from ba2_common.core import ohlcv_final_bars as fb
    from ba2_common.core.market_calendar import is_regular_session
    now = now or fb.now_utc()
    day = now.astimezone(dt.timezone.utc).date() + dt.timedelta(days=1)
    for _ in range(15):
        day -= dt.timedelta(days=1)
        if is_regular_session(day) and fb.session_is_final("AAPL", day, now):
            return day
    raise RuntimeError(f"no final NYSE session found in the 15 days before {now}")


def last_bar_of(path: str) -> Optional[dt.date]:
    """Newest bar date from the Date column's parquet statistics (a full column read as a fallback)."""
    import pandas as pd
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    names = [pf.schema_arrow.names[i] for i in range(len(pf.schema_arrow.names))]
    if "Date" not in names:
        raise ValueError(f"{path} has no Date column")
    best = None
    ok = True
    for g in range(pf.metadata.num_row_groups):
        rg = pf.metadata.row_group(g)
        col = next(rg.column(i) for i in range(rg.num_columns) if rg.column(i).path_in_schema == "Date")
        st = col.statistics
        if st is None or not st.has_min_max:
            ok = False
            break
        best = st.max if best is None or st.max > best else best
    if ok and best is not None:
        ts = pd.Timestamp(best)
        return (ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo is not None else ts).date()
    s = pd.to_datetime(pq.read_table(path, columns=["Date"]).column("Date").to_pandas())
    if s.empty:
        return None
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert("UTC").dt.tz_localize(None)
    return s.max().date()


def check(cache_dir: str, symbols: List[str], last_session: dt.date, exceptions: Dict[str, str],
          truncated: Optional[List[str]] = None, tolerate_unfinished_today: bool = False) -> Dict:
    rows = []
    unreached, explained, beyond, missing = [], [], [], []
    for s in symbols:
        path = os.path.join(cache_dir, f"{s}{SUFFIX}")
        if not os.path.exists(path):
            last = None
        else:
            last = last_bar_of(path)
        row = {"symbol": s, "last_bar": None if last is None else last.isoformat()}
        if last is None:
            row["status"] = "no_file" if not os.path.exists(path) else "empty_file"
            missing.append(s)
        elif last > last_session:
            row["status"] = "bar_after_last_final_session"
            beyond.append(s)
        elif last >= last_session:
            row["status"] = "ok"
        else:
            row["status"] = "stale"
        if row["status"] in ("no_file", "empty_file", "stale"):
            if s in exceptions:
                row["status"], row["reason"] = "explained", exceptions[s]
                explained.append(s)
            else:
                unreached.append(s)
        rows.append(row)
    not_reextended = [s for s in (truncated or []) if s in unreached]
    fatal_beyond = [] if tolerate_unfinished_today else beyond
    passed = not unreached and not fatal_beyond
    return {
        "last_final_session": last_session.isoformat(), "symbols": len(symbols), "ok": sum(1 for r in rows if r["status"] == "ok"),
        "unreached": unreached, "explained": explained, "bars_after_last_final_session": beyond,
        "tolerated_unfinished_today": bool(beyond) and tolerate_unfinished_today,
        "truncated_not_reextended": not_reextended, "passed": passed, "rows": rows,
    }


def parse_exceptions(items: List[str], path: Optional[str]) -> Dict[str, str]:
    out = dict()
    if path:
        for line in open(path, "r", encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                sym, _, why = line.partition(",") if "," in line else line.partition(":")
                out[sym.strip().upper()] = why.strip()
    for it in items:
        sym, _, why = it.partition(":")
        out[sym.strip().upper()] = why.strip()
    bad = [s for s, w in out.items() if not w]
    if bad:
        raise ValueError(f"exceptions need a reason (SYMBOL:reason): {bad}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--symbols-file", required=True, help="one symbol per line")
    ap.add_argument("--last-session", help="YYYY-MM-DD (default: derived from the calendar and the finality rule)")
    ap.add_argument("--now", help="ISO UTC instant for the derivation (default: the clock)")
    ap.add_argument("--exceptions", nargs="*", default=[], help="SYMBOL:reason ...")
    ap.add_argument("--exceptions-file", help="lines 'SYMBOL,reason'")
    ap.add_argument("--no-known-exceptions", action="store_true", help="do not pre-load the known dead symbols")
    ap.add_argument("--repair-report", help="JSON of tools/repair_partial_daily_bars.py")
    ap.add_argument("--tolerate-unfinished-today", action="store_true")
    ap.add_argument("--report-json")
    args = ap.parse_args(argv)
    try:
        exceptions = {} if args.no_known_exceptions else dict(KNOWN_NON_EXTENDABLE)
        exceptions.update(parse_exceptions(args.exceptions, args.exceptions_file))
        now = dt.datetime.fromisoformat(args.now).replace(tzinfo=dt.timezone.utc) if args.now else None
        session = dt.date.fromisoformat(args.last_session) if args.last_session else last_final_session(now)
        symbols = sorted({t.upper() for line in open(args.symbols_file, encoding="utf-8") for t in line.split()})
        truncated = None
        if args.repair_report:
            rep = json.load(open(args.repair_report, encoding="utf-8"))
            truncated = rep["truncated_symbols"] or rep["truncated_symbols_planned"]
    except (ValueError, OSError, KeyError) as e:
        print(f"bad arguments: {e}", file=sys.stderr)
        return 2
    res = check(os.path.abspath(args.cache_dir), symbols, session, exceptions, truncated, args.tolerate_unfinished_today)
    print(f"last final session {res['last_final_session']}: {res['ok']}/{res['symbols']} ok, {len(res['explained'])} explained, "
          f"{len(res['unreached'])} UNREACHED, {len(res['bars_after_last_final_session'])} with a bar after it"
          + (f", {len(res['truncated_not_reextended'])} truncated symbols not re-extended" if truncated is not None else ""))
    for s in res["unreached"][:50]:
        print(f"  UNREACHED {s}")
    for s in res["bars_after_last_final_session"][:50]:
        print(f"  AFTER-SESSION BAR {s}")
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    print("PASS" if res["passed"] else "FAIL")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
