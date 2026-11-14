"""Resume of an INTERRUPTED optimization job, and the evidence that it is safe.

A walk-forward fold's training job killed mid-run (``tools/grid_goal2027.sh stop`` kills the whole
systemd cgroup) leaves its ``strategy_optimizations`` row at ``running`` and its GA checkpoint (keyed
on the job NAME, ``strategy_optimization_handler.checkpoint_task_id``) in the task queue. The
optimizer has resumed from such a checkpoint for a long time; what was missing is the step BEFORE
it: proving the old row is abandoned, proving the new launch is the same job, and freeing the name
so exactly one row carries it. That step lives here and is used by two callers:

* ``tools/run_walk_forward.py`` (``RealRunner.run_train``) ASSESSES the row (``assess``) and, for a
  dead runner, launches the train command with ``--resume-interrupted``;
* the launcher's ``optimize --resume-interrupted`` assesses again (a race with a second starter),
  compares the job identity (``identity_differences``), RETIRES the dead row (``retire_optimization``:
  renamed ``<name>~interrupted-<id>``, status ``failed``, nothing deleted) and creates the new row;
  the handler then resumes from the checkpoint and re-seeds ``all_results`` and the trial memo from
  the retired row (``strategy_optimization_handler``, ``resume_of``).

LIVENESS (``process_alive``): the handler records WHO runs each row in
``strategy_optimization_runners`` (host, pid, process creation time). On the same host a pid that
no longer exists, or exists with another creation time (pid reuse), proves the runner is gone.
Nothing else is taken as proof: a row without a runner record (started before this table existed,
or killed before its first line) and a row owned by ANOTHER host are refused, never resumed.

The walk-forward DRIVER takes a per-cell lock file (``exclusive``) with the same identity, so two
drivers can never work on one cell; under it a half-written out-of-sample Backtest row is known
to be abandoned.
"""
from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence

#: Set to 1 to resume a job whose recorded TEST app version / package minimums differ from the
#: current code's (the resumed generations then run on newer code than the earlier ones; the new
#: row records both versions). Refused by default.
ACROSS_CODE_CHANGE_ENV = "BA2_RESUME_ACROSS_CODE_CHANGE"

#: Statuses of a row whose runner may have been killed. ``pending``: created, handler not started.
INTERRUPTIBLE_STATUSES = ("running", "pending")

#: Config paths that do not identify the job: written by the handler during the run
#: (``seededFrom``), a launch timestamp (``backtest.backtest_id``), or local slot count
#: (``parallelIndividuals``: execution only, like the worker list, which is not in the config).
EXEMPT_CONFIG_PATHS = {("seededFrom",), ("backtest", "backtest_id"), ("parallelIndividuals",)}

_CREATE_TIME_TOL_S = 2.0


class ResumeRefused(Exception):
    """The row cannot be resumed; the message says why and what to do."""


# --------------------------------------------------------------------------------- identity


def this_host() -> str:
    return socket.gethostname()


def process_identity(pid: Optional[int] = None) -> Dict[str, Any]:
    import psutil
    pid = os.getpid() if pid is None else pid
    return {"host": this_host(), "pid": pid, "pid_started": psutil.Process(pid).create_time()}


def process_alive(host: str, pid: int, pid_started: float) -> Optional[bool]:
    """True/False for a process on THIS host; None for another host (cannot be judged here).
    A pid that exists with a different creation time is a reused pid: the runner is dead. A pid
    whose creation time cannot be read (access denied) is treated as alive."""
    if host != this_host():
        return None
    import psutil
    try:
        created = psutil.Process(int(pid)).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    return abs(created - float(pid_started)) <= _CREATE_TIME_TOL_S


def code_versions() -> Dict[str, Any]:
    """The code a job runs under: TEST app version + package minimums (both decide GA results by
    the repo's versioning rules) + the git commit (audit only)."""
    from app.services.self_update import get_version_info
    info = get_version_info()
    return {"app_version": info.get("app_version"),
            "required_package_versions": info.get("required_package_versions"),
            "git_commit": info.get("git_commit")}


def register_runner(db, opt_id: int, *, resumed_from_id: Optional[int] = None,
                    identity: Optional[Dict[str, Any]] = None,
                    versions: Optional[Dict[str, Any]] = None) -> None:
    """Record (or replace) who runs ``opt_id``. Called by the handler as the job starts."""
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    ident = identity or process_identity()
    db.merge(Run(optimization_id=opt_id, host=ident["host"], pid=int(ident["pid"]),
                 pid_started=float(ident["pid_started"]),
                 code_versions=versions if versions is not None else code_versions(),
                 resumed_from_id=resumed_from_id, started_at=datetime.now(), topn_done_at=None))
    db.commit()


def mark_topn_done(db, opt_id: int) -> bool:
    """The launcher's end-of-job TOP-N persist ran to its end (whatever it persisted). False when
    the row has no runner record (a handler that did not register one): nothing is tracked for it,
    and ``assess`` already reads such a completed row as ``done``."""
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    row = db.get(Run, opt_id)
    if row is None:
        return False
    row.topn_done_at = datetime.now()
    db.commit()
    return True


def _runner(db, opt_id: int):
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    return db.get(Run, opt_id)


# --------------------------------------------------------------------------------- assess


@dataclass(frozen=True)
class Assessment:
    """``none`` no row; ``done`` completed (TOP-N persist finished or not tracked); ``topn``
    completed but its TOP-N persist was cut off; ``resume`` interrupted with a dead runner."""
    kind: str
    opt_id: Optional[int] = None
    detail: str = ""


def _ckpt_hint(name: str, opt_id: int) -> str:
    from app.services.strategy_optimization_handler import checkpoint_task_id
    return checkpoint_task_id(name, opt_id)


def assess(db, name: str) -> Assessment:
    """What a new launch of job ``name`` may do with the rows already named so. Raises
    ``ResumeRefused`` (with the reason and the way out) for every row it will not touch."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    rows = (db.query(SO.id, SO.status, SO.error_message).filter(SO.name == name)
            .order_by(SO.id).all())
    if not rows:
        return Assessment("none")
    if len(rows) > 1:
        raise ResumeRefused(
            f"{len(rows)} optimizations are named {name!r} "
            f"({', '.join(f'id={i} {st}' for i, st, _ in rows)}). A failed train plus a re-run "
            f"under the same name does this. Delete the unwanted row(s) from the test DB "
            f"(or use a new --prefix) so exactly one remains.")
    opt_id, status, err = rows[0]
    run = _runner(db, opt_id)
    if status == "completed":
        if run is not None and run.topn_done_at is None:
            return Assessment("topn", opt_id, "completed; its end-of-job TOP-N persist was cut off")
        return Assessment("done", opt_id)
    if status not in INTERRUPTIBLE_STATUSES:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) exists with status {status!r}"
            + (f" ({(err or '')[:300]})" if err else "")
            + ": a failed or cancelled job is not resumed automatically (its failure may repeat). "
              "Read the error; then delete that row (its checkpoint "
            + f"{_ckpt_hint(name, opt_id)} stays and the next start resumes from it), or use a new "
              "--prefix.")
    if run is None:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is {status!r} but no runner was recorded for it "
            f"(started by code older than the runner table, or killed before the handler began): "
            f"whether a process still runs it cannot be proven, so it is not resumed. If none "
            f"does, delete row id={opt_id}; the next start then resumes from checkpoint "
            f"{_ckpt_hint(name, opt_id)} the old way (all_results limited to the checkpoint's elites).")
    alive = process_alive(run.host, run.pid, run.pid_started)
    if alive is None:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is {status!r} and owned by host {run.host!r} "
            f"(this is {this_host()!r}): its runner cannot be checked from here. Resume it on "
            f"{run.host!r}.")
    if alive:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is {status!r} in a LIVE process (pid {run.pid} on "
            f"{run.host}, since {run.started_at}): it is not resumed (two runners on one row). Let "
            f"it finish, or stop that process and start again.")
    return Assessment("resume", opt_id,
                      f"{status}; runner pid {run.pid} on {run.host} is gone")


# --------------------------------------------------------------------------------- identity check


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _short(v: Any, n: int = 160) -> str:
    s = json.dumps(v, default=str, sort_keys=True)
    return s if len(s) <= n else s[:n] + "..."


def config_differences(old: Any, new: Any, path: Sequence[str] = ()) -> List[str]:
    """Dotted paths where two JSON-shaped configs differ (``EXEMPT_CONFIG_PATHS`` skipped)."""
    if tuple(path) in EXEMPT_CONFIG_PATHS:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        out: List[str] = []
        for k in sorted(set(old) | set(new), key=str):
            p = (*path, str(k))
            if tuple(p) in EXEMPT_CONFIG_PATHS:
                continue
            if k not in old:
                out.append(f"{'.'.join(p)}: absent -> {_short(new[k])}")
            elif k not in new:
                out.append(f"{'.'.join(p)}: {_short(old[k])} -> absent")
            else:
                out.extend(config_differences(old[k], new[k], p))
        return out
    if old != new:
        return [f"{'.'.join(path) or '<config>'}: {_short(old)} -> {_short(new)}"]
    return []


def identity_differences(db, stale_id: int, *, new_cfg: Dict[str, Any], fitness_metric: str,
                         optimization_type: str, param_space: Optional[Dict[str, Any]],
                         entry_rules: Any, exit_rules: Any, versions: Dict[str, Any],
                         allow_code_change: bool = False) -> List[str]:
    """Everything that makes the new launch a DIFFERENT job from the interrupted row ``stale_id``
    (empty = the same job): the whole optimization config (GA settings, gene space declaration,
    pins, the run block with its data digests, universe and exclusions), metric and type, the
    decoded gene space, the strategy's rules, and the code versions."""
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization as SO
    old = db.get(SO, stale_id)
    out = config_differences(_jsonable(old.optimization_config or {}), _jsonable(new_cfg))
    if old.fitness_metric != fitness_metric:
        out.append(f"fitness_metric: {old.fitness_metric!r} -> {fitness_metric!r}")
    if (old.optimization_type or "genetic") != optimization_type:
        out.append(f"optimization_type: {old.optimization_type!r} -> {optimization_type!r}")
    if old.parameter_ranges is not None and param_space is not None:
        gene = config_differences(_jsonable(old.parameter_ranges), _jsonable(param_space))
        out.extend(f"gene space {d}" for d in gene)
        if not gene and list(old.parameter_ranges) != list(_jsonable(param_space)):
            out.append("gene space: same genes in another ORDER (chromosomes map by position)")
    st = db.get(Strategy, old.strategy_id)
    for col, new in (("entry_rules", entry_rules), ("exit_rules", exit_rules)):
        if st is None or _jsonable(getattr(st, col)) != _jsonable(new):
            out.extend(f"strategy {d}" for d in config_differences(
                {col: _jsonable(getattr(st, col)) if st is not None else None}, {col: _jsonable(new)}))
    run = _runner(db, stale_id)
    was = (run.code_versions or {}) if run is not None else {}
    if not allow_code_change:
        for k in ("app_version", "required_package_versions"):
            if was.get(k) != versions.get(k):
                out.append(f"code {k}: {_short(was.get(k))} -> {_short(versions.get(k))} (the "
                           f"resumed generations would run on other code than the earlier ones; "
                           f"finish the job on its own code, or set {ACROSS_CODE_CHANGE_ENV}=1 to "
                           f"resume anyway: both versions are recorded on the rows)")
    return out


# --------------------------------------------------------------------------------- retire


def interrupted_name(name: str, row_id: int) -> str:
    return f"{name}~interrupted-{row_id}"


def retire_optimization(db, opt_id: int, note: str, commit: bool = True) -> str:
    """Take an abandoned row out of its job name (``<name>~interrupted-<id>``, status ``failed``,
    reason in ``error_message``). Nothing is deleted: its all_results seed the resumed run and it
    stays as evidence. ``commit=False`` leaves the commit to the caller (the launcher renames it
    in the same transaction that creates the row taking over the name). Returns the new name."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    row = db.get(SO, opt_id)
    new_name = interrupted_name(row.name, row.id)
    row.error_message = (f"INTERRUPTED: runner killed while {row.status}; {note}")[:1000]
    row.name = new_name
    row.status = "failed"
    row.completed_at = datetime.now()
    if commit:
        db.commit()
    return new_name


def retire_backtest(db, bt_id: int, note: str) -> str:
    """Same for a Backtest row left non-completed by a killed persist (an out-of-sample or TOP-N
    re-run's result write). Nothing is deleted."""
    from app.models.backtest import Backtest
    row = db.get(Backtest, bt_id)
    new_name = interrupted_name(row.name, row.id)
    row.name = new_name
    row.status = "failed"
    row.error_message = (f"INTERRUPTED: killed while {note}")[:1000]
    db.commit()
    return new_name


# --------------------------------------------------------------------------------- driver lock


def _read_lock(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ResumeRefused(f"lock file {path} is unreadable ({type(e).__name__}: {e}); check "
                            f"that no driver runs this cell, then delete it") from e


def acquire_lock(path: str, label: str) -> Dict[str, Any]:
    """Take the driver lock at ``path`` for ``label``. A lock held by a live process on this host,
    or by another host, is REFUSED; one left by a dead process is taken over (and said so)."""
    held = _read_lock(path)
    if held is not None:
        alive = process_alive(held.get("host"), held.get("pid"), held.get("pid_started", 0.0))
        if alive is None:
            raise ResumeRefused(f"{label}: lock {path} is held by host {held.get('host')!r} (this "
                                f"is {this_host()!r}); not taken")
        if alive:
            raise ResumeRefused(f"{label}: lock {path} is held by a LIVE process (pid "
                                f"{held.get('pid')} on {held.get('host')}): another driver runs "
                                f"this cell")
        print(f"{label}: taking over the lock of dead driver pid {held.get('pid')} "
              f"({path})", flush=True)
    me = {**process_identity(), "label": label, "acquired_at": datetime.now().isoformat()}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(me, fh)
    os.replace(tmp, path)
    got = _read_lock(path)
    if not got or got.get("pid") != me["pid"] or got.get("host") != me["host"]:
        raise ResumeRefused(f"{label}: lost the race for lock {path} to pid "
                            f"{(got or {}).get('pid')}")
    return me


def release_lock(path: str, me: Dict[str, Any]) -> None:
    held = _read_lock(path)
    if held and held.get("pid") == me["pid"] and held.get("host") == me["host"]:
        os.remove(path)


@contextmanager
def exclusive(path: str, label: str) -> Iterator[Dict[str, Any]]:
    me = acquire_lock(path, label)
    try:
        yield me
    finally:
        release_lock(path, me)
