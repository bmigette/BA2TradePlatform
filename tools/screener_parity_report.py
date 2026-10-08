"""Read-only parity report: the backtest's screener simulation against what the LIVE screener really picked.

    python tools/screener_parity_report.py --db <prod db.sqlite> --panel <daily_panel dir> \
        --instances 8,11 [--from 2026-09-01] [--to 2026-10-08] [--behaviour legacy|post-fix] \
        [--logs "<glob>" ...] [--json-out file.json] [--csv-out file.csv]

For every instance it reads the screener settings from the DB (resolved the way live resolves them), takes the
LIVE SELECTION log lines produced under exactly those thresholds (and, when present, the per-stage counts of the
``feat/screener-stage-log`` ``LIVE STAGES`` line), re-runs the simulation (``ba2_providers.screener.live_sim``)
on the panel for the same mornings, and prints per day: live picks, simulated picks, common, Jaccard, and every
residual symbol with its class (edge of a threshold with its distance, max_stocks displacement, ...).  It also
prints the symbols the DB says were analysed (after the broker / open-position filters) for reference.

``--behaviour legacy`` reproduces the pre-fix live code (the recorded 2026-09/10 days were produced by it);
``post-fix`` (default for any day after the fix is deployed) is the behaviour jobs simulate.  Nothing is written
except the optional --json-out / --csv-out files.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in ("packages/common", "packages/providers", "packages/experts"):
    sys.path.insert(0, os.path.join(ROOT, p))

from ba2_providers.screener import live_parity as lp, live_sim as ls  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="live DB (opened read-only)")
    ap.add_argument("--panel", required=True, help="daily criteria panel directory (cache/screener/daily_panel)")
    ap.add_argument("--instances", required=True, help="comma-separated expert instance ids")
    ap.add_argument("--logs", action="append", help="glob of live log files (repeatable); default: <db dir>/logs/*.log*")
    ap.add_argument("--from", dest="from_day"); ap.add_argument("--to", dest="to_day")
    ap.add_argument("--behaviour", choices=("legacy", "post-fix", "live"), default="live",
                    help="legacy = pre-fix live (validation of recorded days); live = the checkout's own")
    ap.add_argument("--emulate-vendor-history", metavar="SNAPSHOT_JSON",
                    help="VALIDATION ONLY: replace the panel's share counts by the vendor's bulk-table value of that "
                         "snapshot (constant over time = the vendor's own share history over a window of weeks in "
                         "which shares do not change) and drop symbols the vendor no longer lists")
    ap.add_argument("--now", default="5min-open", choices=("5min-open", "5min-0935", "daily-open"),
                    help="the screener's 'now': the open of the 09:30 five-minute bar (what a job's first-bar decision "
                         "reads; default), that bar's close (a 09:35 decision) or the daily bar's open")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply 'now' (sensitivity runs: 0.995, 1.01, ...)")
    ap.add_argument("--json-out"); ap.add_argument("--csv-out")
    args = ap.parse_args(argv)

    panel = ls.load_panel(args.panel)
    if args.emulate_vendor_history:
        import numpy as np
        _snap = json.load(open(args.emulate_vendor_history))
        rows = {k: v[0] / v[1] for k, v in (_snap.get("caps") or {}).items()} or _snap["rows"]   # the cap's own share basis
        sh = np.full(panel.arrays["shares"].shape, np.nan)
        for i, sym in enumerate(panel.symbols):
            if str(sym) in rows:
                sh[:, i] = rows[str(sym)]
        panel = ls.DailyPanel([str(x) for x in panel.symbols], panel.sessions, {**panel.arrays, "shares": sh}, panel.manifest)
        print(f"EMULATED vendor share history from {args.emulate_vendor_history} "
              f"({int(np.isfinite(sh[0]).sum())} symbols with a vendor count)")
    beh = {"legacy": ls.LEGACY_CURRENT, "post-fix": ls.POST_FIX, "live": ls.behaviour_from_live()}[args.behaviour]
    logs = args.logs or [os.path.join(os.path.dirname(os.path.abspath(args.db)), "logs", "*.log*")]
    sel, stg = lp.read_log_records(logs)
    print(f"panel {os.path.basename(os.path.normpath(args.panel))}: {panel.manifest['n_symbols']} symbols, {panel.manifest['first_session']}.."
          f"{panel.manifest['last_bar_date']} (now={args.now} x{args.scale}; criteria {panel.manifest['criteria_version']}, shares "
          f"{panel.manifest['shares_vendor_snapshot']}, lag {panel.manifest['shares_lag_days']}d); behaviour {beh.name}")
    print(f"log records: {len(sel)} LIVE SELECTION, {len(stg)} LIVE STAGES")
    out = {}
    rows = []
    for iid in [int(x) for x in args.instances.split(",")]:
        settings = lp.instance_settings(args.db, iid)
        recs = lp.match_records(sel, settings)
        if args.from_day:
            recs = [r for r in recs if r["day"] >= args.from_day]
        if args.to_day:
            recs = [r for r in recs if r["day"] <= args.to_day]
        res = lp.compare(panel, settings, recs, beh, now_mode=args.now, scale=args.scale)
        analysed = lp.analysed_symbols(args.db, iid)
        stages = {r["day"]: r["stages"] for r in stg}
        print(f"\n=== instance {iid}: cap {settings['market_cap_min']:.3g}..{settings['market_cap_max']:.3g} "
              f"rvol>={settings['relative_volume_min']} drop>={settings['price_drop_pct']}/{settings['price_drop_days']}d "
              f"w2={settings['weinstein_stage2_only']} max={settings['max_stocks']}")
        print(f"{'day':<11}{'live':>5}{'sim':>5}{'both':>5}{'jacc':>7}  {'analysed(db)':>12}  residuals")
        for d in res["days"]:
            if "skipped" in d:
                print(f"{d['day']:<11} skipped: {d['skipped']}")
                continue
            resid = "; ".join(
                [f"live-only {e['symbol']}[{e['class']}" + (f" {e['drop_margin_pct_of_price']:+}" if 'drop_margin_pct_of_price' in e else "") + "]"
                 for e in d["live_only"]] +
                [f"sim-only {e['symbol']}[{e['class']}" + (f" {e['drop_margin_pct_of_price']:+}" if 'drop_margin_pct_of_price' in e else "") + "]"
                 for e in d["sim_only"]])
            st = (f" | live stages {stages[d['day']]}" if d["day"] in stages else "") +                  f" | sim stages {d['sim_stages']}"
            print(f"{d['day']:<11}{d['live']:>5}{d['sim']:>5}{d['common']:>5}{d['jaccard']:>7.3f}  "
                  f"{len(analysed.get(d['day'], [])):>12}  {resid}{st}")
            rows.append({"instance": iid, "day": d["day"], "live": d["live"], "sim": d["sim"],
                         "common": d["common"], "jaccard": d["jaccard"]})
        print("summary:", json.dumps(res["summary"]))
        for u in res["unexplained"]:
            print("  UNEXPLAINED:", json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in u.items()}))
        out[str(iid)] = {"settings": settings, **res}
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(out, f, indent=1, default=str)
    if args.csv_out and rows:
        with open(args.csv_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
