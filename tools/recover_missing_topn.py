"""Recover missing TOP-N rows of completed optimizations by re-running them on ONE remote worker.

usage: python recover_missing_topn.py <worker_name> <opt_id>:<rank>[,<rank>...] [<opt_id>:<ranks> ...]

Mirrors ba2test_launcher._persist_top_backtests per rank: same distinct-fitness ranking, same
decode/_build_daily_trial_config/hoisted state, same /submit-trial-full payload, same master-side
persist (_persist_results + ga_fitness + push_backtest). Differences, on purpose:
  * polls /job-status WITHOUT the `bars` heartbeat check — _persist_trial_worker never updates
    ctl["bars"], so worker_client._submit_and_poll cancels any >420 s re-run as "never started";
  * submits only when the worker reports >= MIN_FREE free slots, so it never queues behind GA trials;
  * skips a rank whose TOP<rank>-<name> row already exists (safe to re-run the script).
"""
import json
import sys
import time
from datetime import datetime as _dt

MIN_FREE = 2
MIN_AVAIL_MB = 14000   # a DeterministicScorer re-run peaks ~10 GB; leave headroom for the grid
MAX_WAIT_S = 8 * 3600
POLL_S = 20
TRIAL_TIMEOUT_S = 5400

WORKER_NAME = sys.argv[1]
QUEUE = []
for spec in sys.argv[2:]:
    oid, ranks = spec.split(":")
    QUEUE += [(int(oid), int(r)) for r in ranks.split(",")]

sys.path.insert(0, r"C:\Users\basti\Documents\dev\BA2TradePlatform\testplatform")
import ba2test_launcher as L  # noqa: E402

L._enter_backend()

import httpx  # noqa: E402
import app.models  # noqa: F401,E402
from app.models.database import SessionLocal  # noqa: E402
from app.models.backtest import Backtest  # noqa: E402
from app.models.strategy import Strategy  # noqa: E402
from app.models.strategy_optimization import StrategyOptimization  # noqa: E402
from app.services.strategy_optimization_handler import (  # noqa: E402
    _build_daily_trial_config, _build_hoisted_state, _resolve_workers,
)
from app.services.backtest.daily_backtest_handler import _persist_results  # noqa: E402
from app.services.strategy_param_space import decode_params  # noqa: E402
from app.services.sync_client import push_backtest  # noqa: E402
from app.services import worker_client  # noqa: E402
from ba2_common.config import CACHE_FOLDER  # noqa: E402
from app.services.backtest.backtest_db import _inmem_trades_enabled  # noqa: E402

import logging as _logging  # noqa: E402
_logging.disable(_logging.INFO)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def build_spec(db, opt_id, rank):
    """(expert, name, trial_cfg, strategy_params, bt_block, fitness_metric, worker) for one rank."""
    opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    strat = db.query(Strategy).filter_by(id=opt.strategy_id).first()
    cfg = opt.optimization_config or {}
    bt_block = dict(cfg["backtest"])
    expert = next((s["class"] for s in (bt_block.get("experts") or []) if isinstance(s, dict) and s.get("class")), None)
    assert expert, f"opt {opt_id}: no expert in optimization_config"
    name = f"TOP{rank}-{opt.name or expert}"
    hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None
    fixed = {}
    for s in (bt_block.get("experts") or []):
        if isinstance(s, dict) and s.get("class") == expert:
            fixed = dict(s.get("settings") or {})
            break
    seen, ranked = set(), []
    for r in sorted(opt.all_results or [], key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9), reverse=True):
        fit = r.get("fitness")
        key = round(fit, 6) if isinstance(fit, (int, float)) else json.dumps(r.get("params"), sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        ranked.append((r["params"], r.get("key"), fit))
        if len(ranked) >= rank:
            break
    assert len(ranked) == rank, f"opt {opt_id}: only {len(ranked)} distinct-fitness individuals, no rank {rank}"
    params, _key, ga_fitness = ranked[rank - 1]
    decoded = decode_params(strat, params)
    trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
    trial_cfg["name"] = name
    trial_cfg["persist_trading_db"] = True
    trial_cfg["ga_fitness"] = ga_fitness
    sp = dict(params)
    if fixed:
        sp["expertFixedSettings"] = fixed
    if decoded.get("entry_rules") is not None:
        sp["entryRules"] = decoded["entry_rules"]
    if decoded.get("exit_rules") is not None:
        sp["exitRules"] = decoded["exit_rules"]
    workers = [w for w in _resolve_workers(db, opt.worker_ids) if w["name"] == WORKER_NAME]
    assert workers, f"opt {opt_id}: {WORKER_NAME} not in worker_ids={opt.worker_ids}"
    return expert, name, trial_cfg, sp, bt_block, (opt.fitness_metric or "consistent_annual_return"), workers[0], ga_fitness


def wait_for_gap(worker):
    deadline = time.time() + MAX_WAIT_S
    last = None
    while True:
        try:
            h = worker_client.health(worker, timeout=10)
            state = (h.get("free"), h.get("busy"), h.get("capacity"))
        except Exception as e:  # noqa: BLE001
            state = (None, None, f"unreachable {type(e).__name__}")
        if state != last:
            log(f"  worker free={state[0]} busy={state[1]} capacity={state[2]}")
            last = state
        if isinstance(state[0], int) and state[0] >= MIN_FREE:
            # The worker refuses submissions under its memory floor ("worker under memory floor
            # (7.4% free)" — opt 363 on remote150 beside 10 GB DeterministicScorer trials), and a
            # free slot says nothing about RAM. Require MIN_AVAIL_MB before submitting.
            try:
                m = worker_client.memory(worker, timeout=15)
                avail = int(m.get("system_available_mb") or 0)
            except Exception as e:  # noqa: BLE001
                avail = -1
                log(f"  memory probe failed ({type(e).__name__}); waiting")
            if avail >= MIN_AVAIL_MB:
                return
            log(f"  worker has {avail} MB available (< {MIN_AVAIL_MB}); waiting")
            time.sleep(POLL_S)
            continue
        if time.time() > deadline:
            raise TimeoutError("no free slot within the wait budget")
        time.sleep(POLL_S)


def run_remote(worker, trial_cfg, fitness_metric):
    payload = {"config": trial_cfg, "fitness_metric": fitness_metric, "cache_root": CACHE_FOLDER,
               "inmem_trades": _inmem_trades_enabled()}
    t0 = time.time()
    with httpx.Client(timeout=30.0) as cl:
        r = cl.post(f"{worker_client._base(worker)}/submit-trial-full",
                    headers=worker_client._headers(worker), json=payload)
        r.raise_for_status()
        job_id = r.json()["job_id"]
    log(f"  accepted as job {job_id}; polling (heartbeat check bypassed, max {TRIAL_TIMEOUT_S}s)")
    url = f"{worker_client._base(worker)}/job-status/{job_id}"
    last = None
    with httpx.Client(timeout=30.0) as cl:
        while time.time() - t0 < TRIAL_TIMEOUT_S:
            time.sleep(5)
            try:
                r = cl.get(url, headers=worker_client._headers(worker))
            except Exception as e:  # noqa: BLE001
                log(f"  poll error {type(e).__name__}; retrying")
                continue
            if r.status_code == 404:
                return None, f"worker forgot job {job_id} (restarted?)"
            body = r.json()
            state = (body.get("status"), body.get("bars"), body.get("started"))
            if state != last:
                log(f"  status={state[0]} bars={state[1]} started={state[2]} t+{time.time()-t0:.0f}s")
                last = state
            if body.get("status") == "done":
                return body.get("result"), None
    worker_client.cancel_job(worker, job_id)
    return None, f"timed out after {TRIAL_TIMEOUT_S}s (cancelled)"


def persist(db, opt_id, expert, name, trial_cfg, sp, bt_block, fitness_metric, out):
    bt = Backtest(
        name=name, model_id=None, engine_type="daily_expert",
        expert_name=expert, optimization_id=opt_id,
        labels=bt_block.get("labels") or None,
        strategy_params=sp,
        start_date=_dt.fromisoformat(str(bt_block["start_date"])),
        end_date=_dt.fromisoformat(str(bt_block["end_date"])),
        initial_capital=float(bt_block["initial_capital"]),
        status="running", started_at=_dt.now(),
    )
    db.add(bt); db.commit(); db.refresh(bt)
    try:
        from app.services.strategy_fitness import compute_fitness as _cf
        _cf(fitness_metric, out["results"])
    except Exception as e:  # noqa: BLE001
        log(f"  fitness annotation failed: {e!r}")
    _persist_results(db, bt, out["results"])
    if trial_cfg.get("ga_fitness") is not None:
        bt.ga_fitness = float(trial_cfg["ga_fitness"])
    bt.status = "completed"; bt.completed_at = _dt.now()
    bt.is_saved = True
    db.commit()
    push_backtest(bt, db)
    return bt


log(f"queue for {WORKER_NAME}: {QUEUE}")
ok, failed = [], []
for opt_id, rank in QUEUE:
    db = SessionLocal()
    try:
        expert, name, trial_cfg, sp, bt_block, fm, worker, gaf = build_spec(db, opt_id, rank)
        if db.query(Backtest).filter(Backtest.optimization_id == opt_id, Backtest.name == name).first():
            log(f"SKIP opt {opt_id} rank {rank}: {name} already exists")
            continue
        log(f"START opt {opt_id} rank {rank} {name} ga_fitness={gaf}")
        wait_for_gap(worker)
        out, err = run_remote(worker, trial_cfg, fm)
        if err or not out or not out.get("ok"):
            msg = err or (out or {}).get("error", "no result")
            log(f"FAILED opt {opt_id} rank {rank}: {msg}")
            failed.append((opt_id, rank, msg))
            continue
        bt = persist(db, opt_id, expert, name, trial_cfg, sp, bt_block, fm, out)
        log(f"PERSISTED opt {opt_id} rank {rank}: backtest id={bt.id} total_return={bt.total_return} "
            f"annualized={bt.annualized_return} max_dd={bt.max_drawdown} trades={bt.total_trades}")
        ok.append((opt_id, rank, bt.id))
    except Exception as e:  # noqa: BLE001
        log(f"FAILED opt {opt_id} rank {rank}: {e!r}")
        failed.append((opt_id, rank, repr(e)))
    finally:
        db.close()
log(f"RESULT: {len(ok)} persisted {ok}; {len(failed)} failed {failed}")
