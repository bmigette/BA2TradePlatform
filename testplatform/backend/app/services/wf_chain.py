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

Redesign 2026-11-17 (``CellSpec`` defaults = the chain above, files and names unchanged):
``chain_seed=False`` starts every fold and the refit cold on ``first_generations``; ``restarts=K``
trains K searches per fold and per refit (``<job>``, ``<job>-r2``, ...) and tests the one with the
best training fitness (needs ``chain_seed=False``); ``verdict_mode="set-score"`` judges the cell on
every tested genome (``walk_forward.set_score`` > ``min_oos_score``; every fold runs);
``min_train_years`` drops folds whose training is shorter (``short_train_folds``).
"""
from __future__ import annotations

import json
import logging
import math
import os
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.services import walk_forward as WF
from app.services.walk_forward import WalkForwardError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------- folds


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                   # 29 February -> 28 February
        return d.replace(year=d.year + n, day=28)


def calendar_years(start: date, end: date) -> float:
    """Length of the INCLUSIVE window ``start..end`` in years: whole calendar years counted exactly
    (2022-01-01..2023-12-31 is 2.0), the remainder in days / 365.25. 0.0 for an empty window."""
    stop = end + timedelta(days=1)
    if stop <= start:
        return 0.0
    n = stop.year - start.year
    while n > 0 and _add_years(start, n) > stop:
        n -= 1
    return n + (stop - _add_years(start, n)).days / 365.25


def short_train_folds(train_start: date, test_years: Sequence[int], floor: Optional[date],
                      min_train_years: Optional[float]) -> List[Dict[str, Any]]:
    """The test years whose fold would train on fewer than ``min_train_years`` (``--wf-min-train-years``)
    years: the anchored train window runs from the later of ``train_start`` and ``floor`` to the day
    before the test year. None = no minimum (nothing is dropped)."""
    if min_train_years is None:
        return []
    if not isinstance(min_train_years, (int, float)) or not math.isfinite(min_train_years) \
            or min_train_years <= 0:
        raise WalkForwardError(f"--wf-min-train-years must be a number > 0, got {min_train_years!r}")
    start = max(train_start, floor) if floor else train_start
    out = []
    for y in test_years:
        end = date(y - 1, 12, 31)
        yrs = calendar_years(start, end)
        if yrs < min_train_years:
            out.append({"test_year": int(y), "train": [start.isoformat(), end.isoformat()],
                        "train_years": round(yrs, 4),
                        "reason": f"trains {yrs:.2f} year(s) < --wf-min-train-years {min_train_years:g}"})
    return out


def build_cell_folds(train_start: date, test_years: Sequence[int], data_end: Optional[date],
                     floor: Optional[date] = None, defer_last_test: bool = False,
                     min_train_years: Optional[float] = None) -> List[WF.Fold]:
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
    names as the full ones.

    ``min_train_years`` (``--wf-min-train-years``): a test year whose fold would train on fewer years
    is DROPPED (``short_train_folds``; the caller logs and reports it) and the remaining folds are
    numbered 1..n, exactly the folds of the reduced year list. A cell left with no fold is refused."""
    start = max(train_start, floor) if floor else train_start
    short = {s["test_year"] for s in short_train_folds(train_start, test_years, floor, min_train_years)}
    years = [y for y in test_years if y not in short]
    if short and not years:
        raise WalkForwardError(f"--wf-min-train-years {min_train_years:g} drops every test year "
                               f"({', '.join(map(str, sorted(short)))}): the cell has no fold left")
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
    refit: bool = True                  # a PASSING cell trains the refit job (False: verdict only)
    # Redesign 2026-11-17. Defaults = the original chain (names, commands and reports unchanged).
    chain_seed: bool = True             # False: every fold and the refit start cold on first_generations
    restarts: int = 1                   # independent searches per fold / refit; best training fitness wins
    verdict_mode: str = "winner"        # "set-score": every tested genome, scored like the CAR fitness
    min_oos_score: Optional[float] = None   # set-score: the score must be ABOVE this (no default)
    min_train_years: Optional[float] = None  # drop folds training on fewer years (None: keep all)

    def non_default_settings(self) -> Dict[str, Any]:
        """The redesign settings that differ from the original chain, for the reports (only these
        keys are added, so a default cell's files stay as they were)."""
        out: Dict[str, Any] = {}
        if not self.chain_seed:
            out["chain_seed"] = False
        if self.restarts != 1:
            out["restarts"] = self.restarts
        if self.verdict_mode != "winner":
            out["verdict_mode"] = self.verdict_mode
            out["min_oos_score"] = self.min_oos_score
        if self.min_train_years is not None:
            out["min_train_years"] = self.min_train_years
        return out


@dataclass
class CellResult:
    name: str
    verdict: str                    # PASS | FAIL | PENDING (last fold's test deferred)
    refit_job: Optional[str]
    walk_forward: WF.WalkForwardResult
    report: Dict[str, Any]


def _fold_extra_args(spec: CellSpec, folds: Sequence[WF.Fold]) -> Dict[int, List[str]]:
    """Generation budget per fold: ``first_generations`` for a cold start (fold 1, or every fold
    without chain seeding), ``next_generations`` for a seeded one."""
    return {f.index: ["--generations",
                      str(spec.first_generations if f.index == 1 or not spec.chain_seed
                          else spec.next_generations)]
            for f in folds}


def cell_folds(spec: CellSpec) -> List[WF.Fold]:
    return build_cell_folds(spec.train_start, spec.test_years, spec.data_end, spec.floor,
                            spec.defer_last_test, spec.min_train_years)


def make_cell_plan(spec: CellSpec) -> Tuple[WF.Plan, List[WF.Fold]]:
    folds = cell_folds(spec)
    plan = WF.make_plan(
        spec.name, folds, spec.optimize_args, spec.launcher, spec.top_n, spec.thresholds,
        test_parallel=spec.test_parallel, skip_train=False, only_fold=None, embargo_days=0,
        selection=spec.selection, seed_chain=spec.chain_seed,
        fold_extra_args=_fold_extra_args(spec, folds),
        require_test_years=spec.require_test_years, exclusive_return=spec.exclusive_return,
        rebase_test_data=spec.defer_last_test, restarts=spec.restarts,
        verdict_mode=spec.verdict_mode, min_oos_score=spec.min_oos_score)
    return plan, folds


def refit_job_name(prefix: str) -> str:
    return f"{prefix}-refit"


def refit_restart_name(prefix: str, j: int) -> str:
    """Refit restart ``j``: restart 1 is ``<prefix>-refit``, j >= 2 ``<prefix>-refit-r<j>``."""
    if j < 1:
        raise WalkForwardError(f"restart index must be >= 1, got {j}")
    return refit_job_name(prefix) if j == 1 else f"{refit_job_name(prefix)}-r{j}"


def refit_source(spec: CellSpec, folds: Sequence[WF.Fold]) -> Optional[str]:
    """The job the refit is seeded from: the last fold's, or None without chain seeding."""
    return WF.train_job_name(spec.name, folds[-1].index) if spec.chain_seed else None


def refit_command(spec: CellSpec, folds: Sequence[WF.Fold], restart: int = 1) -> List[str]:
    """The refit's optimize command: whole window, ``next_generations``, seeded from the last fold
    (without chain seeding: unseeded on ``first_generations``, like every fold). Restart j >= 2 is
    ``<prefix>-refit-r<j>`` with its own GA ``--seed``. NO LOOK-AHEAD, asserted: the seed's train
    window ends before the refit window ends (the refit is allowed to see everything, there is no
    test year left to protect)."""
    start, end = refit_window(folds)
    last = folds[-1]
    if last.train_end >= end:
        raise WalkForwardError(f"refit seed {last.train_end} is not before the refit end {end}")
    head = [spec.launcher] if isinstance(spec.launcher, str) else list(spec.launcher)
    name = refit_restart_name(spec.name, restart)
    src = refit_source(spec, folds)
    gens = spec.next_generations if spec.chain_seed else spec.first_generations
    return [*head, "optimize", *spec.optimize_args, "--generations", str(gens),
            *(["--seed", str(WF.restart_seed(name))] if restart > 1 else []),
            *(["--chain-seed-from", src] if src is not None else []),
            "--start", start.isoformat(), "--end", end.isoformat(), "--name", name]


# --------------------------------------------------------------------------------- fold store


def _fold_path(report_dir: str, name: str, k: int) -> str:
    return os.path.join(report_dir, name, f"fold{k}.json")


def _identities(runner: Any, plan: WF.Plan, f: WF.Fold) -> Optional[List[Any]]:
    """Identity (optimization id + completion time) of this fold's train job AND every earlier one:
    fold k is seeded from fold k-1, so retraining any of them makes fold k's report stale. Without
    chain seeding, the identities of this fold's restarts only (no other fold enters it). None for
    a runner that cannot say (then the key carries no identity)."""
    read = getattr(runner, "train_identity", None)
    if not callable(read):
        return None
    if not plan.seed_chain:
        return [read(n) for n in plan.restart_names(f)]
    return [read(n) for k in plan.folds if k.index <= f.index for n in plan.restart_names(k)]


def _fold_key(plan: WF.Plan, f: WF.Fold, identities: Optional[List[Any]] = None) -> Dict[str, Any]:
    """What a stored fold report must agree with to be trusted (anything else re-evaluates). The
    chain-seed and restart settings enter only when they differ from the original chain, so a
    default fold's stored key is unchanged."""
    th = plan.thresholds
    return {"train_jobs": identities,"train": [f.train_start.isoformat(), f.train_end.isoformat()],
            "test": [f.test_start.isoformat(), f.test_end.isoformat()],
            "top_n": plan.top_n, "selection": plan.selection,
            "exclusive_return": plan.exclusive_return,
            "thresholds": {"min_oos_return": th.min_oos_return, "max_dd_mult": th.max_dd_mult,
                           "min_folds": th.min_folds,
                           **({"max_oos_dd": th.max_oos_dd} if th.max_oos_dd is not None else {})},
            **({"chain_seed": False} if not plan.seed_chain else {}),
            **({"restarts": plan.restarts} if plan.restarts != 1 else {})}


def _save_fold(report_dir: str, plan: WF.Plan, f: WF.Fold, genomes: List[WF.GenomeFold],
               identities: Optional[List[Any]] = None,
               searches: Optional[List[Dict[str, Any]]] = None) -> None:
    doc: Dict[str, Any] = {"key": _fold_key(plan, f, identities), "genomes": [asdict(g) for g in genomes]}
    if searches is not None:
        doc["restarts"] = searches       # a record only: the key decides whether the file is trusted
    WF.atomic_write_text(_fold_path(report_dir, plan.prefix, f.index), json.dumps(doc, indent=2))


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
            _save_fold(report_dir, plan, f, genomes, identities, doc.get("restarts"))
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


def _planned_seed(plan: WF.Plan, f: WF.Fold) -> Optional[str]:
    prev = plan.previous_fold(f) if plan.seed_chain else None
    return WF.train_job_name(plan.prefix, prev.index) if prev else None


def _fold_summary(spec: CellSpec, runner: Any, plan: WF.Plan, f: WF.Fold,
                  winner: WF.GenomeFold, searches: Optional[List[Dict[str, Any]]] = None,
                  set_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``searches`` (restarts > 1): every restart's record; ``job`` is then the winning search's.
    ``set_stats`` (set-score verdict): the fold's statistics over every tested genome."""
    job = (next(s["job"] for s in searches if s["won"]) if searches
           else WF.train_job_name(spec.name, f.index))
    planned = _planned_seed(plan, f)
    extra: Dict[str, Any] = {}
    if searches:
        extra["restarts"] = searches
    if set_stats is not None:
        extra["set"] = set_stats
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
            "seeded_from": _lineage(runner, spec, job, planned), **extra}


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


def lock_path(report_dir: str, name: str) -> str:
    return os.path.join(report_dir, name, ".lock")


def run_cell(spec: CellSpec, runner: Any, report_dir: str,
             log: Callable[[str], None] = print) -> CellResult:
    """Run (or resume) one cell's chain. Raises ``WalkForwardError`` on a refusal or a failed job;
    ``run_cells`` turns that into an ERROR row so the grid carries on.

    Verdicts: PASS (refit run), FAIL (no refit; ``eliminated`` says which folds were skipped because
    the cell could no longer pass), PENDING (the deferred last fold is trained, its test waits for
    the data: the same command with that data tests it and finishes the cell).

    The whole chain runs under the runner's driver lock for the cell (``<report_dir>/<cell>/.lock``,
    when the runner has one): a second driver on the same cell is refused, a killed driver's lock
    is taken over.

    DB row: when the runner offers ``wf_store(spec, report_dir)`` (the real runner does), the chain
    ALSO writes its ``walk_forward_runs`` row as it advances (app.services.wf_store), after the
    files each time. The files and their resume semantics are unchanged. One row per (cell, report
    directory). A failed DB write of a grid / CLI chain is retried, then logged once as an ERROR and
    the chain carries on without its row (the files are the source of truth; the importer repairs
    the row); for a UI run it raises. An exception of the chain is recorded on the row, then
    re-raised unchanged (a failure to record it is logged, never raised in its place)."""
    lock = getattr(runner, "exclusive", None)
    make_store = getattr(runner, "wf_store", None)
    store = make_store(spec, report_dir) if callable(make_store) else None
    with (lock(spec.name, lock_path(report_dir, spec.name)) if callable(lock) else nullcontext()):
        if store is None:
            return _run_cell(spec, runner, report_dir, log)
        try:
            return _run_cell(spec, runner, report_dir, log, store)
        except BaseException as e:
            # Recording the failure must never mask or replace it: a KeyboardInterrupt stays a
            # KeyboardInterrupt (run_cells must not carry on with the grid after Ctrl-C).
            try:
                store.fail(e)
            except Exception as fe:  # noqa: BLE001 -- logged; the chain's own exception is re-raised
                logger.error("[%s] could not record the chain's %s on its walk_forward_runs row "
                             "(%s: %s)", spec.name, type(e).__name__, type(fe).__name__, fe)
            raise


class _NoStore:
    """Stands in for the DB row writer when the runner has none (test fakes, dry runs)."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _run_cell(spec: CellSpec, runner: Any, report_dir: str,
              log: Callable[[str], None], store: Any = None) -> CellResult:
    store = store if store is not None else _NoStore()
    plan, folds = make_cell_plan(spec)
    skipped = short_train_folds(spec.train_start, spec.test_years, spec.floor, spec.min_train_years)
    for s in skipped:
        msg = (f"[{spec.name}] test year {s['test_year']} dropped: its fold would train "
               f"{s['train'][0]}..{s['train'][1]} ({s['train_years']:.2f} years), less than "
               f"--wf-min-train-years {spec.min_train_years:g}")
        logger.warning(msg)
        log(msg)
    deferred = deferred_fold(folds, spec.data_end, spec.defer_last_test)
    store.begin(spec, folds, deferred)
    genomes: List[WF.GenomeFold] = []
    ran: List[WF.Fold] = []
    searches: Dict[int, Optional[List[Dict[str, Any]]]] = {}
    eliminated: Optional[Dict[str, Any]] = None
    set_mode = spec.verdict_mode == "set-score"
    for f in folds:
        # The set-score verdict needs every fold: no early elimination.
        why = None if (spec.run_all_folds or set_mode) else cannot_pass(plan, folds, genomes)
        if why is not None:
            eliminated = {"before_fold": f.index, "reason": why}
            log(f"[{spec.name}] cell can no longer pass ({why}) -- fold {f.index} onward not run")
            break
        store.fold_started(f)
        if f is deferred:
            WF.train_fold(plan, runner, f, log)
            searches[f.index] = WF.search_records(plan, runner, f) if plan.restarts > 1 else None
            log(f"[{spec.name}] fold {f.index}: trained; its test {f.test_start}..{f.test_end} "
                f"waits for data past {spec.data_end}")
            store.fold_deferred(f)
            break
        completed = all(runner.train_completed(n) for n in plan.restart_names(f))
        ids = _identities(runner, plan, f) if completed else None
        stored = _load_fold(report_dir, plan, f, ids) if completed else None
        recs = WF.search_records(plan, runner, f) if completed and plan.restarts > 1 else None
        if stored is not None and recs is not None and not _same_winner(report_dir, plan, f, recs):
            stored = None
        if stored is not None:
            log(f"[{spec.name}] fold {f.index}: completed -- reusing its stored report")
            genomes.extend(stored)
            store.fold_done(f, stored, _winner_identity(runner, ids, recs), reused=True)
        else:
            got = WF.run_fold(plan, runner, f, log)
            ids = _identities(runner, plan, f)
            recs = WF.search_records(plan, runner, f) if plan.restarts > 1 else None
            _save_fold(report_dir, plan, f, got, ids, recs)
            genomes.extend(got)
            store.fold_done(f, got, _winner_identity(runner, ids, recs))
        searches[f.index] = recs
        ran.append(f)
    res = WF.aggregate(spec.name, ran, len(folds), spec.top_n, genomes, spec.thresholds,
                       spec.selection, require_test_years=plan.require_test_years,
                       exclusive_return=plan.exclusive_return, verdict_mode=plan.verdict_mode,
                       min_oos_score=plan.min_oos_score)
    cell_dir = os.path.join(report_dir, spec.name)
    WF.write_report(res, cell_dir)
    pending = deferred is not None and eliminated is None
    passed = bool(res.overall_passed) and not pending and eliminated is None
    refit_job: Optional[str] = None
    refit_block: Optional[Dict[str, Any]] = None
    deferred_block: Optional[Dict[str, Any]] = None
    if pending:
        recs = searches.get(deferred.index)
        job = (next(r["job"] for r in recs if r["won"]) if recs
               else WF.train_job_name(spec.name, deferred.index))
        deferred_block = {"fold": deferred.index, "job": job,
                          "train": [deferred.train_start.isoformat(), deferred.train_end.isoformat()],
                          "test": [deferred.test_start.isoformat(), deferred.test_end.isoformat()],
                          "seeded_from": _lineage(runner, spec, job, _planned_seed(plan, deferred)),
                          **({"restarts": recs} if recs else {})}
        log(f"[{spec.name}] cell PENDING -- fold {deferred.index} trained, test deferred")
    elif passed and not spec.refit:
        log(f"[{spec.name}] cell PASSED -- refit not requested (refit off)")
    elif passed:
        refit_job, refit_block = _run_refit(spec, plan, runner, folds, store, log)
    else:
        log(f"[{spec.name}] cell FAILED -- no refit ("
            + (f"set score {res.set_score['score']:.2f} not above {spec.min_oos_score:g}; "
               if set_mode and res.set_score else "")
            + f"{res.winners_passed}/{len(ran)} folds passed"
            + (f"; required {res.required_failed} failed" if res.required_failed else "")
            + (f"; eliminated before fold {eliminated['before_fold']}" if eliminated else "") + ")")
    winners = {g.fold: g for g in res.genomes if g.rank == 1}
    set_folds = {s["fold"]: s for s in (res.set_score or {}).get("folds", [])}
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
        "folds": [_fold_summary(spec, runner, plan, f, winners[f.index], searches.get(f.index),
                                set_folds.get(f.index)) for f in ran],
        "winner_stitch": asdict(res.winner_stitch) if res.winner_stitch else None,
        # Redesign settings and results: present only when not the original chain's.
        **spec.non_default_settings(),
        **({"skipped_folds": skipped} if spec.min_train_years is not None else {}),
        **({"set_score": res.set_score} if set_mode else {}),
    }
    WF.atomic_write_text(os.path.join(cell_dir, "cell.json"), json.dumps(report, indent=2))
    store.finish(report, res)
    return CellResult(spec.name, report["verdict"], refit_job, res, report)


def _winner_identity(runner: Any, ids: Optional[List[Any]],
                     recs: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The identity of the job whose genomes were tested: the fold's train job (last of ``ids``), or
    with restarts the winning search's."""
    if not recs:
        return ids[-1] if ids else None
    read = getattr(runner, "train_identity", None)
    return read(next(r["job"] for r in recs if r["won"])) if callable(read) else None


def _same_winner(report_dir: str, plan: WF.Plan, f: WF.Fold, recs: List[Dict[str, Any]]) -> bool:
    """A stored restart fold is reused only if it tested the search that wins now (a runner without
    train identities cannot otherwise tell that a restart was retrained)."""
    with open(_fold_path(report_dir, plan.prefix, f.index), encoding="utf-8") as fh:
        stored = json.load(fh).get("restarts") or []
    was = [r["job"] for r in stored if r.get("won")]
    now = [r["job"] for r in recs if r["won"]]
    if was != now:
        logger.warning("[%s] stored report of fold %d tested %s, but the winning search is now %s; "
                       "re-evaluating fold %d", plan.prefix, f.index, was or "no recorded search",
                       now, f.index)
        return False
    return True


def _run_refit(spec: CellSpec, plan: WF.Plan, runner: Any, folds: Sequence[WF.Fold], store: Any,
               log: Callable[[str], None]) -> Tuple[str, Dict[str, Any]]:
    """Train the refit (every restart; skipped when completed) and check each recorded seed against
    the plan (``refit_source``: the last fold, or None without chain seeding). With restarts the
    refit job is the one with the best training fitness."""
    names = [refit_restart_name(spec.name, j) for j in range(1, spec.restarts + 1)]
    src = refit_source(spec, folds)
    read = getattr(runner, "seed_lineage", None)
    for j, job in enumerate(names, start=1):
        cmd = refit_command(spec, folds, j)
        if runner.train_completed(job):
            log(f"[{spec.name}] refit {job} already completed -- skipping")
        else:
            log(f"[{spec.name}] cell PASSED -- refit {job}")
            store.refit_started(job)
            runner.run_train(cmd, job)
            if not runner.train_completed(job):
                raise WalkForwardError(f"refit job {job!r} did not reach status 'completed'")
        if callable(read) and read(job) != src:
            raise WalkForwardError(f"refit {job!r} recorded seed source {read(job)!r}, expected {src!r}")
    start, end = refit_window(folds)
    recs: Optional[List[Dict[str, Any]]] = None
    refit_job = names[0]
    if spec.restarts > 1:
        fit = WF._best_fitness_reader(plan, runner)
        ident = getattr(runner, "train_identity", None)
        fits = [fit(n) for n in names]
        won = WF.choose_search(fits)
        gens = spec.next_generations if spec.chain_seed else spec.first_generations
        recs = [{"restart": j, "job": n,
                 "optimization_id": (ident(n) or {}).get("id") if callable(ident) else None,
                 "best_fitness": float(x), "generations": str(gens), "won": j == won}
                for j, (n, x) in enumerate(zip(names, fits), start=1)]
        refit_job = names[won - 1]
        log(f"[{spec.name}] refit {refit_job} wins on training fitness")
    block = {"job": refit_job, "train": [start.isoformat(), end.isoformat()],
             "seeded_from": _lineage(runner, spec, refit_job, src),
             **({"restarts": recs} if recs else {})}
    return refit_job, block


# --------------------------------------------------------------------------------- the grid


def _f(v: Any, suffix: str = "%") -> str:
    return "n/a" if v is None else f"{v:.1f}{suffix}"


def render_summary(cells: Sequence[Dict[str, Any]]) -> str:
    """One row per cell, one column per fold (at least 3: a grid of 3-fold cells renders as it always
    did). Cells judged by the set score get a second table with the score and its factors."""
    n = max([3] + [len(c.get("folds", [])) for c in cells])
    out = ["# goal2027 walk-forward summary", "",
           "Per fold: IS CAR / OOS total return / OOS maxDD of the rank-1 genome (P = fold passed).",
           "", "| cell | verdict | " + " | ".join(f"fold {k}" for k in range(1, n + 1)) + " | refit |",
           "|---|---|" + "---|" * n + "---|"]
    for c in cells:
        cols = []
        for k in range(n):
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
    scored = [c for c in cells if c.get("set_score")]
    if scored:
        out += ["", "## Set score (every tested genome per fold)", "",
                "Score = mean of the folds' median OOS returns (annualised for a partial year) x "
                "dd_guard x consistency, as the consistent_annual_return fitness; PASS = above the "
                "minimum. Per fold: median OOS return / share of genomes > 0.", "",
                "| cell | verdict | set score | min | base | dd_guard | consistency | folds |",
                "|---|---|---:|---:|---:|---:|---:|---|"]
        for c in scored:
            s = c["set_score"]
            per = ", ".join(f"{_f(x['oos_return_median'])} / {x['share_positive'] * 100:.0f}%"
                            for x in s["folds"])
            out.append(f"| {c['cell']} | {c['verdict']} | {s['score']:.2f} | {s['min_oos_score']:g} | "
                       f"{s['base']:.2f} | {s['dd_guard']:.3f} | {s['consistency']:.3f} | {per} |")
    return "\n".join(out) + "\n"


def write_summary(cells: Sequence[Dict[str, Any]], report_dir: str) -> None:
    WF.atomic_write_text(os.path.join(report_dir, "summary.json"),
                         json.dumps({"cells": list(cells)}, indent=2))
    WF.atomic_write_text(os.path.join(report_dir, "summary.md"), render_summary(cells))


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
