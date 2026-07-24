"""Ad-hoc probe: structure-level exit analysis of an options backtest run.

Groups per-leg option trades into structures (same underlying + entry/exit time)
and reports bars_held distribution plus real net P&L as % of net premium — used to
validate the B9 fix (structure P&L priced off net premium instead of underlying price).

Usage:
    .venv/Scripts/python.exe test_files/analyze_spread_exits.py [backtest_id]
Without an id, uses the newest backtest named like 'TOP1-optm-FMPRating-OS2%'.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = Path.home() / "Documents" / "ba2" / "test" / "dl_forecasting.db"


def main() -> None:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if len(sys.argv) > 1:
        row = cur.execute("SELECT * FROM backtests WHERE id = ?", (int(sys.argv[1]),)).fetchone()
    else:
        row = cur.execute(
            "SELECT * FROM backtests WHERE name LIKE 'TOP1-optm-FMPRating-OS2%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        print("no matching backtest found")
        return

    cols = set(row.keys())
    print(f"backtest id={row['id']} name={row['name']}")
    for c in ("total_return", "total_trades", "created_at", "finished_at"):
        if c in cols:
            print(f"  {c} = {row[c]}")

    if "strategy_params" in cols and row["strategy_params"]:
        params = json.loads(row["strategy_params"])
        exits = {k: v for k, v in params.items() if k.startswith(("exit:", "cond:"))}
        print(f"  exits/conditions: {json.dumps(exits)}")

    trades = json.loads(row["trades"]) if row["trades"] else []
    print(f"  trade rows: {len(trades)}")
    if not trades:
        return
    print(f"  trade fields: {sorted(trades[0].keys())}")

    groups = defaultdict(list)
    for t in trades:
        key = (t.get("underlying_symbol") or t.get("symbol"), t["entry_time"], t["exit_time"])
        groups[key].append(t)

    print(f"  structures: {len(groups)} (leg counts: {sorted({len(v) for v in groups.values()})})")

    sign = {"buy": -1.0, "sell": 1.0}  # buy pays premium, sell receives
    bars_dist = defaultdict(int)
    one_bar = 0
    rows = []
    for (underlying, entry_time, exit_time), legs in groups.items():
        net_entry = sum(
            sign[l["direction"].lower()] * l["entry_price"] * l["size"] * 100 for l in legs
        )
        tot_pnl = sum(l["pnl"] for l in legs)
        bars = max(l["bars_held"] for l in legs)
        pct = tot_pnl / abs(net_entry) * 100 if abs(net_entry) > 1e-9 else float("nan")
        bars_dist[bars] += 1
        if bars <= 1:
            one_bar += 1
        rows.append((entry_time, underlying, len(legs), bars, net_entry, tot_pnl, pct))

    print("\nbars_held distribution (bars: #structures):")
    for b in sorted(bars_dist):
        print(f"  {b:>4}: {bars_dist[b]}")
    print(f"\nstructures closed after <=1 bar: {one_bar}/{len(rows)}")

    print("\nper-structure detail (entry_time, underlying, legs, bars, net_entry_premium, pnl, pnl%):")
    for entry_time, underlying, nlegs, bars, net_entry, tot_pnl, pct in sorted(rows):
        kind = "credit" if net_entry > 0 else "debit"
        print(
            f"  {entry_time} {underlying:<6} legs={nlegs} bars={bars:>3} "
            f"{kind} ${abs(net_entry):>8.2f} pnl=${tot_pnl:>8.2f} ({pct:+.1f}%)"
        )

    tot_pnl_all = sum(r[5] for r in rows)
    tot_basis = sum(abs(r[4]) for r in rows)
    print(f"\ntotal pnl ${tot_pnl_all:.2f} on ${tot_basis:.2f} premium "
          f"({tot_pnl_all / tot_basis * 100 if tot_basis else float('nan'):+.2f}% per-premium)")


if __name__ == "__main__":
    main()
