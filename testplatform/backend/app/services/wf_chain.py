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
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.services import walk_forward as WF
from app.services.walk_forward import WalkForwardError


# --------------------------------------------------------------------------------- folds


def build_cell_folds(train_start: date, test_years: Sequence[int], data_end: Optional[date],
                     floor: Optional[date] = None) -> List[WF.Fold]:
    """The cell's anchored folds. ``floor`` is the expert's data floor (FMPRating 2022-01-01): the
    train start of EVERY fold and of the refit is the later of ``train_start`` and ``floor``.
    ``data_end`` (``--wf-data-end``) shortens the LAST test window; it must leave that window
    non-empty and fall inside the last test year or later (a data end before the last year would
    silently drop whole folds, so it is refused instead)."""
    start = max(train_start, floor) if floor else train_start
    years = list(test_years)
    test_end = None
    if data_end is not None and years:
        last_start, last_end = date(years[-1], 1, 1), date(years[-1], 12, 31)
        if data_end <= last_start:
            raise WalkForwardError(
                f"--wf-data-end {data_end}: the last test window {last_start}..{last_end} would be "
                f"empty (data must reach beyond {last_start})")
        if data_end < last_end:
            test_end = data_end
    folds = WF.build_year_folds(start, years, test_end)
    WF.validate_folds(folds)
    return folds


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


@dataclass
class CellResult:
    name: str
    verdict: str                    # PASS | FAIL
    refit_job: Optional[str]
    walk_forward: WF.WalkForwardResult
    report: Dict[str, Any]


def _fold_extra_args(spec: CellSpec, folds: Sequence[WF.Fold]) -> Dict[int, List[str]]:
    return {f.index: ["--generations",
                      str(spec.first_generations if f.index == 1 else spec.next_generations)]
            for f in folds}


def make_cell_plan(spec: CellSpec) -> Tuple[WF.Plan, List[WF.Fold]]:
    folds = build_cell_folds(spec.train_start, spec.test_years, spec.data_end, spec.floor)
    plan = WF.make_plan(
        spec.name, folds, spec.optimize_args, spec.launcher, spec.top_n, spec.thresholds,
        test_parallel=spec.test_parallel, skip_train=False, only_fold=None, embargo_days=0,
        selection=spec.selection, seed_chain=True, fold_extra_args=_fold_extra_args(spec, folds),
        require_test_years=spec.require_test_years, exclusive_return=spec.exclusive_return)
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


def _fold_key(plan: WF.Plan, f: WF.Fold) -> Dict[str, Any]:
    """What a stored fold report must agree with to be trusted (anything else re-evaluates)."""
    th = plan.thresholds
    return {"train": [f.train_start.isoformat(), f.train_end.isoformat()],
            "test": [f.test_start.isoformat(), f.test_end.isoformat()],
            "top_n": plan.top_n, "selection": plan.selection,
            "exclusive_return": plan.exclusive_return,
            "thresholds": {"min_oos_return": th.min_oos_return, "max_dd_mult": th.max_dd_mult,
                           "min_folds": th.min_folds}}


def _save_fold(report_dir: str, plan: WF.Plan, f: WF.Fold, genomes: List[WF.GenomeFold]) -> None:
    path = _fold_path(report_dir, plan.prefix, f.index)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"key": _fold_key(plan, f), "genomes": [asdict(g) for g in genomes]}, fh,
                  indent=2)


def _load_fold(report_dir: str, plan: WF.Plan, f: WF.Fold) -> Optional[List[WF.GenomeFold]]:
    path = _fold_path(report_dir, plan.prefix, f.index)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc["key"] != _fold_key(plan, f):
            return None
        return [WF.GenomeFold(**g) for g in doc["genomes"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None                      # unreadable store: re-evaluate, never guess


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


def run_cell(spec: CellSpec, runner: Any, report_dir: str,
             log: Callable[[str], None] = print) -> CellResult:
    """Run (or resume) one cell's chain. Raises ``WalkForwardError`` on a refusal or a failed job;
    ``run_cells`` turns that into an ERROR row so the grid carries on."""
    plan, folds = make_cell_plan(spec)
    genomes: List[WF.GenomeFold] = []
    for f in folds:
        name = WF.train_job_name(spec.name, f.index)
        stored = _load_fold(report_dir, plan, f) if runner.train_completed(name) else None
        if stored is not None:
            log(f"[{spec.name}] fold {f.index}: completed -- reusing its stored report")
            genomes.extend(stored)
            continue
        got = WF.run_fold(plan, runner, f, log)
        _save_fold(report_dir, plan, f, got)
        genomes.extend(got)
    res = WF.aggregate(spec.name, folds, len(folds), spec.top_n, genomes, spec.thresholds,
                       spec.selection, require_test_years=plan.require_test_years,
                       exclusive_return=plan.exclusive_return)
    cell_dir = os.path.join(report_dir, spec.name)
    WF.write_report(res, cell_dir)
    passed = bool(res.overall_passed)
    refit_job: Optional[str] = None
    refit_block: Optional[Dict[str, Any]] = None
    if passed:
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
            f"({res.winners_passed}/{len(folds)} folds passed"
            + (f"; required {res.required_failed} failed" if res.required_failed else "") + ")")
    winners = {g.fold: g for g in res.genomes if g.rank == 1}
    report = {
        "cell": spec.name, "expert": spec.expert, "strategy": spec.strategy, "band": spec.band,
        "verdict": "PASS" if passed else "FAIL", "refit_job": refit_job, "refit": refit_block,
        "required_failed": list(res.required_failed),
        "thresholds": {**asdict(spec.thresholds), "top_n": spec.top_n,
                       "exclusive_return": spec.exclusive_return,
                       "require_test_years": list(res.require_test_years)},
        "data_end": spec.data_end.isoformat() if spec.data_end else None,
        "folds": [_fold_summary(spec, runner, plan, f, winners[f.index]) for f in folds],
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
        note = c.get("error") or c.get("refit_job") or "-"
        out.append(f"| {c['cell']} | {c['verdict']} | " + " | ".join(cols) + f" | {note} |")
    return "\n".join(out) + "\n"


def write_summary(cells: Sequence[Dict[str, Any]], report_dir: str) -> None:
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"cells": list(cells)}, fh, indent=2)
    with open(os.path.join(report_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(render_summary(cells))


def run_cells(specs: Sequence[CellSpec], runner_factory: Callable[[CellSpec], Any],
              report_dir: str, log: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    """Run every cell in order. A cell that raises ``WalkForwardError`` is recorded as ERROR with the
    message and the grid goes on (a failed train job of one cell must not stop 34 others); the
    summary is rewritten after each cell so a killed driver leaves a current one."""
    cells: List[Dict[str, Any]] = []
    for spec in specs:
        try:
            cells.append(run_cell(spec, runner_factory(spec), report_dir, log).report)
        except WalkForwardError as e:
            log(f"[{spec.name}] ERROR: {e}")
            cells.append({"cell": spec.name, "expert": spec.expert, "strategy": spec.strategy,
                          "band": spec.band, "verdict": "ERROR", "error": str(e),
                          "refit_job": None, "folds": []})
        write_summary(cells, report_dir)
    return cells
