"""Re-run ONE optimization's TOP-N genome as a single backtest and print its metrics.

Rebuilds the trial exactly as the GA did -- same fitness-dedup ranking, same
``decode_params`` + ``_build_daily_trial_config`` -- so the result is directly comparable with
what the optimization recorded. Two uses so far:

1. RE-MEASURE A WINNER ON A DIFFERENT WINDOW (set ``BA2_START`` / ``BA2_END``). A genome
   optimized over one period can be scored over another WITHOUT an 8h re-optimization, which is
   how old-window winners are made comparable after a window change. ~4-12 min per run.

       BA2_END=2025-12-31 python tools/run_genome_once.py 238 1 S1-on-2025

2. VERIFY REPRODUCIBILITY. Run it repeatedly in SEPARATE processes and compare; add
   ``persistdb`` as a 4th arg to reproduce what ``_persist_top_backtests`` does (an on-disk
   trading DB instead of the RAM-only store). This is how the 2026-07-30 inert-condition bug was
   pinned down: the same genome gave 103 trades / 17.55% in-memory and 169 / 0.20% on disk,
   because the conditions could not see the in-memory trade store.

COST: one process, one trial, peak RSS ~14-15GB. Do NOT run two at once, and preferably not
while a grid is running -- the first attempt at this was OOM-killed alongside a 4+4 grid.

Usage:  [BA2_START=YYYY-MM-DD] [BA2_END=YYYY-MM-DD] python tools/run_genome_once.py \\
            <opt_id> <rank> <label> [persistdb]
"""
import json
import logging
import os
import sqlite3
import sys
import time

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
BACKEND = os.path.join(REPO, "testplatform", "backend")
for p in (BACKEND, os.path.join(REPO, "testplatform")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(BACKEND)

from app.models.database import DATABASE_URL as _DB_URL  # noqa: E402
if _DB_URL.startswith("sqlite:///"):
    from ba2_common.core import db as _ba2_db  # noqa: E402
    _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))
if not os.getenv("FMP_API_KEY"):
    from ba2_common.config import get_app_setting  # noqa: E402
    _k = get_app_setting("FMP_API_KEY")
    if _k:
        os.environ["FMP_API_KEY"] = _k

# Standalone runs bypass the GA's own suppression -> 10x+ slower and a flood of WARNINGs.
logging.disable(logging.WARNING)

DB = os.path.expanduser(r"~\Documents\ba2\test\dl_forecasting.db")


def main() -> int:
    opt_id, rank, label = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cfg_json, all_results, strategy_id = con.execute(
        "SELECT optimization_config, all_results, strategy_id "
        "FROM strategy_optimizations WHERE id = ?", (opt_id,)
    ).fetchone()
    cfg, res = json.loads(cfg_json), json.loads(all_results)

    # Same fitness-dedup selection the launcher's persist phase uses.
    seen, ranked = set(), []
    for r in sorted(res, key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9),
                    reverse=True):
        k = round(r["fitness"], 6)
        if k in seen:
            continue
        seen.add(k)
        ranked.append(r)
        if len(ranked) >= rank:
            break
    trial = ranked[rank - 1]

    import app.models  # noqa: F401
    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    db = SessionLocal()
    strat = db.query(Strategy).filter_by(id=strategy_id).first()
    bt_block = dict(cfg["backtest"])

    # Optional window override (BA2_START / BA2_END, YYYY-MM-DD). Lets a genome that was
    # optimized on one window be RE-MEASURED on another without re-optimizing -- e.g. scoring
    # the old 2026-06-30 winners on the 2025-12-31 window so their metrics are comparable with
    # runs made after the window change. Warmup is derived by the engine, so only the run
    # window moves.
    _ws, _we = os.environ.get("BA2_START"), os.environ.get("BA2_END")
    if _ws:
        bt_block["start_date"] = _ws
    if _we:
        bt_block["end_date"] = _we
    if _ws or _we:
        print(f"window override -> {bt_block['start_date']} .. {bt_block['end_date']}", flush=True)

    decoded = decode_params(strat, trial["params"])
    trial_cfg = _build_daily_trial_config(bt_block, decoded, {"backtest_cfg": bt_block})
    trial_cfg["name"] = f"DETERMINISM-{label}"
    # 4th arg "persistdb" reproduces the ONE thing _persist_top_backtests sets that a GA trial
    # does not: an on-disk trading DB instead of the RAM-only store.
    if len(sys.argv) > 4 and sys.argv[4] == "persistdb":
        trial_cfg["persist_trading_db"] = True

    _t0 = time.perf_counter()
    out = run_daily_backtest(trial_cfg)
    _elapsed = time.perf_counter() - _t0
    print(
        f"RESULT label={label} opt={opt_id} rank={rank} "
        f"hashseed={os.environ.get('PYTHONHASHSEED', 'random')} "
        f"GA_trades={trial.get('trades')} GA_fitness={trial['fitness']:.6f} "
        f"| elapsed={_elapsed:.1f}s trades={out.get('total_trades')} ann={out.get('annualized_return')} "
        f"total={out.get('total_return')} dd={out.get('max_drawdown')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
