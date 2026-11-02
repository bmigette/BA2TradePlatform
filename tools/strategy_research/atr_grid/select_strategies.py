#!/usr/bin/env python
"""Pick which classic strategies (S1-S7) each expert runs in the ATR grid, from goal2020 results.

Operator rule (2026-09-29): a strategy that is never in the top 5 is not worth grid time.

"Top 5" is read PER EXPERT, over the pooled goal2020 TOP-N backtests of that expert (every band,
both sizing modes). A strategy is kept for an expert when at least one of its rows reaches that
pool's top N by ANY of the chosen rankings (default: GA fitness, CAR). Per cell (6 strategies)
"top 5" only means "not last" and drops nothing; per expert x band the pools are too thin (5
slots over ~12 jobs) to be stable. See docs/strategy_research/atr_grid/s1_s7_relevance.md.

Reads the test-platform DB READ-ONLY (``mode=ro``), touching ``backtests`` only through the
covering index ``ix_backtests_summary`` (no blob column is read). Writes a plan JSON that
``tools/run_screener_capband_matrix.py --strategy-plan`` and ``tools/grid_atr27.sh`` consume.

Usage:
    python tools/strategy_research/atr_grid/select_strategies.py \
        --out docs/strategy_research/atr_grid/strategy_plan.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

DEFAULT_DB = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "test", "dl_forecasting.db")
STRATEGIES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
RANKINGS = ("fitness", "car", "calmar")

# goal2020 job names. Only the canonical rows count: a tail other than the data-floor tag
# (-from2022) or matrix 3's -ds is a probe or re-run variant, as in extract_goal2020.py.
NAME_RE = re.compile(
    r"^scr-(?P<band>large|mid|small)-(?P<expert>[A-Za-z]+)-(?P<strat>S\d)-goal2020-"
    r"(?P<mode>riskatr|risk_atr|notional)(?P<tail>.*)$")
SEN_RE = re.compile(r"^sen-(?P<strat>S\d)-goal2020-(?P<mode>risk_atr|notional)(?P<tail>.*)$")
CANONICAL_TAILS = {"", "-from2022", "-ds"}


def parse_job(name: str):
    """(expert, band, strategy, mode) for a canonical goal2020 strategy job, else None."""
    m = NAME_RE.match(name)
    if m:
        d = m.groupdict()
    else:
        m = SEN_RE.match(name)
        if not m:
            return None
        d = {**m.groupdict(), "band": "all", "expert": "FMPSenateTraderWeight"}
    if d["tail"] not in CANONICAL_TAILS:
        return None
    mode = "risk_atr" if d["mode"] in ("riskatr", "risk_atr") else "notional"
    return d["expert"], d["band"], d["strat"], mode


def car_pct(total_return_pct: float, start: str, end: str) -> float:
    years = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days / 365.25
    if years <= 0:
        raise ValueError(f"empty backtest window {start}..{end}")
    growth = 1.0 + total_return_pct / 100.0
    if growth <= 0:
        return -100.0
    return (growth ** (1.0 / years) - 1.0) * 100.0


def load_rows(db_path: str, like: str) -> list:
    """Every completed TOP-N backtest of a completed canonical goal2020 strategy job."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        opts = {}
        for oid, name in con.execute(
                "select id, name from strategy_optimizations "
                "where status = 'completed' and name like ?", (like,)):
            job = parse_job(name)
            if job:
                opts[oid] = (name, job)
        if not opts:
            raise SystemExit(f"no completed canonical jobs match {like!r} in {db_path}")
        qmarks = ",".join("?" * len(opts))
        rows = []
        for (bid, name, oid, status, tr, dd, trades, gaf, sd, ed) in con.execute(
                f"select id, name, optimization_id, status, total_return, max_drawdown, "
                f"total_trades, ga_fitness, start_date, end_date from backtests "
                f"indexed by ix_backtests_summary where optimization_id in ({qmarks})",
                list(opts)):
            if status != "completed" or not re.match(r"^TOP\d+-", name or ""):
                continue
            if tr is None or gaf is None or sd is None or ed is None:
                raise SystemExit(f"backtest {bid} ({name}) lacks return/fitness/dates")
            job_name, (expert, band, strat, mode) = opts[oid]
            car = car_pct(tr, sd, ed)
            ddabs = abs(dd) if dd else 0.0
            rows.append({"backtest_id": bid, "job": job_name, "expert": expert, "band": band,
                         "strategy": strat, "mode": mode, "fitness": gaf, "car": car,
                         "max_drawdown": dd, "calmar": car / ddabs if ddabs else 0.0,
                         "trades": trades})
        return rows
    finally:
        con.close()


def select(rows: list, top_n: int, rankings) -> dict:
    """Per expert: the strategies with a row in the pooled top ``top_n`` of any ranking."""
    pools = defaultdict(list)
    for r in rows:
        pools[r["expert"]].append(r)
    plan, evidence = {}, {}
    for expert, pool in sorted(pools.items()):
        kept = set()
        evidence[expert] = {"rows": len(pool),
                            "strategies_seen": sorted({r["strategy"] for r in pool})}
        for ranking in rankings:
            top = sorted(pool, key=lambda r: (-r[ranking], r["backtest_id"]))[:top_n]
            kept |= {r["strategy"] for r in top}
            evidence[expert][f"top{top_n}_{ranking}"] = [
                {"strategy": r["strategy"], "band": r["band"], "mode": r["mode"],
                 "backtest_id": r["backtest_id"], ranking: round(r[ranking], 3),
                 "car": round(r["car"], 2), "max_drawdown": r["max_drawdown"]}
                for r in top]
        plan[expert] = sorted(kept)
        evidence[expert]["dropped"] = sorted(set(evidence[expert]["strategies_seen"]) - kept)
    return {"experts": plan, "evidence": evidence}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--like", default="%goal2020%", help="SQL LIKE over the optimization name")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--rankings", default="fitness,car",
                    help=f"comma list from {','.join(RANKINGS)}; a strategy is kept when it "
                         f"reaches the top N by ANY of them")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    rankings = [r.strip() for r in args.rankings.split(",") if r.strip()]
    bad = [r for r in rankings if r not in RANKINGS]
    if bad or not rankings:
        ap.error(f"--rankings must be a non-empty list from {RANKINGS}; got {args.rankings!r}")
    if args.top_n < 1:
        ap.error("--top-n must be >= 1")

    rows = load_rows(args.db, args.like)
    result = select(rows, args.top_n, rankings)
    doc = {
        "rule": (f"keep a strategy for an expert when one of its goal2020 TOP-N backtests is in "
                 f"that expert's pooled top {args.top_n} (all bands, both sizing modes) by any "
                 f"of: {', '.join(rankings)}"),
        "source": {"db": args.db, "like": args.like, "rows": len(rows),
                   "generated_at": datetime.now().isoformat(timespec="seconds")},
        **result,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    for expert, strats in doc["experts"].items():
        dropped = doc["evidence"][expert]["dropped"]
        print(f"{expert:24} keep {','.join(strats):16} drop {','.join(dropped) or '-'}")
    print(f"wrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
