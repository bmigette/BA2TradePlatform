"""goal2027 walk-forward CHAIN for one matrix cell, and the grid around it.

Per cell (design docs/plans/2026-10-25-goal2027-grid-design.md section 1.2):

    <cell>-wf1   train floor..2023-12-31   cold start, ``first_generations``      test 2024
    <cell>-wf2   train floor..2024-12-31   seeded from wf1's final population      test 2025
    <cell>-wf3   train floor..2025-12-31   seeded from wf2's final population      test 2026
    <cell>-refit train floor..2026-12-31   seeded from wf3, PASSING cells only     no test

Each fold is the engine's ``walk_forward.run_fold`` (train job skipped when completed, top-N selected,
out-of-sample re-run, evaluated). This module adds what the matrix needs around it: the cell's folds
(expert data floor, ``--wf-data-end`` clamp), a stored per-fold report so a completed fold is skipped
on resume, the refit, the per-cell report and the grid summary. Pure orchestration over the engine's
``Runner`` protocol: the tools supply the real runner, the tests a fake one.

Two-phase year (``--wf-defer-last-test``, docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md):
with data ending before the last test year, wf1/wf2 run whole and wf3 is only TRAINED (cell
PENDING); the same command with the full data later finds every train job completed and only runs
wf3's test, the verdict and the refit. A cell that can no longer pass stops early (FAIL, the
``eliminated`` block) unless ``run_all_folds``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.services import walk_forward as WF
from app.services.walk_forward import WalkForwardError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------- folds


def build_cell_folds(train_start: date, test_years: Sequence[int], data_end: Optional[date],
                     floor: Optional[date] = None, defer_last_test: bool = False) -> List[WF.Fold]:
    """The cell's anchored folds. ``floor`` is the expert's data floor (FMPRating 2022-01-01): the
    train start of EVERY fold and of the refit is the later of ``train_start`` and ``floor``.
    ``data_end`` (``--wf-data-end``) shortens the LAST test window; it must leave that window
    non-empty and fall inside the last test year or later (a data end before the last year would
    silently drop whole folds, so it is refused instead).

    ``defer_last_test`` (``--wf-defer-last-test``, goal2027 phase A): a data end BEFORE the last
    test year is allowed when the last fold's TRAIN window still fits in the data. That fold is then
    trained now and tested once the data exists (``deferred_fold``); its windows are never clamped,
    so the January run with the full data rebuilds the very same jobs. A data end INSIDE the last
    test year is refused under the flag: a partly clamped test and refit would carry the same job
    names as the full ones."""
    start = max(train_start, floor) if floor else train_start
    years = list(test_years)
    test_end = None
    if data_end is not None and years:
        last_start, last_end = date(years[-1], 1, 1), date(years[-1], 12, 31)
        if defer_last_test:
            if last_start <= data_end < last_end:
                raise WalkForwardError(
                    f"--wf-defer-last-test with --wf-data-end {data_end}: the data end is inside the "
                    f"last test year {years[-1]}; give one before {last_start} (train the last fold "
                    f"now, test it later) or none (test it)")
        elif data_end <= last_start:
            raise WalkForwardError(
                f"--wf-data-end {data_end}: the last test window {last_start}..{last_end} would be "
                f"empty (data must reach beyond {last_start}; --wf-defer-last-test trains that "
                f"fold now and tests it later)")
        elif data_end < last_end:
            test_end = data_end
    folds = WF.build_year_folds(start, years, test_end)
    WF.validate_folds(folds)
    last = deferred_fold(folds, data_end, defer_last_test)
    if last is not None:
        if len(folds) == 1:
            raise WalkForwardError(f"--wf-defer-last-test: the plan has one fold and it is deferred, "
                                   f"so nothing would be tested")
        if last.train_end > data_end:
            raise WalkForwardError(
                f"--wf-defer-last-test: fold {last.index} trains to {last.train_end}, after the data "
                f"end {data_end}")
    return folds


def deferred_fold(folds: Sequence[WF.Fold], data_end: Optional[date],
                  defer_last_test: bool) -> Optional[WF.Fold]:
    """The fold trained now and tested later: the last one, when its test starts after the data."""
    if not defer_last_test or data_end is None or not folds or folds[-1].test_start <= data_end:
        return None
    return folds[-1]


def refit_window(folds: Sequence[WF.Fold]) -> Tuple[date, date]:
    """The refit trains on everything: from the folds' train start to the last test end."""
    return folds[0].train_start, folds[-1].test_end


# --------------------------------------------------------------------------------- spec / result


@dataclass
class CellSpec:
    name: str                       # the cell's job name (the engine prefix)
    optimize_args: List[str]        # base ``optimize`` args WITHOUT --start/--end/--name/--generations
    launcher: Any
    train_start: date
    test_years: Sequence[int]
    data_end: Optional[date]
    first_generations: int
    next_generations: int
    top_n: int
    thresholds: WF.Thresholds
    require_test_years: Sequence[int]
    test_parallel: int
    selection: Dict[str, Any]
    expert: str
    strategy: Optional[str]
    band: str
    floor: Optional[date] = None
    exclusive_return: bool = False      # fold return must be ABOVE the threshold
    defer_last_test: bool = False       # train the last fold now, test it when the data exists
    run_all_folds: bool = False         # no early elimination: every fold runs even if the cell cannot pass


@dataclass
class CellResult:
    name: str
    verdict: str                    # PASS | FAIL | PENDING (last fold's test deferred)
    refit_job: Optional[str]
    walk_forward: WF.WalkForwardResult
    report: Dict[str, Any]


def _fold_extra_args(spec: CellSpec, folds: Sequence[WF.Fold]) -> Dict[int, List[str]]:
    return {f.index: ["--generations",
                      str(spec.first_generations if f.index == 1 else spec.next_generations)]
            for f in folds}


def make_cell_plan(spec: CellSpec) -> Tuple[WF.Plan, List[WF.Fold]]:
    folds = build_cell_folds(spec.train_start, spec.test_years, spec.data_end, spec.floor,
                             spec.defer_last_test)
    plan = WF.make_plan(
        spec.name, folds, spec.optimize_args, spec.launcher, spec.top_n, spec.thresholds,
        test_parallel=spec.test_parallel, skip_train=False, only_fold=None, embargo_days=0,
        selection=spec.selection, seed_chain=True, fold_extra_args=_fold_extra_args(spec, folds),
        require_test_years=spec.require_test_years, exclusive_return=spec.exclusive_return,
        rebase_test_data=spec.defer_last_test)
    return plan, folds


def refit_job_name(prefix: str) -> str:
    return f"{prefix}-refit"


def refit_command(spec: CellSpec, folds: Sequence[WF.Fold]) -> List[str]:
    """The refit's optimize command: whole window, ``next_generations``, seeded from the last fold.
    NO LOOK-AHEAD, asserted: the seed's train window ends before the refit window ends (the refit
    is allowed to see everything, there is no test year left to protect)."""
    start, end = refit_window(folds)
    last = folds[-1]
    if last.train_end >= end:
        raise WalkForwardError(f"refit seed {last.train_end} is not before the refit end {end}")
    head = [spec.launcher] if isinstance(spec.launcher, str) else list(spec.launcher)
    return [*head, "optimize", *spec.optimize_args, "--generations", str(spec.next_generations),
            "--chain-seed-from", WF.train_job_name(spec.name, last.index),
            "--start", start.isoformat(), "--end", end.isoformat(), "--name",
            refit_job_name(spec.name)]


# --------------------------------------------------------------------------------- fold store


def _fold_path(report_dir: str, name: str, k: int) -> str:
    return os.path.join(report_dir, name, f"fold{k}.json")


def _identities(runner: Any, plan: WF.Plan, f: WF.Fold) -> Optional[List[Any]]:
    """Identity (optimization id + completion time) of this fold's train job AND every earlier one:
    fold k is seeded from fold k-1, so retraining any of them makes fold k's report stale. None for
    a runner that cannot say (then the key carries no identity)."""
    read = getattr(runner, "train_identity", None)
    if not callable(read):
        return None
    return [read(WF.train_job_name(plan.prefix, k.index)) for k in plan.folds if k.index <= f.index]


def _fold_key(plan: WF.Plan, f: WF.Fold, identities: Optional[List[Any]] = None) -> Dict[str, Any]:
    """What a stored fold report must agree with to be trusted (anything else re-evaluates)."""
    th = plan.thresholds
    return {"train_jobs": identities,"train": [f.train_start.isoformat(), f.train_end.isoformat()],
            "test": [f.test_start.isoformat(), f.test_end.isoformat()],
            "top_n": plan.top_n, "selection": plan.selection,
            "exclusive_return": plan.exclusive_return,
            "thresholds": {"min_oos_return": th.min_oos_return, "max_dd_mult": th.max_dd_mult,
                           "min_folds": th.min_folds,
                           **({"max_oos_dd": th.max_oos_dd} if th.max_oos_dd is not None else {})}}


def _save_fold(report_dir: str, plan: WF.Plan, f: WF.Fold, genomes: List[WF.GenomeFold],
               identities: Optional[List[Any]] = None) -> None:
    path = _fold_path(report_dir, plan.prefix, f.index)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"key": _fold_key(plan, f, identities), "genomes": [asdict(g) for g in genomes]}, fh,
                  indent=2)


def _load_fold(report_dir: str, plan: WF.Plan, f: WF.Fold,
               identities: Optional[List[Any]] = None) -> Optional[List[WF.GenomeFold]]:
    """The stored report of a completed fold, or None. Every rejection is logged WITH ITS REASON:
    a silently ignored report would look like a resume that recomputed for no visible cause."""
    path = _fold_path(report_dir, plan.prefix, f.index)
    if not os.path.exists(path):
        return None                      # never stored: nothing to reject
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        want = _fold_key(plan, f, identities)
        if doc["key"] != want:
            # ONLY the pass thresholds differ: the stored IS/OOS metrics are those of the same train
            # jobs, window, top-n and selection, and the verdict is a pure function of them. Re-judge
            # under the new rule and store the result; the backtests are not run again.
            same_but_rule = ({k: v for k, v in doc["key"].items() if k != "thresholds"}
                             == {k: v for k, v in want.items() if k != "thresholds"})
            if not same_but_rule:
                logger.warning("[%s] stored report %s rejected: key mismatch (window, top-n, "
                               "selection or the train job(s) changed); re-evaluating fold %d",
                               plan.prefix, path, f.index)
                return None
            genomes = [WF.rejudge(WF.GenomeFold(**g), plan.thresholds, plan.exclusive_return)
                       for g in doc["genomes"]]
            logger.warning("[%s] stored report %s was judged under other pass thresholds (%s); its "
                           "metrics are kept and fold %d is re-judged under %s",
                           plan.prefix, path, doc["key"].get("thresholds"), f.index,
                           want["thresholds"])
            _save_fold(report_dir, plan, f, genomes, identities)
            return genomes
        return [WF.GenomeFold(**g) for g in doc["genomes"]]
    except OSError as e:
        logger.warning("[%s] stored report %s rejected: unreadable (%s); re-evaluating fold %d",
                       plan.prefix, path, e, f.index)
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("[%s] stored report %s rejected: unreadable (%s: %s); re-evaluating fold %d",
                       plan.prefix, path, type(e).__name__, e, f.index)
    return None


# --------------------------------------------------------------------------------- one cell


def _lineage(runner: Any, spec: CellSpec, job: str, planned: Optional[str]) -> Optional[str]:
    """The seed source the job REALLY recorded (the runner reads ``seededFrom`` from its row), else
    the planned one."""
    read = getattr(runner, "seed_lineage", None)
    return read(job) if callable(read) else planned


def _fold_summary(spec: CellSpec, runner: Any, plan: WF.Plan, f: WF.Fold,
                  winner: WF.GenomeFold) -> Dict[str, Any]:
    job = WF.train_job_name(spec.name, f.index)
    prev = plan.previous_fold(f)
    planned = WF.train_job_name(spec.name, prev.index) if prev else None
    return {"fold": f.index, "job": job,
            "train": [f.train_start.isoformat(), f.train_end.isoformat()],
            "test": [f.test_start.isoformat(), f.test_end.isoformat()],
            "generations": plan.fold_extra_args[f.index][1],
            "is": {"fitness": winner.is_fitness, "return": winner.is_return, "car": winner.is_car,
                   "max_dd": winner.is_max_dd, "trades": winner.is_trades},
            "oos": {"return": winner.oos_return, "car": winner.oos_car, "max_dd": winner.oos_max_dd,
                    "trades": winner.oos_trades, "backtest_id": winner.oos_backtest_id},
            "efficiency": winner.efficiency, "passed": winner.passed,
            "fail_reasons": winner.fail_reasons,
            "seeded_from": _lineage(runner, spec, job, planned)}


def cannot_pass(plan: WF.Plan, folds: Sequence[WF.Fold], genomes: Sequence[WF.GenomeFold]
                ) -> Optional[str]:
    """Why the cell can no longer pass whatever the folds still to run do, or None. The verdict
    needs ``min_folds`` passing fold winners and every required year passing, so a failed required
    year, or too few folds left to reach ``min_folds``, decides FAIL already: running the rest
    would cost a train job per fold and change nothing."""
    done = {g.fold: g.passed for g in genomes if g.rank == 1}
    failed_req = sorted(f.test_start.year for f in folds
                        if f.index in done and not done[f.index]
                        and f.test_start.year in plan.require_test_years)
    if failed_req:
        return f"required test year(s) {', '.join(map(str, failed_req))} failed"
    passed = sum(done.values())
    left = sum(1 for f in folds if f.index not in done)
    if passed + left < plan.thresholds.min_folds:
        return (f"{passed} fold(s) passed with {left} left, {plan.thresholds.min_folds} needed")
    return None


def run_cell(spec: CellSpec, runner: Any, report_dir: str,
             log: Callable[[str], None] = print) -> CellResult:
    """Run (or resume) one cell's chain. Raises ``WalkForwardError`` on a refusal or a failed job;
    ``run_cells`` turns that into an ERROR row so the grid carries on.

    Verdicts: PASS (refit run), FAIL (no refit; ``eliminated`` says which folds were skipped because
    the cell could no longer pass), PENDING (the deferred last fold is trained, its test waits for
    the data: the same command with that data tests it and finishes the cell)."""
    plan, folds = make_cell_plan(spec)
    deferred = deferred_fold(folds, spec.data_end, spec.defer_last_test)
    genomes: List[WF.GenomeFold] = []
    ran: List[WF.Fold] = []
    eliminated: Optional[Dict[str, Any]] = None
    for f in folds:
        why = None if spec.run_all_folds else cannot_pass(plan, folds, genomes)
        if why is not None:
            eliminated = {"before_fold": f.index, "reason": why}
            log(f"[{spec.name}] cell can no longer pass ({why}) -- fold {f.index} onward not run")
            break
        if f is deferred:
            WF.train_fold(plan, runner, f, log)
            log(f"[{spec.name}] fold {f.index}: trained; its test {f.test_start}..{f.test_end} "
                f"waits for data past {spec.data_end}")
            break
        name = WF.train_job_name(spec.name, f.index)
        stored = (_load_fold(report_dir, plan, f, _identities(runner, plan, f))
                  if runner.train_completed(name) else None)
        if stored is not None:
            log(f"[{spec.name}] fold {f.index}: completed -- reusing its stored report")
            genomes.extend(stored)
        else:
            got = WF.run_fold(plan, runner, f, log)
            _save_fold(report_dir, plan, f, got, _identities(runner, plan, f))
            genomes.extend(got)
        ran.append(f)
    res = WF.aggregate(spec.name, ran, len(folds), spec.top_n, genomes, spec.thresholds,
                       spec.selection, require_test_years=plan.require_test_years,
                       exclusive_return=plan.exclusive_return)
    cell_dir = os.path.join(report_dir, spec.name)
    WF.write_report(res, cell_dir)
    pending = deferred is not None and eliminated is None
    passed = bool(res.overall_passed) and not pending and eliminated is None
    refit_job: Optional[str] = None
    refit_block: Optional[Dict[str, Any]] = None
    deferred_block: Optional[Dict[str, Any]] = None
    if pending:
        job = WF.train_job_name(spec.name, deferred.index)
        prev = plan.previous_fold(deferred)
        deferred_block = {"fold": deferred.index, "job": job,
                          "train": [deferred.train_start.isoformat(), deferred.train_end.isoformat()],
                          "test": [deferred.test_start.isoformat(), deferred.test_end.isoformat()],
                          "seeded_from": _lineage(runner, spec, job, WF.train_job_name(
                              spec.name, prev.index) if prev else None)}
        log(f"[{spec.name}] cell PENDING -- fold {deferred.index} trained, test deferred")
    elif passed:
        refit_job = refit_job_name(spec.name)
        cmd = refit_command(spec, folds)
        if runner.train_completed(refit_job):
            log(f"[{spec.name}] refit {refit_job} already completed -- skipping")
        else:
            log(f"[{spec.name}] cell PASSED -- refit {refit_job}")
            runner.run_train(cmd, refit_job)
            if not runner.train_completed(refit_job):
                raise WalkForwardError(f"refit job {refit_job!r} did not reach status 'completed'")
        start, end = refit_window(folds)
        src = WF.train_job_name(spec.name, folds[-1].index)
        read = getattr(runner, "seed_lineage", None)
        if callable(read) and read(refit_job) != src:
            raise WalkForwardError(f"refit {refit_job!r} recorded seed source "
                                   f"{read(refit_job)!r}, expected {src!r}")
        refit_block = {"job": refit_job, "train": [start.isoformat(), end.isoformat()],
                       "seeded_from": _lineage(runner, spec, refit_job, src)}
    else:
        log(f"[{spec.name}] cell FAILED -- no refit "
            f"({res.winners_passed}/{len(ran)} folds passed"
            + (f"; required {res.required_failed} failed" if res.required_failed else "")
            + (f"; eliminated before fold {eliminated['before_fold']}" if eliminated else "") + ")")
    winners = {g.fold: g for g in res.genomes if g.rank == 1}
    report = {
        "cell": spec.name, "expert": spec.expert, "strategy": spec.strategy, "band": spec.band,
        "verdict": "PENDING" if pending else "PASS" if passed else "FAIL",
        "refit_job": refit_job, "refit": refit_block,
        "deferred": deferred_block, "eliminated": eliminated,
        "required_failed": list(res.required_failed),
        "thresholds": {**asdict(spec.thresholds), "top_n": spec.top_n,
                       "exclusive_return": spec.exclusive_return,
                       "require_test_years": list(res.require_test_years)},
        "data_end": spec.data_end.isoformat() if spec.data_end else None,
        "folds": [_fold_summary(spec, runner, plan, f, winners[f.index]) for f in ran],
        "winner_stitch": asdict(res.winner_stitch) if res.winner_stitch else None,
    }
    os.makedirs(cell_dir, exist_ok=True)
    with open(os.path.join(cell_dir, "cell.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return CellResult(spec.name, report["verdict"], refit_job, res, report)


# --------------------------------------------------------------------------------- the grid


def _f(v: Any, suffix: str = "%") -> str:
    return "n/a" if v is None else f"{v:.1f}{suffix}"


def render_summary(cells: Sequence[Dict[str, Any]]) -> str:
    out = ["# goal2027 walk-forward summary", "",
           "Per fold: IS CAR / OOS total return / OOS maxDD of the rank-1 genome (P = fold passed).",
           "", "| cell | verdict | fold 1 | fold 2 | fold 3 | refit |", "|---|---|---|---|---|---|"]
    for c in cells:
        cols = []
        for k in range(3):
            fo = c.get("folds", [])
            if k < len(fo):
                x = fo[k]
                cols.append(f"{'P' if x['passed'] else 'F'} {_f(x['is']['car'])} / "
                            f"{_f(x['oos']['return'])} / {_f(x['oos']['max_dd'])}")
            else:
                cols.append("-")
        note = (c.get("error") or c.get("refit_job")
                or (c.get("deferred") and f"fold {c['deferred']['fold']} test deferred")
                or (c.get("eliminated") and f"eliminated before fold {c['eliminated']['before_fold']}")
                or "-")
        out.append(f"| {c['cell']} | {c['verdict']} | " + " | ".join(cols) + f" | {note} |")
    return "\n".join(out) + "\n"


def write_summary(cells: Sequence[Dict[str, Any]], report_dir: str) -> None:
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"cells": list(cells)}, fh, indent=2)
    with open(os.path.join(report_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(render_summary(cells))


def _load_summary_cells(report_dir: str) -> List[Dict[str, Any]]:
    """The cells an earlier driver run wrote to this report dir's summary ([] when none). The grid is
    run as one driver call per band (per-band spread), so every call ADDS to one grid summary."""
    path = os.path.join(report_dir, "summary.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return list(json.load(fh)["cells"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise WalkForwardError(f"grid summary {path} is unreadable ({type(e).__name__}: {e}); "
                               f"fix or move it -- overwriting would drop other bands' cells") from e


def _merge_cell(cells: List[Dict[str, Any]], cell: Dict[str, Any]) -> None:
    """Replace the cell of the same name in place (a rerun), else append it."""
    for k, c in enumerate(cells):
        if c["cell"] == cell["cell"]:
            cells[k] = cell
            return
    cells.append(cell)


def run_cells(specs: Sequence[CellSpec], runner_factory: Callable[[CellSpec], Any],
              report_dir: str, log: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    """Run every cell in order. A cell that raises ANY exception is recorded as ERROR (type and
    message, logged with its traceback) and the grid goes on (a failed train job of one cell must not stop 34 others); the
    summary is rewritten after each cell so a killed driver leaves a current one. The summary
    MERGES with the one already in ``report_dir`` (other bands' cells are kept, a rerun cell is
    replaced in place); the return value is this call's cells only."""
    grid = _load_summary_cells(report_dir)
    cells: List[Dict[str, Any]] = []
    for spec in specs:
        try:
            cells.append(run_cell(spec, runner_factory(spec), report_dir, log).report)
        except Exception as e:  # noqa: BLE001 -- ONE cell's crash (db lock, OSError, bug) must not stop the grid
            logger.error("[%s] cell ERROR: %s: %s", spec.name, type(e).__name__, e, exc_info=True)
            log(f"[{spec.name}] ERROR: {type(e).__name__}: {e}")
            cells.append({"cell": spec.name, "expert": spec.expert, "strategy": spec.strategy,
                          "band": spec.band, "verdict": "ERROR", "error": f"{type(e).__name__}: {e}",
                          "refit_job": None, "folds": []})
        _merge_cell(grid, cells[-1])
        write_summary(grid, report_dir)
    return cells
