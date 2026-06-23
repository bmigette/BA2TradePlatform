"""Backfill backtests.adjusted_total_return from stored trades at a per-trade profit cap.

The cap feature computes adjusted_total_return at run time, but existing rows predate it. Since
every backtest stores its full trade list, the adjusted return is fully recomputable WITHOUT
re-running: for each trade, cap its gain at cap_pct of cost basis (entry_price x size); the
adjusted total return = (final - sum_of_excess - initial) / initial.

Usage:
    python test_files/backfill_adjusted_total_return.py [--cap 2000] [--dry-run]
"""
import argparse
import json
import sqlite3

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=2000.0, help="Per-trade profit cap %% of cost basis.")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cap_frac = args.cap / 100.0

    c = sqlite3.connect(args.db)
    rows = c.execute(
        "select id, name, initial_capital, total_return, trades from backtests "
        "where trades is not null and trades != ''"
    ).fetchall()
    updated = 0
    big_change = []
    for bid, name, ic, tr, tj in rows:
        if ic is None or tr is None:
            continue
        try:
            trades = json.loads(tj)
        except Exception:
            continue
        final = ic * (1 + tr / 100.0)
        excess = 0.0
        for t in trades:
            p = t.get("pnl") or 0.0
            cost = (t.get("entry_price") or 0.0) * (t.get("size") or 0.0)
            if p > 0 and cost > 0:
                excess += max(0.0, p - cost * cap_frac)
        adj_final = final - excess
        adj_ret = round(((adj_final - ic) / ic * 100.0) if ic else 0.0, 2)
        if abs(adj_ret - tr) > 1.0:
            big_change.append((bid, name, round(tr, 0), adj_ret))
        if not args.dry_run:
            c.execute("update backtests set adjusted_total_return=? where id=?", (adj_ret, bid))
        updated += 1
    if not args.dry_run:
        c.commit()
    print(f"{'(dry-run) ' if args.dry_run else ''}backfilled adjusted_total_return for {updated} backtests (cap {args.cap:.0f}%).")
    print(f"\n{len(big_change)} with material change (raw -> adjusted):")
    for bid, name, raw, adj in sorted(big_change, key=lambda x: -(x[2] - x[3])):
        print(f"  bid {bid:>3}  raw {raw:>7.0f}% -> adj {adj:>7.0f}%   {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
