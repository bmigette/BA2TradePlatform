"""Re-run ONE optimization's TOP-N genome and report P&L concentration (top-K trades as % of
net P&L), the same check that disqualified S5 TOP5 (96.9% of its net P&L came from one
never-exited position). Same cost/usage as run_genome_once.py -- one process, ~14-15GB peak RSS,
~15-28 min. Do not run alongside 3 other concurrent trials on this box.

Usage:  [BA2_START=YYYY-MM-DD] [BA2_END=YYYY-MM-DD] python tools/genome_concentration_check.py \\
            <opt_id> <rank> <label>

GROUPING. An optimization scored on an option CAR-family metric (option_car, option_car_over_risk,
option_car_target, option_car_target_soft30) had its concentration screened on option STRUCTURES
(legs of one transaction summed, a share lot joined with the overlays written against it -- see
strategy_fitness._option_structure_groups). This check groups the same way for those runs, so the
deploy-time number agrees with what the GA ranked on. Every other metric keeps the per-row
computation below, unchanged.
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

logging.disable(logging.WARNING)

DB = os.path.expanduser(r"~\Documents\ba2\test\dl_forecasting.db")


def main() -> int:
    opt_id, rank, label = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cfg_json, all_results, strategy_id, fitness_metric = con.execute(
        "SELECT optimization_config, all_results, strategy_id, fitness_metric "
        "FROM strategy_optimizations WHERE id = ?", (opt_id,)
    ).fetchone()
    cfg, res = json.loads(cfg_json), json.loads(all_results)

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

    _ws, _we = os.environ.get("BA2_START"), os.environ.get("BA2_END")
    if _ws:
        bt_block["start_date"] = _ws
    if _we:
        bt_block["end_date"] = _we
    if _ws or _we:
        print(f"window override -> {bt_block['start_date']} .. {bt_block['end_date']}", flush=True)

    decoded = decode_params(strat, trial["params"])
    trial_cfg = _build_daily_trial_config(bt_block, decoded, {"backtest_cfg": bt_block},
                                          option_trade_records=False)  # a report, not persisted
    trial_cfg["name"] = f"CONCENTRATION-{label}"

    _t0 = time.perf_counter()
    out = run_daily_backtest(trial_cfg)
    _elapsed = time.perf_counter() - _t0

    trades = out.get("trades") or []
    from app.services.strategy_fitness import _structure_pnls, scores_option_structures
    if scores_option_structures(fitness_metric):
        # The partition the GA's concentration screen used for this metric (see module doc).
        count_label = f"n_structures={{}} metric={fitness_metric}"
        pnls = sorted(_structure_pnls(trades, option_structures=True), reverse=True)
    else:
        count_label = "n_trades={}"
        pnls = sorted((float(t.get("pnl") or 0.0) for t in trades), reverse=True)
    net = sum(pnls)
    top1 = pnls[0] if pnls else 0.0
    top5 = sum(pnls[:5])
    top1_pct = (top1 / net * 100) if net else float("nan")
    top5_pct = (top5 / net * 100) if net else float("nan")

    print(
        f"RESULT label={label} opt={opt_id} rank={rank} "
        f"| elapsed={_elapsed:.1f}s trades={out.get('total_trades')} ann={out.get('annualized_return')} "
        f"total={out.get('total_return')} dd={out.get('max_drawdown')}",
        flush=True,
    )
    print(
        f"CONCENTRATION label={label} net_pnl={net:.2f} top1={top1:.2f} ({top1_pct:.1f}%) "
        f"top5={top5:.2f} ({top5_pct:.1f}%) " + count_label.format(len(pnls)),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
