"""READ-ONLY validation of the consistent_annual_return ("goal") fitness on real results.

Loads every ``backtests`` row named 'TOP%-aggr' from the test DB (sqlite, mode=ro URI),
reassembles the results dict compute_fitness needs (metrics JSON + the separately-stored
equity_curve/trades columns), computes the NEW fitness for each, and prints a ranked table.

Acceptance expectation: balanced configs (e.g. TOP1-scr-large-FMPRating-S4-aggr) rank near
the top; concentrated/uneven S2-mid 100%+ outliers get discounted; <30 trades/yr disqualified.

Usage:
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe test_files/validate_goal_fitness.py
"""
import json
import os
import sqlite3
import sys

# Import compute_fitness from the testplatform backend (no package install needed).
_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "testplatform", "backend")
sys.path.insert(0, _BACKEND)

from app.services.strategy_fitness import (  # noqa: E402
    LOW_TRADE_SENTINEL,
    ZERO_TRADE_SENTINEL,
    _calendar_year_returns,
    _consistency_factor,
    compute_fitness,
)

DB_URI = "file:C:/Users/basti/Documents/ba2/test/dl_forecasting.db?mode=ro"


def main() -> None:
    con = sqlite3.connect(DB_URI, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT name, results, equity_curve, trades FROM backtests "
        "WHERE name LIKE 'TOP%-aggr' ORDER BY id"
    ).fetchall()
    con.close()
    print(f"{len(rows)} 'TOP%-aggr' backtests loaded from {DB_URI}\n")

    scored = []
    for row in rows:
        results = json.loads(row["results"]) if row["results"] else None
        if results is None:
            print(f"SKIP {row['name']}: no results JSON")
            continue
        # equity_curve / trades live in their own columns — reassemble the full dict.
        results["equity_curve"] = json.loads(row["equity_curve"]) if row["equity_curve"] else []
        results["trades"] = json.loads(row["trades"]) if row["trades"] else []

        fit = compute_fitness("consistent_annual_return", results)
        yrs = _calendar_year_returns(results["equity_curve"])
        scored.append({
            "name": row["name"],
            "fitness": fit,
            "ann": results["annualized_return"],
            "adj_ann": results["adjusted_annualized_return"],
            "calmar": results["calmar_ratio"],
            "dd": results["max_drawdown"],
            "tpy": results["avg_trades_per_year"],
            "years": yrs,
            "consistency": _consistency_factor(yrs),
        })

    scored.sort(key=lambda r: r["fitness"], reverse=True)

    hdr = (f"{'#':>2}  {'name':<38} {'fitness':>9} {'ann%':>7} {'adj%':>7} "
           f"{'calmar':>6} {'dd%':>7} {'tr/yr':>6} {'consis':>6}  per-year returns %")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(scored, 1):
        if r["fitness"] == ZERO_TRADE_SENTINEL:
            fit_s = "NO-TRADE"
        elif r["fitness"] == LOW_TRADE_SENTINEL:
            fit_s = "DISQ<30"
        else:
            fit_s = f"{r['fitness']:.2f}"
        yrs_s = ", ".join(f"{y:+.1f}" for y in r["years"])
        print(f"{i:>2}  {r['name']:<38} {fit_s:>9} {r['ann']:>7.2f} {r['adj_ann']:>7.2f} "
              f"{r['calmar']:>6.2f} {r['dd']:>7.2f} {r['tpy']:>6.1f} {r['consistency']:>6.2f}  [{yrs_s}]")


if __name__ == "__main__":
    main()
