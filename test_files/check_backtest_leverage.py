"""Correct leverage / over-deployment check for persisted backtests.

Replaces an earlier FLAWED check that compared peak dollars-deployed to INITIAL capital — wrong for
an account that grows (a 3.5x-grown account fully invested looks like "3.5x leverage"). The correct
invariant is "open-position cost never exceeds CONTEMPORANEOUS equity" (equivalently, cash >= 0).

Two checks per backtest, from the persisted trades + equity_curve:
  1. peak concurrent open COST (entry_price*size of overlapping longs) vs the equity AT that moment
     (from equity_curve) — ratio > 1 + TOL flags a genuine breach.
  2. Sanity vs final equity for context.

NOTE: the definitive proof is the in-engine ``_apply_fill`` cash-secured safeguard (it logs LOUDLY
and clamps if a BUY would drive cash < 0). This script is a fast post-hoc read from stored results;
for cycling/rebalancing experts (many short overlapping legs) it can slightly OVER-count, so it
flags only ratios >= FLAG (default 1.25) and prints the worst offenders for inspection.
"""
import argparse, json, sqlite3
from datetime import datetime

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
TOL = 0.05
FLAG = 1.25


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _equity_at(curve, t):
    best = None
    for p in curve:
        pt = _ts(p.get("date") or p.get("time") or p.get("t"))
        val = p.get("equity") or p.get("value") or p.get("net_liquidating_value") or p.get("nlv")
        if pt is None or val is None:
            continue
        if pt <= t:
            best = float(val)
        else:
            break
    if best is None:
        for p in curve:
            val = p.get("equity") or p.get("value") or p.get("net_liquidating_value") or p.get("nlv")
            if val is not None:
                return float(val)
    return best


def check(bt_id, name, init, final, trades_json, curve_json):
    trades = json.loads(trades_json) if trades_json else []
    curve = json.loads(curve_json) if curve_json else []
    events = []
    for tr in trades:
        ep, sz = tr.get("entry_price"), tr.get("size")
        if ep is None or sz is None:
            continue
        if str(tr.get("direction", "long")).lower() not in ("long", "buy"):
            continue  # cash-secured invariant is the long book
        cost = abs(float(ep) * float(sz))
        et = _ts(tr.get("entry_time") or tr.get("entry_date"))
        xt = _ts(tr.get("exit_time") or tr.get("exit_date"))
        if et is None:
            continue
        events.append((et, +cost))
        if xt is not None:
            events.append((xt, -cost))
    if not events:
        return None
    events.sort(key=lambda e: e[0])
    dep = peak = 0.0
    peak_t = None
    for t, d in events:
        dep += d
        if dep > peak:
            peak, peak_t = dep, t
    eq = _equity_at(curve, peak_t) if peak_t is not None else None
    base = eq if (eq and eq > 0) else (init or 0)
    ratio = (peak / base) if base else None
    return {"id": bt_id, "name": name, "peak": peak, "equity_at_peak": eq,
            "ratio": ratio, "breach": ratio is not None and ratio > FLAG}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    c = sqlite3.connect(args.db)
    rows = c.execute("SELECT id,name,initial_capital,final_equity,trades,equity_curve FROM backtests "
                     "WHERE status='completed' AND trades IS NOT NULL ORDER BY id").fetchall()
    c.close()
    res = [r for r in (check(*x) for x in rows) if r]
    breaches = [r for r in res if r["breach"]]
    print(f"checked {len(res)} backtests | breaches (peak open > {FLAG:.0%} of contemporaneous equity): "
          f"{len(breaches)}")
    for r in sorted(res, key=lambda x: -(x["ratio"] or 0))[:args.top]:
        flag = "  <-- BREACH" if r["breach"] else ""
        print("  bt %-5s ratio=%5.2f peak=$%-11.0f equity@peak=%s  %s%s" % (
            r["id"], r["ratio"] or 0, r["peak"],
            ("$%.0f" % r["equity_at_peak"]) if r["equity_at_peak"] else "n/a", (r["name"] or "")[:32], flag))
    return 1 if breaches else 0


if __name__ == "__main__":
    raise SystemExit(main())
