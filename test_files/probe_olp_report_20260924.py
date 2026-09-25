"""Aggregate the probe_olp_funnel_20260924.py outputs into report tables (stdout).

Usage: .venv/Scripts/python.exe test_files/probe_olp_report_20260924.py [label ...]
"""
import collections
import csv
import gzip
import json
import os
import sys

OUT_DIR = (r"C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-"
           r"BA2TradePlatform\820f80e0-b6ea-41a0-8f76-d0176d9f7156\scratchpad\olp_diag")


def load(label):
    d = os.path.join(OUT_DIR, label)
    s = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
    t = json.load(open(os.path.join(d, "trades.json"), encoding="utf-8"))
    with gzip.open(os.path.join(d, "funnel_rows.csv.gz"), "rt", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    o = json.load(open(os.path.join(d, "orders.json"), encoding="utf-8"))
    r = json.load(open(os.path.join(d, "refine_trades.json"), encoding="utf-8"))
    return s, t, rows, o, r


def funnel(rows, leaf_labels):
    n = len(rows)
    sig = collections.Counter(r["sig"] for r in rows)
    staged = [r for r in rows if r["stage"] != "no_rec"]
    print(f"  decision points (entry day x offered symbol with a rec): {n}")
    print(f"  expert signal: {dict(sig)}")
    print(f"  staged (non-HOLD/SKIP): {len(staged)}")
    # successive leaf funnel
    alive = len(staged)
    fails = collections.Counter(int(r["fail_idx"]) for r in staged if r["stage"] == "cond_fail")
    status = collections.defaultdict(collections.Counter)
    for r in staged:
        if r["stage"] == "cond_fail":
            status[int(r["fail_idx"])][r["fail_status"] or "-"] += 1
    for i, lab in enumerate(leaf_labels):
        k = fails.get(i, 0)
        st = dict(status[i]) if status[i] else ""
        print(f"   leaf {i:2d} {lab[:70]:70s} fail {k:6d}  -> pass {alive - k:6d} {st}")
        alive -= k
    rest = collections.Counter(r["stage"] for r in staged if r["stage"] != "cond_fail")
    print(f"  after all leaves: {dict(rest)}")
    msgs = collections.Counter()
    for r in staged:
        if r["stage"] == "action_refused":
            m = r["msg"] or ""
            head = m.split("|diag")[0]
            head = head.replace(r["sym"], "<SYM>")
            import re
            head = re.sub(r"\(premium=[0-9.]+\)", "(premium=x)", head)
            head = re.sub(r"[A-Z]+\d{6}[CP]\d{8}", "<OCC>", head)
            msgs[head[:110]] += 1
    for m, c in msgs.most_common(8):
        print(f"     refused x{c}: {m}")
    diag = [r["msg"].split("|diag")[1] for r in staged
            if r["stage"] == "action_refused" and "|diag" in (r["msg"] or "")]
    if diag:
        agg = collections.Counter()
        for dg in diag:
            parts = dict(p.split("=", 1) if "=" in p else (p, "") for p in dg.split())
            key = ("in_dte=0" if any(k.startswith("in_dte") and v == "0" for k, v in parts.items())
                   else "liq_pass=0" if parts.get("liq_pass") == "0" else "other")
            agg[key] += 1
        print(f"     no-contract diagnostics: {dict(agg)}")


def main():
    labels = sys.argv[1:] or sorted(x for x in os.listdir(OUT_DIR)
                                    if os.path.isfile(os.path.join(OUT_DIR, x, "summary.json")))
    for lab in labels:
        s, t, rows, o, r = load(lab)
        dec = json.load(open(os.path.join(OUT_DIR, lab, "decoded_rules.json"), encoding="utf-8"))
        print("=" * 100)
        print(f"{lab}: GA={s['ga_record'] and {k: s['ga_record'][k] for k in ('trades','total_return','max_drawdown','fitness')}}")
        print(f"  rerun={s['rerun']}  fitness={s['rerun_fitness']}")
        print(f"  entry days={s['n_entry_days']} offered={s['universe_offered_total']} screen={s['screen_sizes']}")
        print(f"  schedule={dec['run_schedule_override']}")
        funnel(rows, dec["leaf_labels"])
        print(f"  orders (is_entry|status): {s['orders_by_(entry,status)']} illiquid_rejects={s['rejected_illiquid_fills']} arb_rejects={s['rejected_arb_fills']}")
        # expired orders: participation-cap reject vs limit never crossed (from the run log)
        import re
        logp = os.path.join(OUT_DIR, f"log_{lab}.txt")
        illiq = collections.Counter()
        if os.path.exists(logp):
            for line in open(logp, encoding="utf-8", errors="replace"):
                m = re.search(r"illiquid option fill REJECTED: \S+ (\S+) qty (\S+)", line)
                if m:
                    illiq[(m.group(1), float(m.group(2)))] += 1
        cls = collections.Counter()
        for od in o.get("orders", []):
            if od["status"] != "expired":
                continue
            kind = "entry" if od["entry"] else "exit"
            why = ("participation_cap" if illiq.get((od["contract"], float(od["qty"])))
                   else "limit_not_crossed_or_no_bar")
            cls[f"{kind}:{why}"] += 1
        big = [od["qty"] for od in o.get("orders", []) if od["entry"]]
        print(f"  expired-order causes: {dict(cls)}  entry qty median={sorted(big)[len(big)//2] if big else None} max={max(big) if big else None}")
        print(f"  per-year trades (by exit): " + ", ".join(
            f"{y}: n={v['trades']} pnl={v['pnl']:.0f} w={v['wins']}" for y, v in s["per_year_trades"].items()))
        print(f"  per-year equity: " + ", ".join(
            f"{y}: {v['ret_pct']}% (min {v['min']:.0f})" for y, v in s["per_year_equity"].items()))
        print(f"  daily-curve worst DD: {s['daily_curve_worst_dd']}  min equity {s['min_equity_point']}")
        print(f"  refine: {s['refine_summary']}")
        for d in s["refine_worst"][:4]:
            print(f"     refine trade {d['contract']} entry {d['entry'][:10]} exit {d['exit'][:10]} pnl {d['pnl']:.0f} worst {d.get('worst_pnl')} extra {d.get('extra_loss')} eq {d.get('equity_at_entry')} cand {d.get('candidate_dd'):.1f}")
        print("  trades:")
        for x in t:
            print(f"    {x['entry_time'][:10]} -> {(x['exit_time'] or '')[:10]} {x.get('contract_symbol')} "
                  f"size {x['size']:.0f} in {x['entry_price']:.3f} out {x['exit_price']:.3f} "
                  f"pnl {x['pnl']:9.0f} ({x['pnl_pct']:.0f}%) {x['exit_reason']}")


if __name__ == "__main__":
    main()
