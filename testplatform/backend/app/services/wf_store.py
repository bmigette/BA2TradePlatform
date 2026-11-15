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
import os
import re
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services import walk_forward as WF
from app.services.walk_forward import WalkForwardError

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
    }


def spec_from_json(d: Dict[str, Any], launcher: Any) -> Any:
    """The ``CellSpec`` of a stored spec. Every key is required (no defaults): a row whose spec lacks
    one (an imported row has no optimize args) cannot be run, and says which key is missing."""
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
            run_all_folds=bool(d["run_all_folds"]), refit=bool(d["refit"]))
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
                 refit_running: Optional[str] = None) -> Dict[str, Any]:
    """The row's report: one entry per planned fold (windows, state, train job + optimization id, the
    top-N ranks as ``asdict(GenomeFold)``), the stitched out-of-sample result (the final one from
    ``walk_forward`` when the chain finished, else the stitch of the folds done so far), the verdict,
    refit / deferred / eliminated blocks of the cell report, and the thresholds used.

    ``states``: planned | running | done | deferred | eliminated per fold index."""
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
            "train_job": WF.train_job_name(name, k),
            "train_optimization_id": (ident or {}).get("id"),
            "generations": cf.get("generations", generations.get(k)),
            "seeded_from": cf.get("seeded_from", seeded_from.get(k)),
            "winner_passed": None if winner is None else bool(winner["passed"]),
            "ranks": ranks,
        })
    done = [k for k, s in states.items() if s == "done"]
    final = walk_forward is not None
    stitch = ((walk_forward or {}).get("winner_stitch") if final
              else _partial_stitch(windows, genomes, done))
    c = cell or {}
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
        "overall_passed": (walk_forward or {}).get("overall_passed"),
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
    }


def summary_columns(report: Dict[str, Any]) -> Dict[str, Any]:
    """The light list columns of a report: folds done/planned and the stitched out-of-sample result."""
    st = report.get("winner_stitch") or {}
    return {"planned_folds": len(report["folds"]),
            "folds_done": sum(1 for f in report["folds"] if f["state"] == "done"),
            "stitched_return": st.get("total_return"),
            "worst_oos_dd": st.get("worst_fold_dd"),
            "stitched_trades": st.get("trades")}


# --------------------------------------------------------------------------------- live writer


def _session_factory(factory: Optional[Callable] = None) -> Callable:
    if factory is not None:
        return factory
    from app.models.database import SessionLocal
    return SessionLocal


class RunStore:
    """The live writer of ONE chain's row (by name), driven by ``wf_chain.run_cell``.

    ``begin`` claims the row for this driver: it refuses a row another driver is still running
    (recorded process alive or not provably dead), so a second driver never overwrites a live run's
    row. Only a store that has begun writes anything afterwards (``fail`` before ``begin`` is a no-op:
    a refused driver must not mark the live run's row as failed). Every write commits at once and
    raises on failure."""

    def __init__(self, name: str, *, source: str = "cli", token: Optional[str] = None,
                 task_id: Optional[str] = None, report_dir: Optional[str] = None,
                 session_factory: Optional[Callable] = None, identity: Optional[Dict] = None):
        self.name = name
        self.source = source
        self.token = token
        self.task_id = task_id
        self.report_dir = report_dir
        self._S = _session_factory(session_factory)
        self._identity = identity
        self.begun = False
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

    def _write(self, **values) -> None:
        from app.models.walk_forward_run import WalkForwardRun
        db = self._S()
        try:
            row = db.query(WalkForwardRun).filter(WalkForwardRun.name == self.name).first()
            if row is None:
                raise WalkForwardError(f"walk_forward_runs row {self.name!r} disappeared while its "
                                       f"chain was running")
            for k, v in values.items():
                setattr(row, k, v)
            row.updated_at = _now()
            db.commit()
        finally:
            db.close()

    def _write_report(self, cell=None, walk_forward=None, **extra) -> Dict[str, Any]:
        report = self._report(cell, walk_forward)
        self._write(report=report, **summary_columns(report), **extra)
        return report

    # -- the chain's events
    def begin(self, spec: Any, folds: Sequence[WF.Fold], deferred: Optional[WF.Fold]) -> None:
        from app.models.walk_forward_run import WalkForwardRun
        from app.services.optimization_resume import ALIVE, DEAD, judge
        self.spec_json = spec_to_json(spec)
        self.windows = [fold_window(f) for f in folds]
        self.states = {f.index: "planned" for f in folds}
        self.generations = {f.index: (spec.first_generations if f.index == 1 else spec.next_generations)
                            for f in folds}
        drv = self._driver()
        db = self._S()
        try:
            row = db.query(WalkForwardRun).filter(WalkForwardRun.name == self.name).first()
            if row is not None and row.status == "running" and row.driver_token != self.token:
                state, why = judge(row.driver_host, row.driver_pid, row.driver_pid_started,
                                   row.driver_identity)
                mine = (row.driver_host, row.driver_pid) == (drv["host"], drv["pid"])
                if state != DEAD and not mine:
                    raise WalkForwardError(
                        f"walk-forward run {self.name!r} is being run by another driver "
                        f"(pid {row.driver_pid} on {row.driver_host}: {why})"
                        + ("" if state == ALIVE else "; it is not provably dead, so it is not "
                                                     "taken over"))
            if row is None:
                row = WalkForwardRun(name=self.name, source=self.source, status="running",
                                     created_at=_now())
                db.add(row)
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
            if self.report_dir is not None:
                row.report_dir = os.path.abspath(self.report_dir)
            report = self._report()
            row.report = report
            for k, v in summary_columns(report).items():
                setattr(row, k, v)
            db.commit()
        finally:
            db.close()
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
        self._write_report(progress_message=f"fold {f.index}: trained; its test "
                                            f"{f.test_start}..{f.test_end} waits for the data")

    def refit_started(self, job: str) -> None:
        self.refit_running = job
        self._write_report(current_fold=None, progress_message=f"refit {job}: training")

    def finish(self, cell: Dict[str, Any], res: WF.WalkForwardResult) -> None:
        self.refit_running = None
        el = cell.get("eliminated")
        if el:
            for k in self.states:
                if k >= int(el["before_fold"]) and self.states[k] == "planned":
                    self.states[k] = "eliminated"
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


def _fold_files(cell_dir: str) -> Dict[int, Dict[str, Any]]:
    out = {}
    for fn in os.listdir(cell_dir):
        m = re.fullmatch(r"fold(\d+)\.json", fn)
        if m:
            out[int(m.group(1))] = _read_json(os.path.join(cell_dir, fn))
    return out


def report_from_files(name: str, cell_dir: Optional[str],
                      summary_cell: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(row values, report) of one chain from its report files: ``cell.json`` (the cell's final report,
    wf_chain), ``report.json`` (the engine's aggregate, also what ``tools/run_walk_forward.py`` writes)
    and ``fold<k>.json`` (the stored per-fold records, present for every completed fold even when the
    chain later failed). ``summary_cell`` is the cell's entry in the grid's ``summary.json``, the
    latest word on its verdict (an ERROR there wins over an older ``cell.json``)."""
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
        key = doc["key"]
        windows[k] = {"index": k, "train_start": key["train"][0], "train_end": key["train"][1],
                      "test_start": key["test"][0], "test_end": key["test"][1]}
        genomes[k] = list(doc["genomes"])
        if key.get("train_jobs"):
            identities[k] = key["train_jobs"][-1]
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
        thresholds=th, selection=(wf or {}).get("selection"), top_n=(wf or {}).get("top_n"))
    # No verdict anywhere: the chain had not finished when the files were copied (or its driver was
    # killed): shown as running, the state of its last write.
    status = VERDICT_STATUS[verdict] if verdict else "running"
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
        "report_dir": os.path.abspath(cell_dir) if cell_dir else None,
        **summary_columns(report),
    }
    return values, report


def _cells_in(report_dir: str) -> List[Tuple[str, Optional[str], Optional[Dict[str, Any]]]]:
    """(name, cell dir or None, summary entry or None) of every chain in ``report_dir``: a grid
    directory (``summary.json`` + one sub-directory per cell), or ONE chain's directory."""
    report_dir = os.path.abspath(report_dir)
    if not os.path.isdir(report_dir):
        raise WalkForwardError(f"{report_dir} is not a directory")
    own = any(os.path.exists(os.path.join(report_dir, f)) for f in ("cell.json", "report.json")) \
        or _fold_files(report_dir)
    if own:
        return [(os.path.basename(report_dir.rstrip(os.sep)), report_dir, None)]
    summary = _read_json(os.path.join(report_dir, "summary.json"))
    by_name = {c["cell"]: c for c in (summary or {}).get("cells", [])}
    names = set(by_name)
    for d in os.listdir(report_dir):
        p = os.path.join(report_dir, d)
        if os.path.isdir(p) and (any(os.path.exists(os.path.join(p, f)) for f in ("cell.json", "report.json"))
                                 or _fold_files(p)):
            names.add(d)
    out = []
    for n in sorted(names):
        p = os.path.join(report_dir, n)
        out.append((n, p if os.path.isdir(p) else None, by_name.get(n)))
    return out


def import_report_dir(report_dir: str, session_factory: Optional[Callable] = None, *,
                      replace_live: bool = False, dry_run: bool = False) -> List[Tuple[str, str]]:
    """Load every chain of ``report_dir`` into ``walk_forward_runs``; returns (name, action) with action
    created | updated | unchanged | skipped (<why>). IDEMPOTENT: an imported row whose content is equal
    is left alone. A row this DB's own runs wrote (source ui / cli) is never overwritten unless
    ``replace_live``: it is the live record of the same chain."""
    from app.models.walk_forward_run import WalkForwardRun
    S = _session_factory(session_factory)
    out: List[Tuple[str, str]] = []
    for name, cell_dir, summary_cell in _cells_in(report_dir):
        values, _ = report_from_files(name, cell_dir, summary_cell)
        db = S()
        try:
            row = db.query(WalkForwardRun).filter(WalkForwardRun.name == name).first()
            if row is not None and row.source != "import" and not replace_live:
                out.append((name, f"skipped (a {row.source} row of this DB; --replace-live to "
                                  f"overwrite it)"))
                continue
            if row is None:
                if not dry_run:
                    db.add(WalkForwardRun(name=name, source="import", created_at=_now(),
                                          updated_at=_now(), **values))
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
