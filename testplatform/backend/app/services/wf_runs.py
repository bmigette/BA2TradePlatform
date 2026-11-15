"""Walk-forward runs started from the UI: create, validate, run in the background, cancel, resume.

SAME PATH AS THE CLI. A run is a ``wf_chain.CellSpec`` (the cell spec the goal2027 matrix driver builds)
whose ``optimize_args`` are the exact ``ba2-test optimize`` tokens (what follows ``--`` on the CLI). It
is validated by ``preflight``: ``wf_chain.make_cell_plan`` (``check_user_optimize_args``, the fold
validation, the thresholds), the launcher's estimates-source rule (``_resolve_estimates_source`` and
``_refuse_reused_estimates_source_name`` per job name) and its option holdout rail
(``_assert_option_window_excludes_holdout`` on every train window, as tools/run_walk_forward.py does,
and on the refit window). It runs as ``wf_chain.run_cell`` with the real runner
(tools/run_walk_forward.py ``RealRunner``) in a driver PROCESS (tools/run_wf_run.py ->
``drive``), which the task-queue handler ``handle_walk_forward`` starts and watches on the dedicated
walk-forward queue: the train jobs are ``ba2-test optimize`` subprocesses exactly as in the grid, and
the out-of-sample re-runs touch launcher globals and the logging state, so they never run inside the
API server's process.

CANCEL kills the driver's process tree. The killed train job is left as an interrupted optimization,
which the chain's resume (``optimize --resume-interrupted``, app.services.optimization_resume) continues
from its last checkpoint when the run is RESUMED; completed folds are reused from their stored reports.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.services import walk_forward as WF
from app.services import wf_chain as C
from app.services import wf_store as S
from app.services.walk_forward import WalkForwardError

logger = logging.getLogger(__name__)

TASK_TYPE = "walk_forward"

#: The repo checkout this backend belongs to (testplatform/backend/app/services -> repo root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))
DRIVER_SCRIPT = os.path.join(REPO_ROOT, "tools", "run_wf_run.py")
LAUNCHER_SCRIPT = os.path.join(REPO_ROOT, "testplatform", "ba2test_launcher.py")

#: Form prefill, NOT engine defaults: the walk-forward engine has no default thresholds (a verdict is
#: only as honest as its thresholds, walk_forward.Thresholds). These are the values the goal2027 grid
#: runs with (tools/grid_goal2027.sh), shown so a new run starts from a known reference.
PREFILL = {
    "train_start": "2020-01-01", "test_years": [2024, 2025, 2026], "first_generations": 30,
    "next_generations": 20, "top_n": 3, "test_parallel": 1, "min_oos_return": 0.0,
    "exclusive_return": True, "max_dd_mult": 2.5, "max_oos_dd": 35.0, "min_folds": 2,
    "require_test_years": [2026], "defer_last_test": False, "run_all_folds": False, "refit": True,
    "population": 300, "fitness": "consistent_annual_return", "interval": "5min", "early_stop": 5,
    "early_stop_min_rel": 0.01, "parallel": 4,
}


def default_report_dir() -> str:
    from ba2_common.config import TEST_DIR
    return os.path.join(TEST_DIR, "walk_forward")


def launcher_module():
    """The ``ba2test_launcher`` module of THIS checkout (already imported under ``ba2-test serve``)."""
    if "ba2test_launcher" in sys.modules:
        return sys.modules["ba2test_launcher"]
    tp = os.path.join(REPO_ROOT, "testplatform")
    if tp not in sys.path:
        sys.path.insert(0, tp)
    import ba2test_launcher
    return ba2test_launcher


def selection_defaults() -> Dict[str, Any]:
    """persist_distinct_topn's declared selection defaults (as tools/run_walk_forward.py reads them)."""
    tools = os.path.join(REPO_ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import run_walk_forward as RWF
    return RWF.selection_defaults()


# --------------------------------------------------------------------------------- request -> spec


def _iso(v: Optional[str], what: str) -> Optional[date]:
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise WalkForwardError(f"{what} {v!r} is not an ISO date (YYYY-MM-DD)") from e


def _need(req: Dict[str, Any], key: str) -> Any:
    """A required request field: absent -> refused (no defaults, backend/CLAUDE.md)."""
    if req.get(key) is None or req.get(key) == "":
        raise WalkForwardError(f"{key} is required")
    return req[key]


def spec_from_request(req: Dict[str, Any], launcher: Any = None) -> C.CellSpec:
    """The ``CellSpec`` of a create request. ``optimize_args`` = the ``ba2-test optimize`` tokens."""
    args = _need(req, "optimize_args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise WalkForwardError("optimize_args must be a list of strings (the ba2-test optimize tokens)")
    years = _need(req, "test_years")
    try:
        years = [int(y) for y in years]
    except (TypeError, ValueError) as e:
        raise WalkForwardError(f"test_years {years!r} is not a list of years") from e
    max_oos_dd = req.get("max_oos_dd")
    try:
        th = WF.Thresholds(float(_need(req, "min_oos_return")), float(_need(req, "max_dd_mult")),
                           int(_need(req, "min_folds")),
                           None if max_oos_dd in (None, "") else float(max_oos_dd))
        ints = {k: int(_need(req, k)) for k in ("first_generations", "next_generations", "top_n",
                                                 "test_parallel")}
    except (TypeError, ValueError) as e:
        raise WalkForwardError(f"a numeric field is not a number: {e}") from e
    for k, v in ints.items():
        if v < 1:
            raise WalkForwardError(f"{k} must be >= 1, got {v}")
    expert = WF.optimize_arg_value(args, "--expert")
    return C.CellSpec(
        name=str(_need(req, "name")).strip(), optimize_args=list(args),
        launcher=launcher or [sys.executable, LAUNCHER_SCRIPT],
        train_start=_iso(_need(req, "train_start"), "train_start"), test_years=years,
        data_end=_iso(req.get("data_end"), "data_end"),
        first_generations=ints["first_generations"], next_generations=ints["next_generations"],
        top_n=ints["top_n"], thresholds=th,
        require_test_years=tuple(int(y) for y in (req.get("require_test_years") or [])),
        test_parallel=ints["test_parallel"], selection=selection_defaults(),
        expert=expert, strategy=WF.optimize_arg_value(args, "--strategy"),
        band=WF.optimize_arg_value(args, "--screener-cap-band")
        or WF.optimize_arg_value(args, "--screen-preset"),
        floor=_iso(req.get("floor"), "floor"),
        exclusive_return=_flag(req, "exclusive_return"), defer_last_test=_flag(req, "defer_last_test"),
        run_all_folds=_flag(req, "run_all_folds"), refit=_flag(req, "refit"))


def _flag(req: Dict[str, Any], key: str) -> bool:
    """A required yes/no field (stated, never defaulted)."""
    if not isinstance(req.get(key), bool):
        raise WalkForwardError(f"{key} is required (true or false)")
    return req[key]


# --------------------------------------------------------------------------------- validation


def _launcher_refusal(fn: Callable, *a) -> None:
    """Call a launcher rail that refuses with ``sys.exit(message)``; a refusal becomes a
    ``WalkForwardError`` carrying the launcher's own message."""
    try:
        fn(*a)
    except SystemExit as e:
        raise WalkForwardError(str(e.code)) from None


#: Flags ``ba2-test optimize`` requires (argparse ``required=True``) that the engine does not set.
REQUIRED_OPTIMIZE_FLAGS = ("--expert", "--universe")


def preflight(spec: C.CellSpec, L: Any = None) -> Dict[str, Any]:
    """Validate a run BEFORE anything is created; returns the plan as data (folds, the exact train
    commands, the refit command). Raises ``WalkForwardError`` with the refusal's own message."""
    if not spec.name:
        raise WalkForwardError("name is required")
    plan, folds = C.make_cell_plan(spec)
    for flag in REQUIRED_OPTIMIZE_FLAGS:
        if WF.optimize_arg_value(spec.optimize_args, flag) is None:
            raise WalkForwardError(f"optimize args lack {flag} (ba2-test optimize requires it)")
    L = L or launcher_module()
    source = WF.optimize_arg_value(spec.optimize_args, "--estimates-source")
    _launcher_refusal(L._resolve_estimates_source, SimpleNamespace(estimates_source=source),
                      "walk-forward")
    deferred = C.deferred_fold(folds, spec.data_end, spec.defer_last_test)
    jobs = [WF.train_job_name(spec.name, f.index) for f in folds]
    if spec.strategy:
        for f in folds:
            _launcher_refusal(L._assert_option_window_excludes_holdout, [spec.strategy],
                              f.train_end.isoformat())
        if spec.refit and deferred is None:
            _launcher_refusal(L._assert_option_window_excludes_holdout, [spec.strategy],
                              C.refit_window(folds)[1].isoformat())
    if spec.refit:
        jobs.append(C.refit_job_name(spec.name))
    for job in jobs:
        _launcher_refusal(L._refuse_reused_estimates_source_name, job, source, "walk-forward")
    return {
        "folds": [{**S.fold_window(f),
                   "generations": plan.fold_extra_args[f.index][1],
                   "deferred": f is deferred,
                   "train_job": WF.train_job_name(spec.name, f.index),
                   "command": [str(t) for t in plan.command_for(f)]} for f in folds],
        "refit": None if not spec.refit or deferred is not None else {
            "job": C.refit_job_name(spec.name),
            "train_start": C.refit_window(folds)[0].isoformat(),
            "train_end": C.refit_window(folds)[1].isoformat(),
            "command": [str(t) for t in C.refit_command(spec, folds)]},
        "deferred_fold": deferred.index if deferred else None,
        "jobs": jobs,
    }


def existing_jobs(db, names: Sequence[str]) -> List[Dict[str, Any]]:
    """Optimizations already carrying one of these job names (the chain REUSES a completed one)."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    rows = (db.query(SO.id, SO.name, SO.status).filter(SO.name.in_(list(names)))
            .order_by(SO.id).all())
    return [{"id": i, "name": n, "status": st} for i, n, st in rows]


# --------------------------------------------------------------------------------- create / resume


def _queue(name: str, run_id: int, token: str) -> str:
    from app.services.task_queue import get_wf_task_queue
    return get_wf_task_queue().queue_task(
        task_type=TASK_TYPE, name=f"Walk-forward {name}", payload={"run_id": run_id, "token": token},
        description=f"walk-forward chain {name}", max_retries=0, timeout_seconds=30 * 24 * 3600)


def create_run(db, req: Dict[str, Any], L: Any = None) -> Any:
    """Validate (``preflight``), insert the row (status pending, source ui) and queue its task."""
    from app.models.walk_forward_run import WalkForwardRun
    spec = spec_from_request(req)
    preflight(spec, L)
    if name_taken(db, spec.name):
        raise WalkForwardError(f"a walk-forward run named {spec.name!r} already exists; resume it "
                               f"or choose another name")
    token = uuid.uuid4().hex
    row = WalkForwardRun(name=spec.name, lane_key=ui_lane(), source="ui", status="pending",
                         expert=spec.expert,
                         strategy=spec.strategy, band=spec.band,
                         spec={**S.spec_to_json(spec), "name": spec.name},
                         planned_folds=len(C.make_cell_plan(spec)[1]), folds_done=0,
                         driver_token=token, report_dir=default_report_dir(),
                         progress_message="queued", created_at=datetime.now(),
                         updated_at=datetime.now())
    db.add(row)
    db.commit()
    db.refresh(row)
    row.task_id = _queue(spec.name, row.id, token)
    db.commit()
    db.refresh(row)
    return row


def ui_lane() -> str:
    """The lane (report directory) every UI run lives in."""
    from app.models.walk_forward_run import lane_key
    return lane_key(default_report_dir())


def name_taken(db, name: str) -> bool:
    """A UI run of this name exists already (rows of other lanes, e.g. the goal2027 grid's cell of the
    same name, are other chains; their train job NAMES are still shared, see ``existing_jobs``)."""
    from app.models.walk_forward_run import WalkForwardRun
    return db.query(WalkForwardRun.id).filter(WalkForwardRun.name == name,
                                              WalkForwardRun.lane_key == ui_lane()).first() is not None


def task_state(task_id: Optional[str]) -> Optional[str]:
    if not task_id:
        return None
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue
    db = SessionLocal()
    try:
        r = db.query(TaskQueue.status).filter(TaskQueue.task_id == task_id).first()
        return r[0] if r else None
    finally:
        db.close()


def driver_state(row) -> Optional[str]:
    """alive | dead | unknown | foreign of the row's recorded driver, None when none is recorded."""
    if row.driver_pid is None:
        return None
    from app.services.optimization_resume import judge
    return judge(row.driver_host, row.driver_pid, row.driver_pid_started, row.driver_identity)[0]


def is_live(row) -> bool:
    """A run that is queued or whose driver is alive (or cannot be proven dead)."""
    if task_state(row.task_id) in ("queued", "pending"):
        return True
    if row.status not in ("pending", "running"):
        return False
    return driver_state(row) not in ("dead", None) or task_state(row.task_id) == "running"


def resume_run(db, row, data_end: Any = "keep", L: Any = None) -> Any:
    """Queue the run again: the chain skips completed train jobs, reuses stored fold reports and
    continues an interrupted train job from its checkpoint. ``data_end`` replaces the stored data end
    (a two-phase run tests its deferred fold once the data exists: give None)."""
    if row.source != "ui":
        raise WalkForwardError(
            "only runs created in the UI are resumed here"
            + (": this one was imported from a report directory (no optimize args)"
               if row.source == "import" else
               ": this one is driven by a CLI driver (the goal2027 grid); rerun its command"))
    if is_live(row):
        raise WalkForwardError(f"walk-forward run {row.name!r} is still queued or running")
    spec_json = dict(row.spec)
    if data_end != "keep":
        spec_json["data_end"] = data_end or None
    spec = S.spec_from_json(spec_json, [sys.executable, LAUNCHER_SCRIPT])
    preflight(spec, L)
    token = uuid.uuid4().hex
    row.spec = {**S.spec_to_json(spec), "name": spec.name}
    row.status, row.driver_token, row.error_message = "pending", token, None
    row.progress_message, row.completed_at, row.updated_at = "queued (resume)", None, datetime.now()
    db.commit()
    row.task_id = _queue(row.name, row.id, token)
    db.commit()
    db.refresh(row)
    return row


def kill_tree(pid: int) -> None:
    """Terminate ``pid`` and every process below it (the driver, its train job, the GA pool)."""
    import psutil
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as e:
        logger.error(f"walk-forward: cannot list the process tree of pid {pid} ({e}); killing it alone")
        procs = [root]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as e:
            logger.error(f"walk-forward: no permission to terminate pid {p.pid} ({e})")
    _gone, alive = psutil.wait_procs(procs, timeout=10)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as e:
            logger.error(f"walk-forward: no permission to kill pid {p.pid} ({e}); it may still run")


def _mark_if_owned(run_id: int, token: str, status: str, message: str) -> bool:
    """Set the row's status when THIS driver token still owns it (a newer resume may own it now)."""
    from app.models.database import SessionLocal
    from app.models.walk_forward_run import WalkForwardRun
    db = SessionLocal()
    try:
        row = db.query(WalkForwardRun).filter(WalkForwardRun.id == run_id).first()
        if row is None or row.driver_token != token or row.status not in ("pending", "running"):
            return False
        row.status = status
        row.progress_message = message[:500]
        if status == "error":
            row.error_message = message
        row.completed_at = row.updated_at = datetime.now()
        db.commit()
        return True
    finally:
        db.close()


def cancel_run(db, row) -> Any:
    """Stop a UI run: through its task when the handler watches it (the handler kills the driver's
    process tree), else the recorded driver's tree when it is provably this host's live process."""
    from app.services.task_queue import get_wf_task_queue
    if row.source != "ui":
        raise WalkForwardError("only runs created in the UI are cancelled here (a CLI run is "
                               "stopped where its driver runs)")
    if row.status not in ("pending", "running"):
        raise WalkForwardError(f"walk-forward run {row.name!r} is {row.status}, not running")
    ts = task_state(row.task_id)
    if ts in ("queued", "pending", "running"):
        get_wf_task_queue().cancel_task(row.task_id)
    if ts != "running":
        # not watched by a handler (queued, or orphaned by a server restart): stop it here
        state = driver_state(row)
        if state == "alive":
            kill_tree(row.driver_pid)
        elif state in ("unknown", "foreign"):
            raise WalkForwardError(f"the run's driver (pid {row.driver_pid} on {row.driver_host}) "
                                   f"is {state}: not killed from here")
        _mark_if_owned(row.id, row.driver_token, "cancelled", "cancelled")
    db.refresh(row)
    return row


# --------------------------------------------------------------------------------- the driver


def drive(run_id: int, token: str, runner: Any, *, task_id: Optional[str] = None,
          L: Any = None, log: Callable[[str], None] = print,
          session_factory: Optional[Callable] = None) -> C.CellResult:
    """Run (or resume) the stored run ``run_id`` with ``runner`` (the real one in tools/run_wf_run.py):
    the stored spec, re-validated by ``preflight``, through ``wf_chain.run_cell``. The runner must
    offer ``wf_store`` (the chain then keeps the row current); its source / token / task id are set
    here so the row records which driver owns it."""
    from app.models.walk_forward_run import WalkForwardRun
    Sf = S._session_factory(session_factory)
    db = Sf()
    try:
        row = db.query(WalkForwardRun).filter(WalkForwardRun.id == run_id).first()
        if row is None:
            raise WalkForwardError(f"walk-forward run {run_id} does not exist")
        if row.driver_token != token:
            raise WalkForwardError(f"walk-forward run {row.name!r} is owned by another start "
                                   f"(token mismatch): this driver does not run it")
        if not row.report_dir:
            raise WalkForwardError(f"walk-forward run {row.name!r} records no report directory")
        # The row's report directory IS its lane (one row per name and report directory): the chain
        # always runs there, so its files, its .lock and its row stay one chain.
        spec_json, out_dir = dict(row.spec), row.report_dir
    finally:
        db.close()
    if not callable(getattr(runner, "wf_store", None)):
        raise WalkForwardError("the runner offers no wf_store: the run's row would not be kept")
    spec = S.spec_from_json(spec_json, [sys.executable, LAUNCHER_SCRIPT])
    preflight(spec, L if L is not None else getattr(runner, "launcher_module", launcher_module)())
    runner.wf_source, runner.wf_token, runner.wf_task_id = "ui", token, task_id
    return C.run_cell(spec, runner, out_dir, log)


def driver_log_path(row) -> Optional[str]:
    if not row.report_dir:
        return None
    return os.path.join(row.report_dir, row.name, "driver.log")


def handle_walk_forward(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task-queue handler: start the driver process for the run and watch it. A cancel (or pause) of
    the task kills the driver's process tree; the row is then ``cancelled`` (resumable)."""
    from app.models.database import SessionLocal
    from app.models.walk_forward_run import WalkForwardRun
    from app.services.task_queue import STOP_CANCELLED, STOP_PAUSED, get_wf_task_queue
    run_id, token = payload.get("run_id"), payload.get("token")
    if run_id is None or not token:
        return {"status": "failed", "error": "payload needs run_id and token"}
    db = SessionLocal()
    try:
        row = db.query(WalkForwardRun).filter(WalkForwardRun.id == run_id).first()
        if row is None:
            return {"status": "failed", "error": f"walk-forward run {run_id} does not exist"}
        log_path = driver_log_path(row)
    finally:
        db.close()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    cmd = [sys.executable, DRIVER_SCRIPT, "--run-id", str(run_id), "--token", token,
           "--task-id", task_id]
    tq = get_wf_task_queue()
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} task {task_id}: "
                 f"{' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        tq.update_progress(task_id, 0.0, f"driver pid {proc.pid}")
        reason = None
        while proc.poll() is None:
            time.sleep(2.0)
            reason = tq.stop_reason(task_id)
            if reason in (STOP_CANCELLED, STOP_PAUSED):
                try:
                    kill_tree(proc.pid)
                    proc.wait(timeout=30)
                except Exception as e:  # noqa: BLE001 -- the row is still marked below; logged loudly
                    logger.error(f"walk-forward: stopping the driver of run {run_id} failed: {e}",
                                 exc_info=True)
                break
    if reason in (STOP_CANCELLED, STOP_PAUSED):
        _mark_if_owned(run_id, token, "cancelled", "cancelled" if reason == STOP_CANCELLED
                       else "paused (task paused; resume continues it)")
        return {"status": reason}
    rc = proc.returncode
    if rc != 0:
        msg = (f"walk-forward driver exited with code {rc}"
               + (" (refused)" if rc == 2 else "") + f"; see {log_path}")
        _mark_if_owned(run_id, token, "error", msg)
        return {"status": "failed", "error": msg}
    return {"status": "completed", "run_id": run_id}


def recover_interrupted(db) -> int:
    """Startup: a UI run left pending / running whose driver is dead and whose task no longer runs
    (the server restarted under it) is marked ``error`` with that reason; it can be resumed. A
    driver that outlived the server keeps its row (it is still writing it)."""
    from app.models.walk_forward_run import WalkForwardRun
    n = 0
    rows = (db.query(WalkForwardRun).filter(WalkForwardRun.source == "ui",
                                            WalkForwardRun.status.in_(["pending", "running"])).all())
    for row in rows:
        if task_state(row.task_id) in ("queued", "running"):
            continue
        if row.status == "running" and driver_state(row) in ("alive", "unknown"):
            continue
        row.status = "error"
        row.error_message = "interrupted (the server stopped while it ran); resume continues it"
        row.progress_message = "interrupted"
        row.completed_at = row.updated_at = datetime.now()
        n += 1
    if n:
        db.commit()
    return n
