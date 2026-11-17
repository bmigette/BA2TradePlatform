"""Walk-forward results in the DB (``walk_forward_runs``), for the UI.

The walk-forward chain (app.services.wf_chain) writes its report FILES exactly as before; this module
writes the same result to one DB row per chain so the UI can show it without the report directory:

* ``RunStore``: the live writer. ``wf_chain.run_cell`` calls it as the chain advances (begin, each fold
  started / done / deferred, refit started, finish, fail) when the runner offers one
  (``runner.wf_store(spec, report_dir)``; the real runner does, test fakes do not). A write that fails
  RAISES: the cell then fails loudly like any other cell error (``run_cells`` records ERROR with the
  traceback), never silently without its DB row.
* ``import_report_dir``: loads a report directory written elsewhere (``reports/goal2027_wf/`` from
  another host, or one ``tools/run_walk_forward.py`` prefix) into the table, idempotently.

Both build the row's report with ONE function (``build_report``) so a live run and an imported one have
the same shape. The report is JSON, keyed like the engine's own records: every rank is
``asdict(walk_forward.GenomeFold)`` (fold, rank, is_*, oos_*, efficiency, passed, fail_reasons).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services import walk_forward as WF
from app.services.walk_forward import WalkForwardError

logger = logging.getLogger(__name__)

REPORT_SCHEMA = 1

#: Row status of each cell verdict (wf_chain ``report["verdict"]``; ERROR is run_cells' own).
VERDICT_STATUS = {"PASS": "pass", "FAIL": "fail", "PENDING": "deferred-pending", "ERROR": "error"}


def _now() -> datetime:
    return datetime.now()


# --------------------------------------------------------------------------------- spec


def spec_to_json(spec: Any) -> Dict[str, Any]:
    """The JSON a row stores for a ``wf_chain.CellSpec`` (the launcher path is resolved at run time
    and not stored). ``spec_from_json`` inverts it."""
    th = spec.thresholds
    return {
        "train_start": spec.train_start.isoformat(),
        "test_years": [int(y) for y in spec.test_years],
        "data_end": spec.data_end.isoformat() if spec.data_end else None,
        "first_generations": int(spec.first_generations),
        "next_generations": int(spec.next_generations),
        "top_n": int(spec.top_n),
        "thresholds": {"min_oos_return": th.min_oos_return, "max_dd_mult": th.max_dd_mult,
                       "min_folds": th.min_folds, "max_oos_dd": th.max_oos_dd},
        "require_test_years": [int(y) for y in spec.require_test_years],
        "exclusive_return": bool(spec.exclusive_return),
        "defer_last_test": bool(spec.defer_last_test),
        "run_all_folds": bool(spec.run_all_folds),
        "refit": bool(getattr(spec, "refit", True)),
        "test_parallel": int(spec.test_parallel),
        "selection": dict(spec.selection),
        "optimize_args": list(spec.optimize_args),
        "floor": spec.floor.isoformat() if spec.floor else None,
        "expert": spec.expert,
        "strategy": spec.strategy,
        "band": spec.band,
        # chain_seed / restarts / verdict_mode + min_oos_score / min_train_years: only when they
        # differ from the original chain, so a default spec's stored JSON is unchanged
        **spec.non_default_settings(),
    }


#: The redesign settings a stored spec may carry. ABSENT = the original chain's value: every row
#: written before these settings existed was run under exactly those (not a fallback default).
_ORIGINAL_CHAIN = {"chain_seed": True, "restarts": 1, "verdict_mode": "winner", "min_oos_score": None,
                   "min_train_years": None}


def spec_from_json(d: Dict[str, Any], launcher: Any) -> Any:
    """The ``CellSpec`` of a stored spec. Every key is required (no defaults): a row whose spec lacks
    one (an imported row has no optimize args) cannot be run, and says which key is missing. The
    redesign settings (``_ORIGINAL_CHAIN``) are stored only when not the original chain's, so their
    absence means that chain."""
    from app.services import wf_chain as C
    if d.get("optimize_args") is None:
        raise WalkForwardError("this run has no stored optimize args (imported from a report "
                               "directory): it can be viewed, not run or resumed here")
    try:
        th = d["thresholds"]
        return C.CellSpec(
            name=d["name"], optimize_args=list(d["optimize_args"]), launcher=launcher,
            train_start=date.fromisoformat(d["train_start"]),
            test_years=[int(y) for y in d["test_years"]],
            data_end=date.fromisoformat(d["data_end"]) if d["data_end"] else None,
            first_generations=int(d["first_generations"]), next_generations=int(d["next_generations"]),
            top_n=int(d["top_n"]),
            thresholds=WF.Thresholds(float(th["min_oos_return"]), float(th["max_dd_mult"]),
                                     int(th["min_folds"]),
                                     None if th["max_oos_dd"] is None else float(th["max_oos_dd"])),
            require_test_years=tuple(int(y) for y in d["require_test_years"]),
            test_parallel=int(d["test_parallel"]), selection=dict(d["selection"]),
            expert=d["expert"], strategy=d["strategy"], band=d["band"],
            floor=date.fromisoformat(d["floor"]) if d["floor"] else None,
            exclusive_return=bool(d["exclusive_return"]), defer_last_test=bool(d["defer_last_test"]),
            run_all_folds=bool(d["run_all_folds"]), refit=bool(d["refit"]),
            chain_seed=bool(d["chain_seed"]) if "chain_seed" in d else _ORIGINAL_CHAIN["chain_seed"],
            restarts=int(d["restarts"]) if "restarts" in d else _ORIGINAL_CHAIN["restarts"],
            verdict_mode=(str(d["verdict_mode"]) if "verdict_mode" in d
                          else _ORIGINAL_CHAIN["verdict_mode"]),
            min_oos_score=(None if d.get("min_oos_score") is None else float(d["min_oos_score"])),
            min_train_years=(None if d.get("min_train_years") is None
                             else float(d["min_train_years"])))
    except KeyError as e:
        raise WalkForwardError(f"stored walk-forward spec lacks {e.args[0]!r}") from e


# --------------------------------------------------------------------------------- report


def fold_window(f: WF.Fold) -> Dict[str, Any]:
    return {"index": f.index, "train_start": f.train_start.isoformat(),
            "train_end": f.train_end.isoformat(), "test_start": f.test_start.isoformat(),
            "test_end": f.test_end.isoformat()}


def _fold_of(w: Dict[str, Any]) -> Optional[WF.Fold]:
    if not w.get("train_start") or not w.get("test_start"):
        return None
    return WF.Fold(int(w["index"]), date.fromisoformat(w["train_start"]),
                   date.fromisoformat(w["train_end"]), date.fromisoformat(w["test_start"]),
                   date.fromisoformat(w["test_end"]))


def _partial_stitch(windows: Sequence[Dict[str, Any]], genomes: Sequence[Dict[str, Any]],
                    done: Iterable[int]) -> Optional[Dict[str, Any]]:
    """Stitch of the rank-1 genomes of the folds done so far (the engine's ``stitch``), for a run that
    has not reached its final report yet. None when no fold is done."""
    by_index = {int(w["index"]): _fold_of(w) for w in windows}
    items = []
    for k in sorted(done):
        g = next((x for x in genomes if int(x["fold"]) == k and int(x["rank"]) == 1), None)
        if g is None or by_index.get(k) is None:
            continue
        items.append((by_index[k], WF.GenomeFold(**g)))
    if not items:
        return None
    return asdict(WF.stitch(items))


def build_report(*, name: str, windows: Sequence[Dict[str, Any]], genomes: Sequence[Dict[str, Any]],
                 states: Dict[int, str], generations: Dict[int, Any] = None,
                 identities: Dict[int, Optional[Dict[str, Any]]] = None,
                 seeded_from: Dict[int, Optional[str]] = None, cell: Optional[Dict[str, Any]] = None,
                 walk_forward: Optional[Dict[str, Any]] = None,
                 thresholds: Optional[Dict[str, Any]] = None,
                 selection: Optional[Dict[str, Any]] = None, top_n: Optional[int] = None,
                 refit_running: Optional[str] = None, planned_known: bool = True) -> Dict[str, Any]:
    """The row's report: one entry per planned fold (windows, state, train job + optimization id, the
    top-N ranks as ``asdict(GenomeFold)``), the stitched out-of-sample result (the final one from
    ``walk_forward`` when the chain finished, else the stitch of the folds done so far), the verdict,
    refit / deferred / eliminated blocks of the cell report, and the thresholds used.

    ``states``: planned | running | done | deferred | eliminated per fold index. ``planned_known``
    False: the number of planned folds is not recorded anywhere (an imported chain with fold files
    only); ``folds`` then lists the folds known, not the plan.

    ``overall_passed`` is only ever set for a PASS / FAIL verdict: a PENDING or unfinished chain has
    no overall verdict, whatever an intermediate aggregate said."""
    generations, identities, seeded_from = generations or {}, identities or {}, seeded_from or {}
    cell_folds = {int(x["fold"]): x for x in (cell or {}).get("folds", [])}
    folds = []
    for w in windows:
        k = int(w["index"])
        ranks = sorted((dict(g) for g in genomes if int(g["fold"]) == k), key=lambda g: g["rank"])
        ident = identities.get(k)
        cf = cell_folds.get(k, {})
        winner = next((g for g in ranks if g["rank"] == 1), None)
        folds.append({
            **{key: w.get(key) for key in ("index", "train_start", "train_end", "test_start",
                                            "test_end")},
            "state": states.get(k, "planned"),
            # the tested search's job: the winning restart's when the fold had restarts
            "train_job": cf.get("job", WF.train_job_name(name, k)),
            "train_optimization_id": (ident or {}).get("id"),
            "generations": cf.get("generations", generations.get(k)),
            "seeded_from": cf.get("seeded_from", seeded_from.get(k)),
            "winner_passed": None if winner is None else bool(winner["passed"]),
            "ranks": ranks,
            **{key: cf[key] for key in ("restarts", "set") if key in cf},
        })
    done = [k for k, s in states.items() if s == "done"]
    final = walk_forward is not None
    stitch = ((walk_forward or {}).get("winner_stitch") if final
              else _partial_stitch(windows, genomes, done))
    c = cell or {}
    decided = c.get("verdict") in ("PASS", "FAIL")
    return {
        "schema": REPORT_SCHEMA,
        "name": name,
        "final": final,
        "verdict": c.get("verdict"),
        "folds": folds,
        "winner_stitch": stitch,
        "stitch_partial": (not final) and stitch is not None,
        "rank_stitches": (walk_forward or {}).get("rank_stitches") or {},
        "winners_passed": (walk_forward or {}).get("winners_passed",
                                                   sum(1 for f in folds if f["state"] == "done"
                                                       and f["winner_passed"])),
        "overall_passed": (walk_forward or {}).get("overall_passed") if decided else None,
        "planned_folds_known": planned_known,
        "complete": (walk_forward or {}).get("complete"),
        "notes": (walk_forward or {}).get("notes") or [],
        "required_failed": c.get("required_failed") or (walk_forward or {}).get("required_failed") or [],
        "refit_job": c.get("refit_job"),
        "refit": c.get("refit"),
        "refit_running": refit_running,
        "deferred": c.get("deferred"),
        "eliminated": c.get("eliminated"),
        "data_end": c.get("data_end"),
        "thresholds": c.get("thresholds") or thresholds,
        "selection": (walk_forward or {}).get("selection") or selection or {},
        "top_n": (walk_forward or {}).get("top_n", top_n),
        # Redesign keys (restarts, chain seeding off, set-score verdict, dropped folds): present only
        # when the cell report carries them, so a default chain's row report is unchanged.
        **{key: c[key] for key in _REDESIGN_KEYS if key in c},
    }


#: Cell-report keys of the 2026-11-17 redesign (wf_chain.CellSpec.non_default_settings + results).
_REDESIGN_KEYS = ("chain_seed", "restarts", "verdict_mode", "min_oos_score", "min_train_years",
                  "skipped_folds", "set_score")


def summary_columns(report: Dict[str, Any]) -> Dict[str, Any]:
    """The light list columns of a report: folds done/planned and the stitched out-of-sample result."""
    st = report.get("winner_stitch") or {}
    return {"planned_folds": len(report["folds"]) if report.get("planned_folds_known", True) else None,
            "folds_done": sum(1 for f in report["folds"] if f["state"] == "done"),
            "stitched_return": st.get("total_return"),
            "worst_oos_dd": st.get("worst_fold_dd"),
            "stitched_trades": st.get("trades"),
            "stitch_partial": bool(report.get("stitch_partial"))}


# --------------------------------------------------------------------------------- live writer


def _session_factory(factory: Optional[Callable] = None) -> Callable:
    if factory is not None:
        return factory
    from app.models.database import SessionLocal
    return SessionLocal


class RunStore:
    """The live writer of ONE chain's row, identified by (name, lane of ``report_dir``), driven by
    ``wf_chain.run_cell``.

    OWNERSHIP (``begin``). One row per (cell name, report directory): two lanes using the same cell
    name never share a row. Within a lane, ``run_cell`` holds the chain's driver lock
    (``<report_dir>/<name>/.lock``, the real runner's ``exclusive``) BEFORE ``begin``, so for a
    grid / CLI chain (``source="cli"``) any other driver of that row is gone: a ``running`` row left
    by a stopped grid is taken over, never refused, and the row records who drives it now (report
    directory, host, pid). A UI run (``source="ui"``) is additionally owned by its start token: a
    ``running`` row driven under another token is refused while that driver is not provably dead.
    Taking over a row also makes it this driver's (``source``), so a later re-import never
    overwrites live progress with older files.

    FAILURES. ``source="ui"``: every write raises (the UI run fails loudly; its row is all the UI
    has). ``source="cli"``: a write is retried with backoff; when it still fails, ONE loud ERROR is
    logged, the store goes DEGRADED (no further attempts) and the chain carries on: the report files
    are the source of truth and ``tools/import_wf_reports.py <report_dir>`` repairs the row. An
    ownership refusal in ``begin`` is fatal for both. ``fail`` before ``begin`` (a refused driver) is
    a no-op."""

    RETRY_DELAYS = (0.5, 1.0, 2.0)

    def __init__(self, name: str, *, report_dir: str, source: str = "cli",
                 token: Optional[str] = None, task_id: Optional[str] = None,
                 session_factory: Optional[Callable] = None, identity: Optional[Dict] = None):
        from app.models.walk_forward_run import lane_key
        if source not in ("cli", "ui"):
            raise ValueError(f"RunStore source must be 'cli' or 'ui', got {source!r}")
        self.name = name
        self.source = source
        self.token = token
        self.task_id = task_id
        self.report_dir = os.path.abspath(report_dir)
        self.lane = lane_key(report_dir)
        self._S = _session_factory(session_factory)
        self._identity = identity
        self.begun = False
        self.degraded = False
        self.windows: List[Dict[str, Any]] = []
        self.genomes: Dict[int, List[Dict[str, Any]]] = {}
        self.states: Dict[int, str] = {}
        self.generations: Dict[int, Any] = {}
        self.identities: Dict[int, Optional[Dict[str, Any]]] = {}
        self.spec_json: Dict[str, Any] = {}
        self.refit_running: Optional[str] = None

    # -- helpers
    def _driver(self) -> Dict[str, Any]:
        if self._identity is None:
            from app.services.optimization_resume import process_identity
            self._identity = process_identity()
        return self._identity

    def _query(self, db):
        from app.models.walk_forward_run import WalkForwardRun
        return db.query(WalkForwardRun).filter(WalkForwardRun.name == self.name,
                                               WalkForwardRun.lane_key == self.lane)

    def _report(self, cell=None, walk_forward=None) -> Dict[str, Any]:
        th = self.spec_json.get("thresholds")
        return build_report(
            name=self.name, windows=self.windows,
            genomes=[g for k in sorted(self.genomes) for g in self.genomes[k]],
            states=self.states, generations=self.generations, identities=self.identities,
            cell=cell, walk_forward=walk_forward,
            thresholds=None if th is None else {**th, "top_n": self.spec_json.get("top_n"),
                                                "exclusive_return": self.spec_json.get("exclusive_return"),
                                                "require_test_years": self.spec_json.get("require_test_years")},
            selection=self.spec_json.get("selection"), top_n=self.spec_json.get("top_n"),
            refit_running=self.refit_running)

    def _attempt(self, what: str, fn: Callable[[], None]) -> None:
        """Run one DB write: fatal for a UI run; retried, then degraded (logged once) for a grid one."""
        if self.degraded:
            return
        if self.source == "ui":
            fn()
            return
        last: Optional[Exception] = None
        for delay in (0.0, *self.RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                fn()
                return
            except WalkForwardError:
                raise                     # an ownership refusal, not a DB failure
            except Exception as e:  # noqa: BLE001 -- a DB failure of the side record; see below
                last = e
        self.degraded = True
        logger.error(
            "[%s] walk_forward_runs row could NOT be written (%s, %d attempts: %s: %s). The chain "
            "CONTINUES and this cell's row is no longer updated; the report files under %s are the "
            "source of truth -- repair the row with: python tools/import_wf_reports.py %s",
            self.name, what, 1 + len(self.RETRY_DELAYS), type(last).__name__, last,
            os.path.join(self.report_dir, self.name), self.report_dir)

    def _write(self, **values) -> None:
        def go():
            db = self._S()
            try:
                row = self._query(db).first()
                if row is None:
                    raise RuntimeError(f"walk_forward_runs row {self.name!r} ({self.report_dir}) "
                                       f"disappeared while its chain was running")
                for k, v in values.items():
                    setattr(row, k, v)
                row.updated_at = _now()
                db.commit()
            finally:
                db.close()
        self._attempt(", ".join(sorted(values)) or "update", go)

    def _write_report(self, cell=None, walk_forward=None, **extra) -> None:
        report = self._report(cell, walk_forward)
        self._write(report=report, **summary_columns(report), **extra)

    def _stored_folds(self, folds: Sequence[WF.Fold]) -> None:
        """Seed the report from fold reports already on disk (a resumed chain, or a grid cell started
        before rows existed), so a restart does not blank the displayed result. Only a file whose
        windows are this fold's is used; the chain re-validates it (``_load_fold``) and re-reports
        the fold when it reuses or re-evaluates it."""
        for f in folds:
            path = os.path.join(self.report_dir, self.name, f"fold{f.index}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                key = doc["key"]
                if (key["train"] != [f.train_start.isoformat(), f.train_end.isoformat()]
                        or key["test"] != [f.test_start.isoformat(), f.test_end.isoformat()]):
                    continue
                self.genomes[f.index] = [asdict(WF.GenomeFold(**g)) for g in doc["genomes"]]
                ident = _tested_identity(doc)
                if ident is not None:
                    self.identities[f.index] = ident
                self.states[f.index] = "done"
            except (OSError, ValueError, KeyError, TypeError) as e:
                logger.warning("[%s] stored fold report %s not shown on the row (%s: %s); the chain "
                               "decides about it", self.name, path, type(e).__name__, e)

    # -- the chain's events
    def begin(self, spec: Any, folds: Sequence[WF.Fold], deferred: Optional[WF.Fold]) -> None:
        from app.models.walk_forward_run import WalkForwardRun
        from app.services.optimization_resume import ALIVE, DEAD, judge
        self.spec_json = spec_to_json(spec)
        self.windows = [fold_window(f) for f in folds]
        self.states = {f.index: "planned" for f in folds}
        self.generations = {f.index: (spec.first_generations if f.index == 1 or not spec.chain_seed
                                      else spec.next_generations)
                            for f in folds}
        self._stored_folds(folds)
        drv = self._driver()

        def go():
            db = self._S()
            try:
                row = self._query(db).first()
                if (row is not None and self.source == "ui" and row.status == "running"
                        and row.driver_token != self.token):
                    state, why = judge(row.driver_host, row.driver_pid, row.driver_pid_started,
                                       row.driver_identity)
                    mine = (row.driver_host, row.driver_pid) == (drv["host"], drv["pid"])
                    if state != DEAD and not mine:
                        raise WalkForwardError(
                            f"walk-forward run {self.name!r} is being run by another driver "
                            f"(pid {row.driver_pid} on {row.driver_host}: {why})"
                            + ("" if state == ALIVE else "; it is not provably dead, so it is not "
                                                         "taken over"))
                if row is not None and row.status == "running" and row.driver_pid is not None:
                    logger.info("[%s] taking over the walk_forward_runs row last driven by pid %s on "
                                "%s (%s)", self.name, row.driver_pid, row.driver_host, row.source)
                if row is None:
                    row = WalkForwardRun(name=self.name, lane_key=self.lane, created_at=_now())
                    db.add(row)
                row.source = self.source
                row.status = "running"
                row.expert, row.strategy, row.band = spec.expert, spec.strategy, spec.band
                row.spec = {**self.spec_json, "name": self.name}
                row.error_message = None
                row.completed_at = None
                row.started_at = _now()
                row.updated_at = row.started_at
                row.current_fold = None
                row.progress_message = "starting"
                row.driver_token = self.token
                row.driver_host, row.driver_pid = drv["host"], drv["pid"]
                row.driver_pid_started, row.driver_identity = drv["pid_started"], drv.get("proc_identity")
                if self.task_id is not None:
                    row.task_id = self.task_id
                row.report_dir = self.report_dir
                report = self._report()
                row.report = report
                for k, v in summary_columns(report).items():
                    setattr(row, k, v)
                db.commit()
            finally:
                db.close()
        self._attempt("begin", go)
        self.begun = True

    def fold_started(self, f: WF.Fold) -> None:
        self.states[f.index] = "running"
        self._write_report(current_fold=f.index,
                           progress_message=f"fold {f.index}: train "
                                            f"{f.train_start}..{f.train_end}, test "
                                            f"{f.test_start}..{f.test_end}")

    def fold_done(self, f: WF.Fold, genomes: Sequence[WF.GenomeFold],
                  identity: Optional[Dict[str, Any]] = None, reused: bool = False) -> None:
        self.states[f.index] = "done"
        self.genomes[f.index] = [asdict(g) for g in genomes]
        if identity is not None:
            self.identities[f.index] = identity
        self._write_report(progress_message=f"fold {f.index}: done"
                                            + (" (stored report reused)" if reused else ""))

    def fold_deferred(self, f: WF.Fold) -> None:
        self.states[f.index] = "deferred"
        self.genomes.pop(f.index, None)
        self._write_report(progress_message=f"fold {f.index}: trained; its test "
                                            f"{f.test_start}..{f.test_end} waits for the data")

    def refit_started(self, job: str) -> None:
        self.refit_running = job
        self._write_report(current_fold=None, progress_message=f"refit {job}: training")

    def finish(self, cell: Dict[str, Any], res: WF.WalkForwardResult) -> None:
        self.refit_running = None
        el = cell.get("eliminated")
        for k in list(self.states):
            if el and k >= int(el["before_fold"]):
                self.states[k] = "eliminated"
                self.genomes.pop(k, None)
        status = VERDICT_STATUS[cell["verdict"]]
        self._write_report(cell, WF.result_to_json(res), status=status, current_fold=None,
                           progress_message=f"verdict {cell['verdict']}",
                           completed_at=_now())

    def fail(self, exc: BaseException) -> None:
        if not self.begun:
            return
        self.refit_running = None
        self._write_report(status="error", error_message=f"{type(exc).__name__}: {exc}",
                           progress_message="error", completed_at=_now())


# --------------------------------------------------------------------------------- import

_PARTIAL = re.compile(r"PARTIAL RUN: (\d+) of (\d+) folds")


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tested_identity(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Identity of the job whose genomes a stored fold report holds: the last of its key's train jobs
    (the fold's own job), or with restarts the winning search's: restarts run unchained, so the key
    lists exactly this fold's restarts in order (wf_chain._identities)."""
    key = doc["key"]
    if not key.get("train_jobs"):
        return None
    won = [r for r in doc.get("restarts") or [] if r.get("won")]
    if won:
        return key["train_jobs"][int(won[0]["restart"]) - 1]
    return key["train_jobs"][-1]


def _fold_files(cell_dir: str) -> Dict[int, Dict[str, Any]]:
    out = {}
    for fn in os.listdir(cell_dir):
        m = re.fullmatch(r"fold(\d+)\.json", fn)
        if m:
            out[int(m.group(1))] = _read_json(os.path.join(cell_dir, fn))
    return out


def _has_fold_files(d: str) -> bool:
    """Whether ``d`` holds fold<k>.json files (by name only: a corrupt file is reported per chain)."""
    return any(re.fullmatch(r"fold(\d+)\.json", fn) for fn in os.listdir(d))


def report_from_files(name: str, cell_dir: Optional[str],
                      summary_cell: Optional[Dict[str, Any]] = None,
                      base_dir: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(row values, report) of one chain from its report files: ``cell.json`` (the cell's final report,
    wf_chain), ``report.json`` (the engine's aggregate, also what ``tools/run_walk_forward.py`` writes)
    and ``fold<k>.json`` (the stored per-fold records, present for every completed fold even when the
    chain later failed). ``summary_cell`` is the cell's entry in the grid's ``summary.json``, the
    latest word on its verdict (an ERROR there wins over an older ``cell.json``). ``base_dir``: the
    chain's report directory (the parent of its cell directory), which is the row's lane.

    A corrupt or incomplete file raises (``ValueError`` / ``KeyError`` / ``TypeError`` / ``OSError``):
    the importer records that chain as an import error and goes on with the others."""
    cell = _read_json(os.path.join(cell_dir, "cell.json")) if cell_dir else None
    wf = _read_json(os.path.join(cell_dir, "report.json")) if cell_dir else None
    fold_docs = _fold_files(cell_dir) if cell_dir else {}
    if cell is None and wf is None and not fold_docs and summary_cell is None:
        raise WalkForwardError(f"{cell_dir}: no cell.json, report.json, fold<k>.json or summary entry")
    windows: Dict[int, Dict[str, Any]] = {}
    genomes: Dict[int, List[Dict[str, Any]]] = {}
    identities: Dict[int, Optional[Dict[str, Any]]] = {}
    states: Dict[int, str] = {}
    for k, doc in fold_docs.items():
        if not isinstance(doc, dict) or not isinstance(doc.get("key"), dict):
            raise KeyError(f"fold{k}.json has no 'key' record")
        key = doc["key"]
        windows[k] = {"index": k, "train_start": key["train"][0], "train_end": key["train"][1],
                      "test_start": key["test"][0], "test_end": key["test"][1]}
        genomes[k] = list(doc["genomes"])
        ident = _tested_identity(doc)
        if ident is not None:
            identities[k] = ident
        states[k] = "done"
    for f in (wf or {}).get("folds", []):
        windows[int(f["index"])] = {"index": int(f["index"]), **{x: f[x] for x in (
            "train_start", "train_end", "test_start", "test_end")}}
        states[int(f["index"])] = "done"
    for k in {int(g["fold"]) for g in (wf or {}).get("genomes", [])}:
        genomes[k] = [g for g in wf["genomes"] if int(g["fold"]) == k]
    generations, seeded = {}, {}
    for cf in (cell or {}).get("folds", []):
        k = int(cf["fold"])
        windows.setdefault(k, {"index": k, "train_start": cf["train"][0], "train_end": cf["train"][1],
                               "test_start": cf["test"][0], "test_end": cf["test"][1]})
        states[k] = "done"
        generations[k] = cf.get("generations")
        seeded[k] = cf.get("seeded_from")
    d = (cell or {}).get("deferred")
    if d:
        k = int(d["fold"])
        windows[k] = {"index": k, "train_start": d["train"][0], "train_end": d["train"][1],
                      "test_start": d["test"][0], "test_end": d["test"][1]}
        states[k] = "deferred"
    planned = max(windows) if windows else 0
    # The plan's size is recorded by report.json only (all folds when complete, else the PARTIAL note).
    planned_known = wf is not None
    for note in (wf or {}).get("notes", []):
        m = _PARTIAL.search(note)
        if m:
            planned = max(planned, int(m.group(2)))
    el = (cell or {}).get("eliminated")
    if el:
        planned = max(planned, int(el["before_fold"]))
    for k in range(1, planned + 1):
        if k not in windows:
            windows[k] = {"index": k, "train_start": None, "train_end": None, "test_start": None,
                          "test_end": None}
            states[k] = "eliminated" if el and k >= int(el["before_fold"]) else "planned"
    verdict = (summary_cell or {}).get("verdict") or (cell or {}).get("verdict")
    if verdict is None and wf is not None:
        verdict = ("PASS" if wf.get("overall_passed") else
                   "FAIL" if wf.get("overall_passed") is False else None)
    final_cell = dict(cell) if cell else ({"verdict": verdict} if verdict else None)
    if final_cell is not None and verdict:
        final_cell["verdict"] = verdict
    th = (cell or {}).get("thresholds") or (wf or {}).get("thresholds")
    report = build_report(
        name=name, windows=[windows[k] for k in sorted(windows)],
        genomes=[g for k in sorted(genomes) for g in genomes[k]], states=states,
        generations=generations, identities=identities, seeded_from=seeded, cell=final_cell,
        walk_forward=wf if (cell is not None or verdict in ("PASS", "FAIL")) and wf is not None else None,
        thresholds=th, selection=(wf or {}).get("selection"), top_n=(wf or {}).get("top_n"),
        planned_known=planned_known)
    # No verdict anywhere and no summary entry: the chain had not finished when the files were copied
    # (or its driver was killed). Said as such, never as a live "running".
    status = VERDICT_STATUS[verdict] if verdict else "incomplete"
    sc = summary_cell or {}
    src = cell or sc
    windows_known = [w for w in report["folds"] if w["train_start"]]
    spec = {
        "name": name, "imported": True,
        "train_start": windows_known[0]["train_start"] if windows_known else None,
        "test_years": sorted({int(w["test_start"][:4]) for w in windows_known}),
        "data_end": (cell or {}).get("data_end"),
        "first_generations": generations.get(1), "next_generations": generations.get(2),
        "top_n": report["top_n"], "thresholds": th, "selection": report["selection"],
        "require_test_years": (th or {}).get("require_test_years"),
        "exclusive_return": (th or {}).get("exclusive_return", (wf or {}).get("exclusive_return")),
        "optimize_args": None,
    }
    values = {
        "expert": src.get("expert"), "strategy": src.get("strategy"), "band": src.get("band"),
        "status": status, "spec": spec, "report": report,
        "error_message": sc.get("error"),
        "report_dir": os.path.abspath(base_dir) if base_dir else None,
        **summary_columns(report),
    }
    return values, report


def _cells_in(report_dir: str) -> List[Tuple[str, Optional[str], Optional[Dict[str, Any]], str]]:
    """(name, cell dir or None, summary entry or None, base dir) of every chain in ``report_dir``: a
    grid directory (``summary.json`` + one sub-directory per cell; base = it), or ONE chain's
    directory (base = its parent)."""
    report_dir = os.path.abspath(report_dir)
    if not os.path.isdir(report_dir):
        raise WalkForwardError(f"{report_dir} is not a directory")
    own = any(os.path.exists(os.path.join(report_dir, f)) for f in ("cell.json", "report.json")) \
        or _has_fold_files(report_dir)
    if own:
        return [(os.path.basename(report_dir.rstrip(os.sep)), report_dir, None,
                 os.path.dirname(report_dir.rstrip(os.sep)))]
    summary = _read_json(os.path.join(report_dir, "summary.json"))
    by_name = {c["cell"]: c for c in (summary or {}).get("cells", [])}
    names = set(by_name)
    for d in os.listdir(report_dir):
        p = os.path.join(report_dir, d)
        if os.path.isdir(p) and (any(os.path.exists(os.path.join(p, f)) for f in ("cell.json", "report.json"))
                                 or _has_fold_files(p)):
            names.add(d)
    out = []
    for n in sorted(names):
        p = os.path.join(report_dir, n)
        out.append((n, p if os.path.isdir(p) else None, by_name.get(n), report_dir))
    return out


def import_report_dir(report_dir: str, session_factory: Optional[Callable] = None, *,
                      replace_live: bool = False, dry_run: bool = False) -> List[Tuple[str, str]]:
    """Load every chain of ``report_dir`` into ``walk_forward_runs``; returns (name, action) with action
    created | updated | unchanged | skipped (<why>) | error (<why>). The row of a chain is (name,
    lane of its report directory): importing a copy of another host's grid never touches this DB's
    own rows of the same cell names in another directory. IDEMPOTENT: an imported row whose content is
    equal is left alone. A row this DB's own runs wrote in the SAME lane (source ui / cli) is never
    overwritten unless ``replace_live``. A chain whose files are corrupt is reported as an error and
    the others are still imported."""
    from app.models.walk_forward_run import WalkForwardRun, lane_key
    S = _session_factory(session_factory)
    out: List[Tuple[str, str]] = []
    for name, cell_dir, summary_cell, base in _cells_in(report_dir):
        try:
            values, _ = report_from_files(name, cell_dir, summary_cell, base)
        except (WalkForwardError, OSError, ValueError, KeyError, TypeError) as e:
            out.append((name, f"error ({type(e).__name__}: {e}); not imported"))
            continue
        lane = lane_key(base)
        db = S()
        try:
            row = (db.query(WalkForwardRun)
                   .filter(WalkForwardRun.name == name, WalkForwardRun.lane_key == lane).first())
            if row is not None and row.source != "import" and not replace_live:
                out.append((name, f"skipped (a {row.source} row of this DB; --replace-live to "
                                  f"overwrite it)"))
                continue
            if row is None:
                if not dry_run:
                    db.add(WalkForwardRun(name=name, lane_key=lane, source="import",
                                          created_at=_now(), updated_at=_now(), **values))
                    db.commit()
                out.append((name, "created"))
                continue
            same = all(_canon(getattr(row, k)) == _canon(v) for k, v in values.items())
            if same and row.source == "import":
                out.append((name, "unchanged"))
                continue
            if not dry_run:
                for k, v in values.items():
                    setattr(row, k, v)
                row.source = "import"
                row.updated_at = _now()
                db.commit()
            out.append((name, "updated"))
        finally:
            db.close()
    return out


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)
