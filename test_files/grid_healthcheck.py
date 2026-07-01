"""One-shot grid health snapshot for the -tpsl screener matrix. Prints a concise status + a
HEALTH: OK | ATTENTION line so the caller can decide whether to alert. No sleeping here — the
caller schedules the cadence."""
import json
import sqlite3

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
SUFFIX = "-aggr"
TOTAL_PLANNED = 31


def main():
    c = sqlite3.connect(DB)
    jobs = c.execute("SELECT id,name,status FROM strategy_optimizations WHERE name LIKE ?",
                     (f"%{SUFFIX}",)).fetchall()
    done = [j for j in jobs if j[2] == "completed"]
    running = [j for j in jobs if j[2] == "running"]
    bad = [j for j in jobs if j[2] in ("cancelled", "failed", "error")]
    print(f"{SUFFIX} grid: {len(done)} completed / {len(running)} running / {len(bad)} bad "
          f"(of {len(jobs)} created, {TOTAL_PLANNED} planned)")
    if running:
        print(f"  running now: {', '.join(r[1] for r in running)}")
    if bad:
        print(f"  BAD: {', '.join(b[1]+'='+b[2] for b in bad)}")

    # latest completed FMPRating job + its TOP1 backtest (sanity that results look healthy)
    fmp_done = [j for j in done if "FMPRating" in j[1]]
    if fmp_done:
        last = fmp_done[-1]
        bc = [d[1] for d in c.execute("PRAGMA table_info(backtests)").fetchall()]
        r = c.execute("SELECT * FROM backtests WHERE optimization_id=? AND name LIKE 'TOP1%'",
                      (last[0],)).fetchone()
        if r:
            d = dict(zip(bc, r))
            res = d.get("results")
            try:
                res = json.loads(res) if isinstance(res, str) else (res or {})
            except Exception:
                res = {}
            sp = d.get("strategy_params")
            try:
                sp = json.loads(sp) if isinstance(sp, str) else (sp or {})
            except Exception:
                sp = {}
            print(f"  last FMPRating '{last[1]}' TOP1: ret={d['total_return']}% "
                  f"adj={d.get('adjusted_total_return')}% trades={d['total_trades']} "
                  f"(/yr={res.get('avg_trades_per_year')}) calmar={d['calmar_ratio']} dd={d['max_drawdown']}% "
                  f"| guards mpt/q={sp.get('model:min_price_targets_per_quarter')} "
                  f"maa_mo={sp.get('model:max_analyst_age_months')}")
    c.close()
    print("HEALTH: ATTENTION" if bad else "HEALTH: OK")


if __name__ == "__main__":
    main()
