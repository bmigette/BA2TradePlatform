"""Resume of an INTERRUPTED optimization job, and the evidence that it is safe.

A walk-forward fold's training job killed mid-run (``tools/grid_goal2027.sh stop`` kills the whole
systemd cgroup) leaves its ``strategy_optimizations`` row at ``running`` and its GA checkpoint (keyed
on the job NAME, ``strategy_optimization_handler.checkpoint_task_id``) in the task queue. The
optimizer has resumed from such a checkpoint for a long time; what was missing is the step BEFORE
it: proving the old row is abandoned, proving the new launch is the same job, and handing the name
over so exactly one row carries it. That step lives here and is used by:

* ``tools/run_walk_forward.py`` (``RealRunner.run_train``) ASSESSES the row (``assess``) and, for a
  dead runner, launches the train command with ``--resume-interrupted``;
* the launcher's ``optimize --resume-interrupted`` compares the job identity
  (``identity_differences``) and CLAIMS the dead row with one guarded statement
  (``claim_and_retire``: renamed ``<name>~interrupted-<id>``, status ``failed``, nothing deleted) in
  the transaction that inserts the new row; a second starter's claim matches no row and is refused;
* the handler resumes only from a checkpoint written by the interrupted row (or the rows it
  continues), carrying this job's identity hash, and re-seeds ``all_results`` and the trial memo
  from the interrupted row.

LIVENESS (``judge``) is three-valued plus foreign. The handler records WHO runs each row
(``strategy_optimization_runners``): host, pid, process creation time and an independent process
identity (``proc_identity``: on Linux the boot id and the kernel start tick from /proc/<pid>/stat,
which no wall-clock step moves; everywhere a hash of the command line). DEAD only when the pid no
longer exists, or that identity PROVES the pid now belongs to another process (other boot, other
start tick, other command line). A pid that exists with a start time that does not match and
nothing proving reuse is UNKNOWN (a clock step on a VM moves psutil's create_time on Linux) and is
refused, never treated as dead. Another host is FOREIGN and refused. Re-run Backtest rows (top-N,
out-of-sample) record their writer the same way (``backtest_writers``).

The walk-forward DRIVER holds a per-prefix OS file lock (``exclusive``: ``fcntl.flock`` /
``msvcrt.locking``), released by the operating system when the holder dies, so two drivers can
never work on one cell and no liveness guess is involved.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

#: Set to 1 to resume a job whose recorded TEST app version / package minimums differ from the
#: current code's (the resumed generations then run on newer code than the earlier ones; the new
#: row records both versions in ``optimization_config.resumedFrom``). Refused by default.
ACROSS_CODE_CHANGE_ENV = "BA2_RESUME_ACROSS_CODE_CHANGE"

#: Statuses of a row whose runner may have been killed. ``pending``: created, handler not started.
INTERRUPTIBLE_STATUSES = ("running", "pending")

#: Config paths that do not identify the job: written by the handler during the run
#: (``seededFrom``, ``resumedFrom``), a launch timestamp (``backtest.backtest_id``), or the local
#: slot count (``parallelIndividuals``: execution only, like the worker list, not in the config).
#: ``seededFrom`` is still compared against the new launch's chain-seed source (identity_differences).
EXEMPT_CONFIG_PATHS = {("seededFrom",), ("resumedFrom",), ("discardedCheckpoints",),
                       ("resumeRowKey",), ("backtest", "backtest_id"), ("parallelIndividuals",)}

#: ``optimization_config`` key holding the row's random identity; the runner record stores the same
#: value (``row_key``). SQLite reuses a deleted max-id row's id, never this key.
ROW_KEY = "resumeRowKey"

ALIVE, DEAD, UNKNOWN, FOREIGN = "alive", "dead", "unknown", "foreign"
#: A run whose handler returned PAUSED while its process (a serve worker) lives: its task can
#: resume it there, so it is not resumable elsewhere (refused like a live run).
PAUSED = "paused"

#: The supported way to give up an interrupted job (no hand edits of the DB).
ABANDON_CMD = "ba2-test abandon-interrupted"

_CREATE_TIME_TOL_S = 2.0


class ResumeRefused(Exception):
    """The row cannot be resumed; the message says why and what to do."""


# --------------------------------------------------------------------------------- process identity


def this_host() -> str:
    return socket.gethostname()


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _linux_start_ticks(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat: the start time in clock ticks since boot (immune to wall-clock
    steps). None where /proc does not exist; raises ProcessLookupError when the pid is gone."""
    if not os.path.isdir("/proc/self"):
        return None
    raw = _read_text(f"/proc/{int(pid)}/stat")
    if raw is None:
        raise ProcessLookupError(pid)
    return int(raw[raw.rindex(")") + 2:].split()[19])


def proc_identity(pid: int) -> Dict[str, Any]:
    """The independent identity of a live process: whatever of boot id + kernel start tick (Linux)
    and command-line hash can be read. Raises ProcessLookupError when the pid does not exist."""
    import psutil
    out: Dict[str, Any] = {}
    boot = _read_text("/proc/sys/kernel/random/boot_id")
    ticks = _linux_start_ticks(pid)
    if boot and ticks is not None:
        out["boot_id"], out["start_ticks"] = boot, ticks
    try:
        out["cmdline_sha"] = hashlib.sha1(
            "\0".join(psutil.Process(int(pid)).cmdline()).encode("utf-8")).hexdigest()[:16]
    except psutil.NoSuchProcess as e:
        raise ProcessLookupError(pid) from e
    except psutil.AccessDenied:
        pass
    return out


def process_identity(pid: Optional[int] = None) -> Dict[str, Any]:
    import psutil
    pid = os.getpid() if pid is None else pid
    return {"host": this_host(), "pid": pid, "pid_started": psutil.Process(pid).create_time(),
            "proc_identity": proc_identity(pid)}


def judge(host: Optional[str], pid: Optional[int], pid_started: Optional[float],
          recorded: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(state, why) of a recorded runner: ALIVE, DEAD, UNKNOWN or FOREIGN (see module docstring).
    Only DEAD allows touching what it owned."""
    if not host or pid is None:
        return UNKNOWN, "no host/pid recorded"
    if host != this_host():
        return FOREIGN, f"runs on host {host!r} (this is {this_host()!r})"
    import psutil
    try:
        proc = psutil.Process(int(pid))
        now = proc_identity(int(pid))
    except (psutil.NoSuchProcess, ProcessLookupError):
        return DEAD, f"pid {pid} no longer exists"
    rec = recorded or {}
    if rec.get("boot_id") and now.get("boot_id"):
        if rec["boot_id"] != now["boot_id"]:
            return DEAD, f"the host rebooted since pid {pid} was recorded"
        if rec.get("start_ticks") is not None and now.get("start_ticks") is not None:
            if int(rec["start_ticks"]) == int(now["start_ticks"]):
                return ALIVE, f"pid {pid} is the recorded process (same kernel start tick)"
            return DEAD, (f"pid {pid} was reused (kernel start tick {now['start_ticks']} != recorded "
                          f"{rec['start_ticks']})")
    if rec.get("cmdline_sha") and now.get("cmdline_sha") and rec["cmdline_sha"] != now["cmdline_sha"]:
        return DEAD, f"pid {pid} was reused (another command line)"
    try:
        created = proc.create_time()
    except psutil.NoSuchProcess:
        return DEAD, f"pid {pid} no longer exists"
    except psutil.AccessDenied:
        return UNKNOWN, f"pid {pid} exists and its start time cannot be read"
    if pid_started is not None and abs(created - float(pid_started)) <= _CREATE_TIME_TOL_S:
        return ALIVE, f"pid {pid} is the recorded process (same start time)"
    return UNKNOWN, (f"pid {pid} exists with start time {created:.0f} != recorded {pid_started}, and "
                     f"nothing proves it is another process (a clock step moves this value)")


def code_versions() -> Dict[str, Any]:
    """The code a job runs under: TEST app version + package minimums (both decide GA results by
    the repo's versioning rules) + the git commit (audit only)."""
    from app.services.self_update import get_version_info
    info = get_version_info()
    return {"app_version": info.get("app_version"),
            "required_package_versions": info.get("required_package_versions"),
            "git_commit": info.get("git_commit")}


def _same_process(run, ident: Dict[str, Any]) -> bool:
    """Is the recorded runner ``run`` the process ``ident``? Host + pid, and the kernel start tick
    when both carry one (immune to clock steps), else the start time."""
    if run.host != ident["host"] or int(run.pid) != int(ident["pid"]):
        return False
    rec, now = run.proc_identity or {}, ident.get("proc_identity") or {}
    if rec.get("boot_id") and now.get("boot_id") and rec.get("start_ticks") is not None \
            and now.get("start_ticks") is not None:
        return rec["boot_id"] == now["boot_id"] and int(rec["start_ticks"]) == int(now["start_ticks"])
    return abs(float(run.pid_started) - float(ident["pid_started"])) <= _CREATE_TIME_TOL_S


def _opt_row_key(db, opt_id: int) -> Optional[str]:
    """The optimization row's random identity (``optimization_config[ROW_KEY]``), or None."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    cfg = db.query(SO.optimization_config).filter(SO.id == opt_id).scalar()  # never all_results
    return (cfg or {}).get(ROW_KEY)


def _bt_row_key(db, bt_id: int) -> Optional[str]:
    """A Backtest row's identity beyond its id: creation time + name (the AFTER DELETE trigger of
    migration 033 removes a writer record with its row in any case)."""
    from app.models.backtest import Backtest
    r = db.query(Backtest.created_at, Backtest.name).filter(Backtest.id == bt_id).first()
    return None if r is None else f"{r[0]}|{r[1]}"


def _taken_over(db, opt_id: int) -> Optional[str]:
    """Why row ``opt_id`` may no longer be run by anyone: claimed by a resume, or retired."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    for (succ,) in db.query(Run.optimization_id).filter(Run.resumed_from_id == opt_id).all():
        if runner_of(db, succ) is not None:
            return f"it was claimed by optimization {succ} (--resume-interrupted)"
    name = db.query(SO.name).filter(SO.id == opt_id).scalar()
    if name and ("~interrupted-" in name or "~abandoned-" in name):
        return f"it was retired as {name!r}"
    return None


def register_runner(db, opt_id: int, *, resumed_from_id: Optional[int] = None,
                    identity: Optional[Dict[str, Any]] = None,
                    versions: Optional[Dict[str, Any]] = None, commit: bool = True) -> str:
    """Record who runs ``opt_id`` and return the run's token (written into each of its
    checkpoints). Called by the launcher when it creates the row (in the same transaction) and by
    the handler as the job starts or a paused task resumes.

    Refused (``ResumeRefused``, nothing changed) when the row was taken over (claimed by a resume,
    or retired), or is owned by ANOTHER process: that one continues it only by claiming it into a
    new row (``optimize --resume-interrupted``). The SAME process registering again keeps the
    record and its token, so the row's own checkpoints stay its own (``ended_at`` cleared). The
    record is bound to the row's random key (``row_key`` = ``optimization_config[ROW_KEY]``): a
    record left by a deleted row whose id SQLite reused is purged by ``runner_of``, never
    inherited."""
    import uuid
    from app.models.strategy_optimization import StrategyOptimization as SO
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    ident = identity or process_identity()
    if db.get(SO, opt_id) is None:
        raise ResumeRefused(f"optimization {opt_id} does not exist")
    taken = _taken_over(db, opt_id)
    if taken:
        raise ResumeRefused(f"optimization {opt_id} must not be run here: {taken}. Not started.")
    run = runner_of(db, opt_id)
    if run is not None:
        if not _same_process(run, ident):
            state, why = judge_runner(run)
            raise ResumeRefused(
                f"optimization {opt_id} is owned by runner pid {run.pid} on {run.host} ({state}: "
                f"{why}); another process continues it only through optimize --resume-interrupted "
                f"(a new row that claims it). Not started.")
        if resumed_from_id is not None and run.resumed_from_id is None:
            run.resumed_from_id = resumed_from_id
        run.ended_at, run.ended_as = None, None
        if commit:
            db.commit()
        return run.run_token
    token = uuid.uuid4().hex
    row = db.get(SO, opt_id)
    key = (row.optimization_config or {}).get(ROW_KEY) or uuid.uuid4().hex
    row.optimization_config = {**(row.optimization_config or {}), ROW_KEY: key}
    db.add(Run(optimization_id=opt_id, row_key=key, host=ident["host"],
               pid=int(ident["pid"]), pid_started=float(ident["pid_started"]),
               proc_identity=ident.get("proc_identity"), run_token=token,
               code_versions=versions if versions is not None else code_versions(),
               resumed_from_id=resumed_from_id, started_at=datetime.now(), topn_done_at=None))
    if commit:
        db.commit()
    return token


def mark_runner_ended(db, opt_id: int, token: str, paused: bool = False) -> None:
    """The handler of run ``token`` returned (any outcome, an exception included): that run is no
    longer live. ``paused``: it returned PAUSED, so its task may resume it in the same process
    (judged PAUSED while that process lives). Not called on a kill. The launcher's TOP-N persist
    after a completed handler is still covered: a completed row is judged by ``topn_done_at`` and
    the process, not by this."""
    run = runner_of(db, opt_id)
    if run is not None and run.run_token == token:
        run.ended_at = datetime.now()
        run.ended_as = "paused" if paused else "returned"
        db.commit()


def mark_topn_done(db, opt_id: int) -> bool:
    """The launcher's end-of-job TOP-N persist ran to its end (whatever it persisted). False when
    the row has no runner record (a handler that did not register one): nothing is tracked for it,
    and ``assess`` already reads such a completed row as ``done``."""
    run = runner_of(db, opt_id)
    if run is None:
        return False
    run.topn_done_at = datetime.now()
    db.commit()
    return True


def runner_of(db, opt_id: int):
    """The runner record of row ``opt_id``, or None. A record whose bound row key does not match
    the row (the row was deleted and its id reused, or is gone) belongs to NO row: it is purged
    (deleted in the caller's transaction) and None is returned."""
    from app.models.strategy_optimization import StrategyOptimizationRunner as Run
    run = db.get(Run, opt_id)
    if run is None:
        return None
    if run.row_key is None or run.row_key != _opt_row_key(db, opt_id):
        db.delete(run)
        db.flush()
        return None
    return run


def judge_runner(run, *, ignore_ended: bool = False) -> Tuple[str, str]:
    """A recorded run whose handler returned (``ended_at``) is DEAD as a run even if its process
    lives on, unless it returned PAUSED in a process that still lives (PAUSED). ``ignore_ended``:
    judge the PROCESS (a completed job's launcher still persisting its TOP-N)."""
    if run is None:
        return UNKNOWN, "no runner was recorded"
    if run.ended_at is not None and not ignore_ended:
        if run.ended_as == "paused":
            state, why = judge(run.host, run.pid, run.pid_started, run.proc_identity)
            if state == ALIVE:
                return PAUSED, (f"paused at {run.ended_at} in live process pid {run.pid}: its task "
                                f"can resume it there")
            return state, why
        return DEAD, f"its handler returned at {run.ended_at}"
    return judge(run.host, run.pid, run.pid_started, run.proc_identity)


def newest_record_row(db, opt_id: int) -> Optional[int]:
    """The newest row of ``opt_id``'s resume lineage that holds trial records. A row that continues
    another holds that one's records plus its own once it wrote anything; one killed before its
    first write holds none, so the walk goes on to the row it continues."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    for rid in resume_lineage(db, opt_id):
        recs = db.query(SO.all_results).filter(SO.id == rid).scalar()
        if recs:
            return rid
    return None


def checkpoint_owned_by(db, ckpt: Dict[str, Any], opt_ids: Sequence[int]) -> Optional[int]:
    """Which of ``opt_ids`` wrote ``ckpt``: its ``optimization_id`` AND that row's current run
    token must match (a deleted row's id is reused by SQLite; its token is not). None if none."""
    wid = ckpt.get("optimization_id")
    if wid is None or int(wid) not in [int(i) for i in opt_ids]:
        return None
    run = runner_of(db, int(wid))
    if run is None or not run.run_token or ckpt.get("run_token") != run.run_token:
        return None
    return int(wid)


def resume_lineage(db, opt_id: int, limit: int = 50) -> List[int]:
    """``opt_id`` and every row it continues (runner ``resumed_from_id`` chain), newest first."""
    out, cur = [], opt_id
    while cur is not None and cur not in out and len(out) < limit:
        out.append(cur)
        run = runner_of(db, cur)
        cur = run.resumed_from_id if run is not None else None
    return out


# --------------------------------------------------------------------------------- backtest writers


def register_backtest_writer(db, bt_id: int, identity: Optional[Dict[str, Any]] = None) -> None:
    """Record the process writing Backtest ``bt_id`` (the caller commits, with the row itself),
    bound to the row's creation time like a runner record."""
    from app.models.strategy_optimization import BacktestWriter
    ident = identity or process_identity()
    db.merge(BacktestWriter(backtest_id=bt_id, row_key=_bt_row_key(db, bt_id),
                            host=ident["host"], pid=int(ident["pid"]),
                            pid_started=float(ident["pid_started"]),
                            proc_identity=ident.get("proc_identity"), started_at=datetime.now()))


def writer_of(db, bt_id: int):
    """The writer record of Backtest ``bt_id``, or None; a record not bound to this row (its id
    was reused) is purged, as in ``runner_of``."""
    from app.models.strategy_optimization import BacktestWriter
    w = db.get(BacktestWriter, bt_id)
    if w is None:
        return None
    if w.row_key is None or w.row_key != _bt_row_key(db, bt_id):
        db.delete(w)
        db.flush()
        return None
    return w


def judge_backtest_writer(db, bt_id: int) -> Tuple[str, str]:
    w = writer_of(db, bt_id)
    if w is None:
        return UNKNOWN, "no writer was recorded for this row"
    return judge(w.host, w.pid, w.pid_started, w.proc_identity)


def retire_dead_backtest(db, bt_id: int, label: str, note: str) -> str:
    """Retire a non-completed Backtest row ONLY when its recorded writer is DEAD; otherwise raise
    ``ResumeRefused`` (a live or unprovable writer may still be saving it)."""
    state, why = judge_backtest_writer(db, bt_id)
    if state != DEAD:
        raise ResumeRefused(
            f"{label} (backtest {bt_id}) is not completed and its writer is {state.upper()}: {why}. "
            f"Not touched (it may still be being written). Let that process finish, or stop it. If "
            f"you have checked that no process writes it: {ABANDON_CMD} --backtest-id {bt_id} "
            f"--confirm-not-running --reason '...' (the row is renamed and kept; that genome is "
            f"re-run on the next start, nothing else is lost).")
    return retire_backtest(db, bt_id, note)


# --------------------------------------------------------------------------------- assess


@dataclass(frozen=True)
class Assessment:
    """``none`` no row; ``done`` completed (TOP-N persist finished or not tracked); ``topn``
    completed but its TOP-N persist was cut off (runner dead); ``resume`` interrupted with a dead
    runner."""
    kind: str
    opt_id: Optional[int] = None
    detail: str = ""


def _ckpt_hint(name: str, opt_id: int) -> str:
    from app.services.strategy_optimization_handler import checkpoint_task_id
    return checkpoint_task_id(name, opt_id)


def abandon_hint(name: str, opt_id: int, confirm: bool = False) -> str:
    """The precise cost of giving the job up, and the supported command (never a hand edit)."""
    return (f"To give it up: {ABANDON_CMD} --id {opt_id}"
            + (" --confirm-not-running" if confirm else "")
            + f" --reason '...'. That COSTS its finished trials (they are not reused) and retires "
              f"its checkpoint {_ckpt_hint(name, opt_id)} (payload kept in the task queue): the next "
              f"start of {name!r} trains from generation 0.")


def _refuse_unless_dead(name: str, opt_id: int, status: str, run, what: str,
                       ignore_ended: bool = False) -> str:
    state, why = judge_runner(run, ignore_ended=ignore_ended)
    if state == DEAD:
        return why
    if run is None and status == "pending":
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is 'pending' with no runner yet: it is most likely "
            f"QUEUED (a --submit / UI job waiting for the serve worker), which may still start it, "
            f"so it is not {what}. Check the serve task queue first (UI: Backtesting -> History -> "
            f"Running jobs; or the task_queue row of type strategy_optimization whose payload has "
            f"optimization_id {opt_id}) and cancel that queued task, or wait for it to run. Only if "
            f"no task holds it any more: " + abandon_hint(name, opt_id, confirm=True))
    if run is None:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is {status!r} but no runner was recorded for it "
            f"(started by code older than the runner table): whether a process still runs it cannot "
            f"be proven, so it is not {what}. If you have checked that no process runs it: "
            + abandon_hint(name, opt_id, confirm=True))
    if state == ALIVE:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is {status!r} in a LIVE process ({why}; host "
            f"{run.host}, since {run.started_at}): it is not {what} (two runners on one row). Let it "
            f"finish, or stop that process and start again (it then resumes, nothing is lost).")
    if state == PAUSED:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) is PAUSED: {why}. It is not {what}: resume it "
            f"there (nothing is lost), or cancel that task and stop the process; it can then be "
            f"resumed here.")
    raise ResumeRefused(
        f"optimization {name!r} (id={opt_id}) is {status!r} and its runner is {state.upper()}: "
        f"{why}. It is not {what}: only a runner proven dead is replaced. Check pid {run.pid} on "
        f"{run.host}; stop it if it is the job, then start again (it then resumes, nothing is "
        f"lost). " + abandon_hint(name, opt_id, confirm=True))


def plain_launch_gate(db, name: str) -> Tuple[Optional[str], Optional[int]]:
    """A launch WITHOUT --resume-interrupted (other grids, a hand launch). Returns
    ``(None, None)`` when it may simply create its row; ``("claim", id)`` for the ONE dead
    non-terminal row of ``name``, which it must claim and resume (identity check) rather than run
    beside; ``("supersede", id)`` for a dead ``pending`` row that never ran and continues nothing
    (no trial, no checkpoint of its own, no ``resumed_from_id``): nothing to resume, so the new row
    retires it in its own transaction, whatever its config. Raises for a live, paused or unprovable
    non-terminal row, or more than one: two non-terminal rows of one name never exist. Terminal rows
    (completed, failed, cancelled) are not in the way, as before."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    rows = (db.query(SO.id, SO.status).filter(SO.name == name,
                                               SO.status.in_(INTERRUPTIBLE_STATUSES))
            .order_by(SO.id).all())
    if not rows:
        return None, None
    if len(rows) > 1:
        raise ResumeRefused(
            f"{len(rows)} non-terminal optimizations are named {name!r} "
            f"({', '.join(f'id={i} {st}' for i, st in rows)}): none is resumed. Give up the extra "
            f"ones with {ABANDON_CMD} --id <id> --reason '...' (each COSTS that row's finished "
            f"trials; its checkpoint is retired, payload kept).")
    opt_id, status = rows[0]
    run = runner_of(db, opt_id)
    _refuse_unless_dead(name, opt_id, status, run, "replaced by a new row")
    if status == "pending" and (run is None or run.resumed_from_id is None):
        from app.models.strategy_optimization import StrategyOptimization as SO
        from app.services import strategy_optimization_handler as H
        recs = db.query(SO.all_results).filter(SO.id == opt_id).scalar()
        ckpt = H._load_checkpoint(H.checkpoint_task_id(name, opt_id))
        if not recs and (ckpt is None or checkpoint_owned_by(db, ckpt, [opt_id]) is None):
            return "supersede", opt_id
    return "claim", opt_id


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
            f"under the same name does this. Give up each unwanted non-completed row with "
            f"{ABANDON_CMD} --id <id> --reason '...' (COSTS that row's finished trials; its "
            f"checkpoint is retired, payload kept), or use a new --prefix.")
    opt_id, status, err = rows[0]
    run = runner_of(db, opt_id)
    if status == "completed":
        if run is not None and run.topn_done_at is None:
            why = _refuse_unless_dead(name, opt_id, "completed (TOP-N persist unfinished)", run,
                                      "touched", ignore_ended=True)
            return Assessment("topn", opt_id,
                              f"completed; its end-of-job TOP-N persist was cut off ({why})")
        return Assessment("done", opt_id)
    if status not in INTERRUPTIBLE_STATUSES:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) exists with status {status!r}"
            + (f" ({(err or '')[:300]})" if err else "")
            + ": a failed or cancelled job is not resumed automatically (its failure may repeat). "
              "Read the error and fix its cause. " + abandon_hint(name, opt_id)
            + " Or use a new --prefix.")
    why = _refuse_unless_dead(name, opt_id, status, run, "resumed")
    return Assessment("resume", opt_id, f"{status}; {why}")


# --------------------------------------------------------------------------------- identity check


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _short(v: Any, n: int = 160) -> str:
    s = json.dumps(v, default=str, sort_keys=True)
    return s if len(s) <= n else s[:n] + "..."


def _strip_exempt(cfg: Any, path: Sequence[str] = ()) -> Any:
    if not isinstance(cfg, dict):
        return cfg
    return {k: _strip_exempt(v, (*path, str(k))) for k, v in cfg.items()
            if (*path, str(k)) not in EXEMPT_CONFIG_PATHS}


def job_identity_hash(cfg: Dict[str, Any], fitness_metric: str, optimization_type: str,
                      param_space: Any, entry_rules: Any, exit_rules: Any) -> str:
    """One hash of everything ``identity_differences`` compares except the code versions: the
    config (window, data digests, universe, exclusions, GA settings, pins) minus the exempt paths,
    metric, type, decoded gene space (in order) and strategy rules. Written into every checkpoint;
    a resume only continues from a checkpoint carrying the same hash."""
    blob = json.dumps(_jsonable({
        "config": _strip_exempt(cfg or {}), "fitness_metric": fitness_metric,
        "optimization_type": optimization_type or "genetic",
        "genes": [[k, v] for k, v in (param_space or {}).items()],
        "entry_rules": entry_rules, "exit_rules": exit_rules}), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


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
    pins, the run block with its data window, digests, universe and exclusions), metric and type,
    the decoded gene space, the strategy's rules, the chain-seed source the row actually used
    (``seededFrom``), and the code versions."""
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization as SO
    old = db.get(SO, stale_id)
    old_cfg = _jsonable(old.optimization_config or {})
    out = config_differences(old_cfg, _jsonable(new_cfg))
    seeded = old_cfg.get("seededFrom") or {}
    if seeded.get("optimization_id") is not None:
        want = (new_cfg or {}).get("chainSeedFromOptimizationId")
        if want is None or int(want) != int(seeded["optimization_id"]):
            out.append(f"seededFrom: the row was seeded from optimization "
                       f"{seeded['optimization_id']} ({seeded.get('name')!r}); this launch seeds "
                       f"from {want!r}")
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
    run = runner_of(db, stale_id)
    was = (run.code_versions or {}) if run is not None else {}
    if not allow_code_change:
        for k in ("app_version", "required_package_versions"):
            if was.get(k) != versions.get(k):
                out.append(f"code {k}: {_short(was.get(k))} -> {_short(versions.get(k))} (the "
                           f"resumed generations would run on other code than the earlier ones; "
                           f"finish the job on its own code, or set {ACROSS_CODE_CHANGE_ENV}=1 to "
                           f"resume anyway: both versions are recorded on the new row)")
    return out


# --------------------------------------------------------------------------------- claim / retire


def interrupted_name(name: str, row_id: int) -> str:
    return f"{name}~interrupted-{row_id}"


def claim_and_retire(db, opt_id: int, name: str, note: str) -> str:
    """CLAIM the interrupted row with ONE guarded statement: it is renamed
    ``<name>~interrupted-<id>`` and set ``failed`` only while it still carries ``name`` and an
    interruptible status. A second starter's claim matches no row (rowcount 0) and is refused. Does
    not commit: the caller commits it in the transaction that inserts the row taking the name.
    Nothing is deleted: its all_results seed the resumed run and it stays as evidence."""
    from sqlalchemy import update
    from app.models.strategy_optimization import StrategyOptimization as SO
    new_name = interrupted_name(name, opt_id)
    res = db.execute(
        update(SO).where(SO.id == opt_id, SO.name == name, SO.status.in_(INTERRUPTIBLE_STATUSES))
        .values(name=new_name, status="failed", completed_at=datetime.now(),
                error_message=f"INTERRUPTED: runner killed; {note}"[:1000])
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise ResumeRefused(
            f"optimization {name!r} (id={opt_id}) was claimed by another starter (or changed) "
            f"between the check and the claim: this launch is refused, the other one resumes it")
    return new_name


def retire_backtest(db, bt_id: int, note: str) -> str:
    """Take a Backtest row out of its name (the caller has proven its writer dead). Nothing is
    deleted."""
    from app.models.backtest import Backtest
    row = db.get(Backtest, bt_id)
    new_name = interrupted_name(row.name, row.id)
    row.name = new_name
    row.status = "failed"
    row.error_message = (f"INTERRUPTED: killed while {note}")[:1000]
    db.commit()
    return new_name


# --------------------------------------------------------------------------------- driver lock

_WIN_LOCK_OFFSET = 1 << 20      # the locked byte lies beyond the identity text, which stays readable


def _os_lock(fd: int) -> None:
    """Non-blocking exclusive OS lock; raises OSError when another open handle holds it. The OS
    drops it when the holder dies."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _describe(path: str) -> str:
    text = _read_text(path)
    if not text:
        return "holder unknown"
    try:
        h = json.loads(text)
        return f"pid {h.get('pid')} on {h.get('host')}, since {h.get('acquired_at')}"
    except ValueError:
        return "holder unknown"


def acquire_lock(path: str, label: str) -> Dict[str, Any]:
    """Take the driver lock at ``path`` (an OS lock on the file, held while the returned handle is
    open). Held by any other live process -> ``ResumeRefused``. The file's content only names the
    holder for messages; it plays no part in the decision. The file is never deleted (deleting a
    lock file races with the next opener)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    # Held by THIS process only: never passed to a child (exec'd subprocesses and the spawn-based
    # pools close it). An orphaned child must not keep the cell locked: its own row-level records
    # (runner, backtest writer) are what stop a resume from touching its work.
    os.set_inheritable(fd, False)
    try:
        _os_lock(fd)
    except OSError:
        os.close(fd)
        raise ResumeRefused(f"{label}: lock {path} is held by a LIVE process ({_describe(path)}): "
                            f"another driver runs this cell") from None
    os.lseek(fd, 0, os.SEEK_SET)
    prev = os.read(fd, 65536).decode("utf-8", "replace").strip()
    if prev:
        print(f"{label}: taking over the lock left by a dead driver ({_describe(path)})", flush=True)
    me = {**process_identity(), "label": label, "acquired_at": datetime.now().isoformat()}
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(me).encode("utf-8"))
    return {"fd": fd, "path": path, "me": me}


def release_lock(handle: Dict[str, Any]) -> None:
    fd = handle["fd"]
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        _os_unlock(fd)
    finally:
        os.close(fd)


@contextmanager
def exclusive(path: str, label: str) -> Iterator[Dict[str, Any]]:
    handle = acquire_lock(path, label)
    try:
        yield handle["me"]
    finally:
        release_lock(handle)


# --------------------------------------------------------------------------------- abandon


def abandon_optimization(db, opt_id: int, reason: str, confirm_not_running: bool = False
                         ) -> Dict[str, Any]:
    """Give up an interrupted (or failed / cancelled) optimization cleanly, instead of deleting it
    by hand: the row is renamed ``<name>~abandoned-<id>`` and set ``failed`` with the reason, its
    run is marked ended, and its OWN checkpoint is retired (payload kept). Its finished trials are
    not reused: the next start of the name trains from generation 0.

    A completed row, a LIVE or PAUSED run are refused. A run that cannot be judged (no runner
    record, another host, unknown) needs ``confirm_not_running`` (the operator checked). The row is
    renamed BEFORE the checkpoint is retired: a kill in between leaves a checkpoint whose writer is
    retired, which the next start retires itself."""
    from app.models.strategy_optimization import StrategyOptimization as SO
    from app.services import strategy_optimization_handler as H
    row = db.get(SO, opt_id)
    if row is None:
        raise ResumeRefused(f"optimization {opt_id} does not exist")
    if row.name and f"~abandoned-{opt_id}" in row.name:
        return {"already": True, "optimization_id": opt_id, "name": row.name, "renamed": row.name}
    if row.name and "~interrupted-" in row.name:
        raise ResumeRefused(f"optimization {opt_id} was already retired by a resume ({row.name!r}; "
                            f"another row continues it): nothing to abandon")
    if row.status == "completed":
        raise ResumeRefused(f"optimization {opt_id} ({row.name!r}) is completed: not abandoned")
    if not (reason or "").strip():
        raise ResumeRefused("a --reason is required")
    run = runner_of(db, opt_id)
    state, why = judge_runner(run)
    if state in (ALIVE, PAUSED) or (state != DEAD and not confirm_not_running):
        raise ResumeRefused(
            f"optimization {opt_id} ({row.name!r}) is not abandoned: its run is {state.upper()} "
            f"({why})." + ("" if state in (ALIVE, PAUSED) else
                           " If you have checked that no process runs it, add --confirm-not-running."))
    name, n_trials = row.name, len(row.all_results or [])
    tid = H.checkpoint_task_id(name, opt_id)
    ckpt = H._load_checkpoint(tid)
    others = db.query(SO.id).filter(SO.name == name, SO.id != opt_id).count()
    own_ckpt = ckpt is not None and (
        checkpoint_owned_by(db, ckpt, [opt_id]) is not None
        or (ckpt.get("optimization_id") is None and others == 0))
    now = datetime.now()
    row.name = f"{name}~abandoned-{opt_id}"
    row.status = "failed"
    row.completed_at = now
    row.error_message = (f"ABANDONED by operator ({reason}); was {name!r}, {n_trials} finished "
                         f"trial(s) not reused")[:1000]
    if run is not None:
        run.ended_at = run.ended_at or now
        run.ended_as = "abandoned"
    db.commit()
    retired = H._retire_checkpoint(tid, f"abandoned with optimization {opt_id}: {reason}") \
        if own_ckpt else None
    return {"optimization_id": opt_id, "name": name, "renamed": row.name, "trials_lost": n_trials,
            "checkpoint": tid if own_ckpt else None, "retired_as": retired}


def abandon_backtest(db, bt_id: int, reason: str, confirm_not_running: bool = False) -> str:
    """Give up a non-completed re-run Backtest row (renamed, ``failed``, kept): the genome is
    re-run on the next start. Refused while its recorded writer lives; a writer that cannot be
    judged needs ``confirm_not_running``."""
    from app.models.backtest import Backtest
    row = db.get(Backtest, bt_id)
    if row is None or row.status == "completed":
        raise ResumeRefused(f"backtest {bt_id} does not exist or is completed: not abandoned")
    if not (reason or "").strip():
        raise ResumeRefused("a --reason is required")
    state, why = judge_backtest_writer(db, bt_id)
    if state == ALIVE or (state != DEAD and not confirm_not_running):
        raise ResumeRefused(f"backtest {bt_id} is not abandoned: its writer is {state.upper()} ({why})"
                            + ("" if state == ALIVE else
                               ". If you have checked that no process writes it, add "
                               "--confirm-not-running."))
    return retire_backtest(db, bt_id, f"abandoned by operator ({reason})")
