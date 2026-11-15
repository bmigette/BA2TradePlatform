"""Walk-forward API: list / view runs (``walk_forward_runs``), preview + create + cancel + resume UI runs.

Creation goes through the CLI's path (app.services.wf_runs: ``wf_chain.CellSpec`` + ``preflight`` + the
chain in a driver process on the dedicated walk-forward queue). A refusal is HTTP 400 with the
refusal's own message as ``detail`` (the UI prints it verbatim); a name that exists is 409.
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import get_db
from app.models.walk_forward_run import WalkForwardRun
from app.services import walk_forward as WF
from app.services import wf_runs as R
from app.services.walk_forward import WalkForwardError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/walk-forward", tags=["walk-forward"])

#: Small columns only: the list never reads the report / spec blobs.
_LIGHT = ("id", "name", "source", "expert", "strategy", "band", "status", "planned_folds",
          "folds_done", "current_fold", "progress_message", "stitched_return", "worst_oos_dd",
          "stitched_trades", "error_message", "task_id", "created_at", "started_at", "updated_at",
          "completed_at")


def _iso(v):
    return v.isoformat() if v is not None else None


def _camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(p.capitalize() for p in rest)


def _light(values: Dict[str, Any]) -> Dict[str, Any]:
    return {_camel(k): (_iso(v) if k.endswith("_at") else v) for k, v in values.items()}


class RunRequest(BaseModel):
    """A walk-forward run: ``optimize_args`` are the exact ``ba2-test optimize`` tokens (what follows
    ``--`` on the CLI), without --start/--end/--name/--generations (the chain sets them per job)."""
    name: str
    optimize_args: List[str]
    train_start: str
    test_years: List[int]
    data_end: Optional[str] = None
    first_generations: int
    next_generations: int
    top_n: int
    test_parallel: int
    min_oos_return: float
    exclusive_return: bool
    max_dd_mult: float
    max_oos_dd: Optional[float] = None
    min_folds: int
    require_test_years: List[int] = []
    defer_last_test: bool
    run_all_folds: bool
    refit: bool
    floor: Optional[str] = None


class ResumeRequest(BaseModel):
    # Replace the stored data end (a two-phase run tests its deferred fold: send null). Omit to keep.
    data_end: Optional[str] = None
    change_data_end: bool = False


def _refused(e: WalkForwardError, status: int = 400):
    return HTTPException(status_code=status, detail=str(e))


@router.get("/options")
def options():
    """What the create form offers: the launcher's experts and strategies, the data sources, the
    fitness catalog, and the prefill (goal2027 grid values; the engine itself has no defaults)."""
    from ba2_common.core.estimates_source import ESTIMATES_SOURCES
    from ba2_common.core.ohlcv_history import DEFAULT_BT_HISTORY_SOURCE, HISTORY_SOURCES
    from app.services.strategy_fitness import METRICS_CATALOG
    L = R.launcher_module()
    return {
        "experts": sorted(L._EXPERT_OPT),
        "strategies": sorted(L._STRATEGY_BUILDERS),
        "optionStrategies": sorted(L._PURE_OPTION_STRATEGIES),
        "estimatesSources": list(ESTIMATES_SOURCES),
        "historySources": sorted(HISTORY_SOURCES),
        "defaultHistorySource": DEFAULT_BT_HISTORY_SOURCE,
        "capBands": ["small", "mid", "large"],
        "fitness": [m["key"] for m in METRICS_CATALOG],
        "forbiddenFlags": WF.FORBIDDEN_OPTIMIZE_FLAGS,
        "chainSetFlags": ["--start", "--end", "--name", "--generations"],
        "prefill": R.PREFILL,
        "reportDir": R.default_report_dir(),
    }


@router.post("/preview")
def preview(req: RunRequest, db: Session = Depends(get_db)):
    """Validate a run exactly like create and return its folds and exact commands; nothing is created."""
    try:
        spec = R.spec_from_request(req.model_dump())
        plan = R.preflight(spec)
    except WalkForwardError as e:
        raise _refused(e)
    exists = db.query(WalkForwardRun.id).filter(WalkForwardRun.name == spec.name).first()
    return {**plan, "existingJobs": R.existing_jobs(db, plan["jobs"]),
            "nameTaken": exists is not None}


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    cols = [getattr(WalkForwardRun, c) for c in _LIGHT]
    rows = db.query(*cols).order_by(WalkForwardRun.id.desc()).all()
    return {"runs": [_light(dict(zip(_LIGHT, r))) for r in rows]}


@router.post("/runs")
def create_run(req: RunRequest, db: Session = Depends(get_db)):
    try:
        row = R.create_run(db, req.model_dump())
    except WalkForwardError as e:
        raise _refused(e, 409 if "already exists" in str(e) else 400)
    logger.info(f"walk-forward run {row.name!r} created (id {row.id}, task {row.task_id})")
    return _detail(db, row)


def _row(db, run_id: int) -> WalkForwardRun:
    row = db.query(WalkForwardRun).filter(WalkForwardRun.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"walk-forward run {run_id} not found")
    return row


def _jobs(db, report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The train / refit optimizations of the run that exist in THIS DB, by job name. A job the report
    recorded with another optimization id is another DB's row of the same name: not linked."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    recorded = {f["train_job"]: f.get("train_optimization_id") for f in report.get("folds", [])}
    names = list(recorded) + [j for j in (report.get("refit_job"), report.get("refit_running")) if j]
    rows = (db.query(SO.id, SO.name, SO.status, SO.progress, SO.best_fitness, SO.started_at,
                     SO.completed_at).filter(SO.name.in_(names)).order_by(SO.id).all())
    out: Dict[str, Dict[str, Any]] = {}
    for i, n, st, pr, bf, sa, ca in rows:
        if recorded.get(n) is not None and recorded[n] != i:
            continue
        out[n] = {"id": i, "status": st, "progress": pr, "bestFitness": bf, "startedAt": _iso(sa),
                  "completedAt": _iso(ca)}
    return out


def _backtests(db, report: Dict[str, Any]) -> Dict[int, bool]:
    """Which out-of-sample backtest ids of the report are the SAME rows in this DB: the id must exist
    with the name the engine gives that fold/rank (an imported report's ids belong to another DB)."""
    from app.models.backtest import Backtest
    want = {}
    for f in report.get("folds", []):
        for g in f.get("ranks", []):
            if g.get("oos_backtest_id") is not None:
                want[int(g["oos_backtest_id"])] = WF.oos_backtest_name(f["index"], g["rank"],
                                                                       f["train_job"])
    if not want:
        return {}
    rows = db.query(Backtest.id, Backtest.name).filter(Backtest.id.in_(list(want))).all()
    have = {i: n for i, n in rows}
    return {i: have.get(i) == name for i, name in want.items()}


def _log_tail(row, lines: int = 80) -> Optional[List[str]]:
    path = R.driver_log_path(row)
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 64 * 1024))
        text = fh.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def _detail(db, row: WalkForwardRun) -> Dict[str, Any]:
    out = _light({c: getattr(row, c) for c in _LIGHT})
    report = row.report or {"folds": []}
    jobs = _jobs(db, report)
    task = R.task_state(row.task_id)
    driver = R.driver_state(row) if row.status in ("pending", "running") else None
    live = None
    if row.status == "running":
        k = row.current_fold
        job = (next((f["train_job"] for f in report["folds"] if f["index"] == k), None)
               if k else report.get("refit_running"))
        info = jobs.get(job) if job else None
        gens = None
        if k:
            gens = next((f.get("generations") for f in report["folds"] if f["index"] == k), None)
        elif job:
            gens = (row.spec or {}).get("next_generations")
        gen = None
        if info and info["progress"] is not None and gens:
            gen = min(int(gens), round(float(info["progress"]) / 100.0 * int(gens)))
        live = {"fold": k, "job": job, "optimization": info,
                "generation": gen, "generations": int(gens) if gens else None,
                "phase": (None if info is None else
                          "training" if info["status"] in ("pending", "running") else
                          "testing" if info["status"] == "completed" and k else info["status"])}
    interrupted = (row.status in ("pending", "running") and task not in ("queued", "pending", "running")
                   and driver in ("dead", None) and row.source == "ui")
    out.update({
        "spec": row.spec, "report": report, "reportDir": row.report_dir,
        "driverHost": row.driver_host, "driverPid": row.driver_pid, "driverState": driver,
        "taskStatus": task, "jobs": jobs, "backtests": _backtests(db, report), "live": live,
        "interrupted": interrupted,
        "canCancel": row.source == "ui" and row.status in ("pending", "running") and not interrupted,
        "canResume": row.source == "ui" and (row.status in ("error", "cancelled", "deferred-pending")
                                             or interrupted),
        "logTail": _log_tail(row),
    })
    return out


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    return _detail(db, _row(db, run_id))


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: int, db: Session = Depends(get_db)):
    row = _row(db, run_id)
    try:
        R.cancel_run(db, row)
    except WalkForwardError as e:
        raise _refused(e, 409)
    return _detail(db, row)


@router.post("/runs/{run_id}/resume")
def resume(run_id: int, req: Optional[ResumeRequest] = None, db: Session = Depends(get_db)):
    row = _row(db, run_id)
    try:
        R.resume_run(db, row, data_end=(req.data_end if req is not None and req.change_data_end
                                        else "keep"))
    except WalkForwardError as e:
        raise _refused(e, 409 if "still queued or running" in str(e) else 400)
    return _detail(db, row)


@router.get("/runs/{run_id}/oos-equity")
def oos_equity(run_id: int, db: Session = Depends(get_db)):
    """The stitched out-of-sample equity of the fold winners: each done fold's rank-1 out-of-sample
    backtest curve (when that row is in this DB), chained so fold k+1 starts where fold k ended."""
    from app.models.backtest import Backtest, _downsample_curves
    row = _row(db, run_id)
    report = row.report or {"folds": []}
    valid = _backtests(db, report)
    points: List[Dict[str, Any]] = []
    folds: List[Dict[str, Any]] = []
    missing: List[str] = []
    level: Optional[float] = None
    for f in report["folds"]:
        if f["state"] != "done":
            continue
        g = next((x for x in f["ranks"] if x["rank"] == 1), None)
        bid = None if g is None else g.get("oos_backtest_id")
        if bid is None or not valid.get(int(bid)):
            missing.append(f"fold {f['index']}")
            continue
        r = db.query(Backtest.equity_curve, Backtest.initial_capital).filter(Backtest.id == bid).first()
        curve = [p for p in (r[0] or []) if isinstance(p, dict) and isinstance(p.get("equity"), (int, float))]
        if not curve:
            missing.append(f"fold {f['index']} (backtest {bid} has no equity curve)")
            continue
        curve, _ = _downsample_curves(curve, [], target=800)
        first = float(curve[0]["equity"])
        if first <= 0:
            missing.append(f"fold {f['index']} (non-positive starting equity)")
            continue
        level = float(r[1]) if level is None else level
        scale = level / first
        for p in curve:
            points.append({"date": p.get("date"), "equity": float(p["equity"]) * scale,
                           "fold": f["index"]})
        level = float(curve[-1]["equity"]) * scale
        folds.append({"fold": f["index"], "backtestId": int(bid), "start": f["test_start"],
                      "end": f["test_end"]})
    return {"available": bool(points) and not missing, "missing": missing, "folds": folds,
            "points": points if not missing else []}
