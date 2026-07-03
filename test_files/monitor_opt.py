"""Poll a strategy_optimization until it leaves 'running', then dump its top tagged backtests.

Usage: monitor_opt.py <optimization_id> [poll_seconds]
Re-invokes nothing itself; run in the background so the harness notifies on exit.
"""
import json
import sqlite3
import sys
import time

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"


def _cols(c, t):
    return [d[1] for d in c.execute(f"PRAGMA table_info({t})").fetchall()]


def _genes(strategy_params):
    try:
        sp = json.loads(strategy_params) if isinstance(strategy_params, str) else (strategy_params or {})
    except Exception:
        return {}
    return {k.split(":", 1)[1]: v for k, v in sp.items()
            if isinstance(k, str) and k.startswith("model:")} if isinstance(sp, dict) else {}


def main():
    oid = int(sys.argv[1])
    poll = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    while True:
        c = sqlite3.connect(DB)
        st = c.execute("SELECT name,status,best_fitness FROM strategy_optimizations WHERE id=?",
                       (oid,)).fetchone()
        if st and st[1] != "running":
            name, status, bf = st
            print(f"=== opt {oid} '{name}' -> {status}  best_fitness={bf} ===", flush=True)
            bc = _cols(c, "backtests")
            rows = c.execute(
                "SELECT * FROM backtests WHERE optimization_id=? "
                "ORDER BY (adjusted_total_return IS NULL), adjusted_total_return DESC, "
                "total_return DESC LIMIT 5", (oid,)).fetchall()
            print(f"-- top {len(rows)} tagged backtests --", flush=True)
            for r in rows:
                d = dict(zip(bc, r))
                res = d.get("results")
                try:
                    res = json.loads(res) if isinstance(res, str) else (res or {})
                except Exception:
                    res = {}
                atpy = res.get("avg_trades_per_year")
                g = _genes(d.get("strategy_params"))
                # surface the two new guard genes explicitly
                mpt = g.get("min_price_targets_per_quarter")
                maa = g.get("max_analyst_age_months")
                print(f"  {d['name']}: ret={d['total_return']}% adj={d.get('adjusted_total_return')}% "
                      f"trades={d['total_trades']} (/yr={atpy}) calmar={d['calmar_ratio']} "
                      f"sharpe={d['sharpe_ratio']} win%={d['win_rate']} dd={d['max_drawdown']}", flush=True)
                print(f"     guards: min_price_targets/q={mpt} max_analyst_age_mo={maa} | "
                      f"min_analysts={g.get('min_analysts')} target={g.get('target_price_type')} "
                      f"profit_ratio={g.get('profit_ratio')} ptw_days={g.get('price_target_window_days')}",
                      flush=True)
            c.close()
            return 0
        c.close()
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
