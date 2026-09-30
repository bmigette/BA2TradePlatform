"""Probe: LEAPS depth in the local ThetaData option cache (read-only; 2026-09-30).

For each symbol: which expiries carry bars >= the band floor (a LEAPS expiry), and for each trading
day of 2020-01-02..2025-12-31 whether an O_LEAP entry is possible -- a CALL with a two-sided quote
at a DTE inside the band -- plus quote / iv coverage and per-strike bar density inside the band.

Usage:
    LEAPS_BAND=365,550 python tools/probe_leaps_depth.py <symbols.txt> <out.json>
    python tools/probe_leaps_depth.py --report <out.json>

Results are recorded in docs/superpowers/specs/2026-08-31-leaps-grid-design.md §1b.
"""
import glob
import json
import os
import statistics as st
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import pandas as pd
import pyarrow.parquet as pq

ROOT = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "common", "cache",
                    "ThetaDataOptionsProvider")
W0, W1 = pd.Timestamp("2020-01-02"), pd.Timestamp("2025-12-31")
BAND = tuple(int(x) for x in os.environ.get("LEAPS_BAND", "365,550").split(","))
COLS = ["option_type", "strike", "expiry", "bar_date", "volume", "bid", "ask", "iv"]


def _min_bar(f):
    md = pq.ParquetFile(f).metadata
    idx = md.schema.names.index("bar_date")
    mins = []
    for i in range(md.num_row_groups):
        stats = md.row_group(i).column(idx).statistics
        if stats is None or not stats.has_min_max:
            return None
        mins.append(stats.min)
    return min(mins) if mins else None


def scan(sym):
    d = os.path.join(ROOT, sym)
    exps = sorted(e[4:] for e in os.listdir(d) if e.startswith("exp="))
    out = {"sym": sym, "n_exp": len(exps), "last_exp": exps[-1] if exps else None,
           "leaps_exps": [], "entry_days": [], "band_rows": 0, "band_quoted": 0,
           "band_iv": 0, "band_contract_days": [], "err": None}
    try:
        for e in exps:
            ex = pd.Timestamp(e)
            if (ex - W0).days < BAND[0]:
                continue
            fs = glob.glob(os.path.join(d, f"exp={e}", "*.parquet"))
            if not fs:
                continue
            mb = _min_bar(fs[0])
            if mb is None or (ex - pd.Timestamp(mb)).days < BAND[0]:
                continue  # never quoted this far out: not a LEAPS expiry for this band
            out["leaps_exps"].append(e)
            t = pq.read_table(fs[0], columns=COLS).to_pandas()
            t["bar_date"] = pd.to_datetime(t["bar_date"])
            t["dte"] = (ex - t["bar_date"]).dt.days
            b = t[(t.dte >= BAND[0]) & (t.dte <= BAND[1]) & (t.option_type == "call")
                  & (t.bar_date >= W0) & (t.bar_date <= W1)]
            if b.empty:
                continue
            q = b[(b.bid > 0) & (b.ask > 0)]
            out["band_rows"] += len(b)
            out["band_quoted"] += len(q)
            out["band_iv"] += int((b.iv.notna() & (b.iv > 0)).sum())
            out["entry_days"].extend(sorted(set(q.bar_date.dt.strftime("%Y-%m-%d"))))
            out["band_contract_days"].extend(b.groupby("strike").bar_date.nunique().tolist())
    except Exception as e:  # noqa: BLE001 -- recorded per symbol and reported, never dropped
        out["err"] = repr(e)
    out["entry_days"] = sorted(set(out["entry_days"]))
    return out


def report(path):
    res = json.load(open(path))
    cal = set()
    for f in sorted(glob.glob(os.path.join(ROOT, "AAPL", "exp=20*-0[1-6]-*", "*.parquet"))):
        cal |= set(pd.to_datetime(pq.read_table(f, columns=["bar_date"]).to_pandas().bar_date))
    cal = [d for d in sorted(cal) if W0 <= d <= W1]
    ncal, years = len(cal), Counter(d.year for d in cal)
    errs = [r for r in res if r["err"]]
    has = [r for r in res if r["leaps_exps"]]
    cov = {r["sym"]: len(r["entry_days"]) / ncal for r in res}
    print(f"symbols {len(res)}, errors {len(errs)}, trading days {ncal}, band {BAND}")
    print(f"symbols with a LEAPS expiry: {len(has)} / {len(res)}")
    buckets = Counter(">=90%" if c >= .9 else "75-90%" if c >= .75 else "50-75%" if c >= .5
                      else "1-50%" if c > 0 else "0%" for c in cov.values())
    print("share of days with a quoted in-band call:", dict(buckets))
    for y in sorted(years):
        vals = sorted(sum(1 for d in r["entry_days"] if d.startswith(str(y))) / years[y]
                      for r in res)
        print(f"  {y}: median {st.median(vals):.0%}  p25 {vals[len(vals) // 4]:.0%}  "
              f"symbols >=50%: {sum(v >= .5 for v in vals)}")
    print("latest cached expiry:", Counter(r["last_exp"][:7] for r in res if r["last_exp"]))
    last = sorted(max(r["entry_days"]) for r in res if r["entry_days"])
    if last:
        print("latest possible entry: median", last[len(last) // 2], "max", last[-1])
    rows = sum(r["band_rows"] for r in res)
    if rows:
        print(f"in-band call rows {rows}: two-sided quote "
              f"{sum(r['band_quoted'] for r in res) / rows:.1%}, iv present "
              f"{sum(r['band_iv'] for r in res) / rows:.1%}")
    cd = [x for r in res for x in r["band_contract_days"]]
    if cd:
        print(f"per-strike quoted days in band: median {st.median(cd)}")
    print("LEAPS expiry months:", dict(sorted(Counter(
        e[5:7] for r in has for e in r["leaps_exps"]).items())))
    if errs:
        print("errors:", [(r["sym"], r["err"][:80]) for r in errs[:5]])


if __name__ == "__main__":
    if sys.argv[1] == "--report":
        report(sys.argv[2])
        sys.exit(0)
    syms = [s.strip() for s in open(sys.argv[1]) if s.strip()]
    syms = [s for s in syms if os.path.isdir(os.path.join(ROOT, s))]
    with ProcessPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(scan, syms, chunksize=4))
    json.dump(res, open(sys.argv[2], "w"))
    print("done", len(res))
