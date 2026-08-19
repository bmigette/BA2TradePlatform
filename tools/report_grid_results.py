#!/usr/bin/env python
"""Report FINISHED optimization jobs in economic terms, not just GA fitness.

Fitness is a ranking device: it folds CAR, drawdown, concentration, Monte-Carlo and spread into
one number that is only comparable WITHIN a scale generation (it changed on 2026-08-04, and the
robustness multipliers changed again after). It says nothing about what the strategy actually
earned. This prints total profit, annualized return and max drawdown alongside it.

Usage:
    python tools/report_grid_results.py                    # every finished goal2020 job
    python tools/report_grid_results.py --like %%Senate%%  # filter by backtest name
    python tools/report_grid_results.py --top 3            # show TOP1..TOP3 per job
"""
from __future__ import annotations

import argparse
import os
import sqlite3

DB = os.path.expanduser("~/Documents/ba2/test/dl_forecasting.db")


def _rows(like: str, top: int):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    return con.execute(
        """
        select name, initial_capital, final_equity, total_return, annualized_return,
               max_drawdown, calmar_ratio, total_trades, win_rate, ga_fitness,
               start_date, end_date
        from backtests
        where status = 'completed' and name like ? and ga_fitness is not null
        order by name
        """,
        (like,),
    ).fetchall(), top


def _job_of(name: str) -> tuple:
    """('TOP2-scr-mid-Foo-S1-...', ) -> (rank, job). Unranked names sort as rank 0."""
    if name.startswith("TOP") and "-" in name:
        head, rest = name.split("-", 1)
        try:
            return int(head[3:]), rest
        except ValueError:
            pass
    return 0, name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--like", default="%goal2020%", help="SQL LIKE over the backtest name")
    ap.add_argument("--top", type=int, default=1, help="how many ranks per job (default 1)")
    args = ap.parse_args()

    rows, top = _rows(args.like, args.top)
    if not rows:
        print(f"no completed backtests matching {args.like!r}")
        return

    by_job: dict = {}
    for r in rows:
        rank, job = _job_of(r[0])
        by_job.setdefault(job, []).append((rank, r))

    hdr = (f"{'job':<52} {'#':>2} {'profit':>11} {'total':>8} {'CAR':>7} "
           f"{'maxDD':>7} {'calmar':>7} {'trades':>7} {'win%':>6} {'fitness':>8}")
    print(hdr)
    print("-" * len(hdr))
    for job in sorted(by_job):
        for rank, r in sorted(by_job[job])[:top]:
            (_n, cap, final, tot, car, dd, calmar, trades, win, fit, _s, _e) = r
            profit = (final - cap) if (final is not None and cap is not None) else None
            print(f"{job[:52]:<52} {rank:>2} "
                  f"{('$' + format(profit, ',.0f')) if profit is not None else 'n/a':>11} "
                  f"{('%.1f%%' % tot) if tot is not None else 'n/a':>8} "
                  f"{('%.2f%%' % car) if car is not None else 'n/a':>7} "
                  f"{('%.1f%%' % dd) if dd is not None else 'n/a':>7} "
                  f"{('%.2f' % calmar) if calmar is not None else 'n/a':>7} "
                  f"{trades if trades is not None else 'n/a':>7} "
                  f"{('%.0f' % win) if win is not None else 'n/a':>6} "
                  f"{fit:>8.3f}")

    print("\nCAR = annualized return; maxDD = max drawdown; calmar = CAR/|maxDD|.")
    print("Profit is final_equity - initial_capital over the FULL window shown in the DB row.")
    print("Fitness is only comparable within one scale generation (changed 2026-08-04).")


if __name__ == "__main__":
    main()
