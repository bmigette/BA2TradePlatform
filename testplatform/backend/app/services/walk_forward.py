"""Walk-forward validation of GA optimization jobs: the PURE logic.

Everything here is side-effect free apart from ``run_walk_forward`` (which drives a ``Runner``) and
``write_report``. Subprocess / DB / re-run work lives behind the ``Runner`` protocol in
tools/run_walk_forward.py, so the fold maths, the validation, the aggregation and the verdict are
unit-testable with a fake runner and nothing is optimized. Design: docs/plans/2026-10-02-walk-forward-engine-design.md.

FOLDS. Anchored and expanding: every fold trains from the same start and ends the day before its
test year. ``--embargo-days N`` leaves N calendar days between the train end and the test start;
for the year-based folds the TRAIN END moves back (the test year stays whole), for explicit folds
the gap is validated. Test windows never overlap their own train window nor each other.

NO HIDDEN DEFAULTS. The pass thresholds are a required ``Thresholds`` triple; a missing metric
raises (``MissingMetric``); a genome whose out-of-sample run made zero trades FAILS.

CONVENTIONS. Returns and drawdowns are percent. Drawdown sign is not trusted (``abs`` is used).
CAR is ``distinct_topn.annualise`` of the total return over ``(end - start).days / 365.25`` for
BOTH the in-sample and the out-of-sample window, so the two are the same formula.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from app.services.distinct_topn import annualise

OOS_LABELS = ("WalkForward", "OOS")     # + ``wf-fold-<k>`` per fold
# The window and name are owned by the engine. The others import state from ANOTHER job (a
# look-ahead door: that job may have seen the fold's test window) or break the one-name-one-row
# resume rule; seeding comes only through ``initial_population_for_fold``.
FORBIDDEN_OPTIMIZE_FLAGS = {
    "--start": "the engine sets the fold's train window",
    "--end": "the engine sets the fold's train window",
    "--name": "the engine names each fold's job",
    "--warm-start-from": "it imports another job's population, which may have seen the test "
                         "window (look-ahead); seeding goes through initial_population_for_fold",
    "--rerun": "it creates a second optimization under the same name and breaks resume",
    "--submit": "it queues the job and returns before it is completed, so the fold cannot "
                "continue",
}


class WalkForwardError(Exception):
    """A refusal: the CLI prints the message and exits non-zero. Never swallowed."""


class MissingMetric(WalkForwardError):
    """A metric needed for the report is absent (None / non-finite). Never defaulted."""


# --------------------------------------------------------------------------------- folds


@dataclass(frozen=True)
class Fold:
    index: int            # 1-based position in the plan (stable under --only-fold)
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    @property
    def train_years(self) -> float:
        return window_years(self.train_start, self.train_end)

    @property
    def test_years(self) -> float:
        return window_years(self.test_start, self.test_end)


def window_years(start: date, end: date) -> float:
    days = (end - start).days
    if days <= 0:
        raise WalkForwardError(f"window {start}..{end} is empty")
    return days / 365.25


def _iso(text: str, what: str) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as e:
        raise WalkForwardError(f"{what} {text!r} is not an ISO date (YYYY-MM-DD)") from e


def build_year_folds(train_start: date, test_years: Sequence[int],
                     test_end: Optional[date] = None, embargo_days: int = 0) -> List[Fold]:
    """Anchored, expanding folds: one per test year. ``test_end`` truncates the LAST year only
    (a partial year, e.g. 2026-06-30). With an embargo the train end is ``test_start - 1 -
    embargo_days`` (the test year stays whole)."""
    if not test_years:
        raise WalkForwardError("no test years given")
    if embargo_days < 0:
        raise WalkForwardError(f"--embargo-days must be >= 0, got {embargo_days}")
    years = list(test_years)
    if years != sorted(set(years)):
        raise WalkForwardError(f"test years must be strictly ascending, got {years}")
    folds = []
    for i, y in enumerate(years, start=1):
        t_start, t_end = date(y, 1, 1), date(y, 12, 31)
        if i == len(years) and test_end is not None:
            if test_end.year != y:
                raise WalkForwardError(
                    f"--test-end {test_end} is not inside the last test year {y}")
            t_end = test_end
        folds.append(Fold(i, train_start, t_start - timedelta(days=1 + embargo_days),
                          t_start, t_end))
    if test_end is not None and test_end.year != years[-1]:
        raise WalkForwardError(f"--test-end {test_end} is not inside the last test year {years[-1]}")
    return folds


def parse_explicit_fold(text: str, index: int) -> Fold:
    parts = text.split(":")
    if len(parts) != 4:
        raise WalkForwardError(
            f"--fold {text!r}: expected TRAIN_START:TRAIN_END:TEST_START:TEST_END")
    a, b, c, d = (_iso(p, "--fold date") for p in parts)
    return Fold(index, a, b, c, d)


def validate_folds(folds: Sequence[Fold], embargo_days: int = 0) -> None:
    """Refuse loudly: empty windows, a test that does not start strictly after its train ends
    (plus the embargo), folds out of order or with overlapping test windows."""
    if not folds:
        raise WalkForwardError("no folds")
    prev: Optional[Fold] = None
    for f in folds:
        if f.train_start >= f.train_end:
            raise WalkForwardError(f"fold {f.index}: train {f.train_start}..{f.train_end} is empty")
        if f.test_start > f.test_end:
            raise WalkForwardError(f"fold {f.index}: test {f.test_start}..{f.test_end} is empty")
        if f.test_start <= f.train_end:
            raise WalkForwardError(
                f"fold {f.index}: test start {f.test_start} is not after train end {f.train_end} "
                f"(the windows overlap)")
        gap = (f.test_start - f.train_end).days - 1
        if gap < embargo_days:
            raise WalkForwardError(
                f"fold {f.index}: only {gap} day(s) between train end {f.train_end} and test "
                f"start {f.test_start}, embargo is {embargo_days}")
        if prev is not None:
            if f.test_start <= prev.test_end:
                raise WalkForwardError(
                    f"fold {f.index}: test window {f.test_start}..{f.test_end} starts at or "
                    f"before fold {prev.index}'s test end {prev.test_end} (folds must be ordered "
                    f"with non-overlapping test windows)")
            if f.train_end < prev.train_end:
                raise WalkForwardError(
                    f"fold {f.index}: train end {f.train_end} is before fold {prev.index}'s "
                    f"{prev.train_end} (folds must be ordered)")
        prev = f


def check_user_optimize_args(args: Sequence[str]) -> None:
    """The wrapped optimize args must not carry the window, the job name, or a flag that imports
    state from another job / queues the job: see ``FORBIDDEN_OPTIMIZE_FLAGS``."""
    for a in args:
        for flag, why in FORBIDDEN_OPTIMIZE_FLAGS.items():
            if a == flag or a.startswith(flag + "="):
                raise WalkForwardError(
                    f"optimize args must not contain {flag} ({why}); got {a!r}")


def optimize_arg_value(args: Sequence[str], flag: str) -> Optional[str]:
    """The value of ``flag`` in an optimize arg list (``--flag V`` or ``--flag=V``), or None."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


# --------------------------------------------------------------------------------- names


def train_job_name(prefix: str, k: int) -> str:
    return f"{prefix}-wf{k}"


def oos_name_prefix(k: int) -> str:
    """``_persist_top_backtests`` appends ``<rank>-<optimization name>``: -> WF<k>-OOS-R<rank>-<job>."""
    return f"WF{k}-OOS-R"


def oos_backtest_name(k: int, rank: int, train_name: str) -> str:
    return f"{oos_name_prefix(k)}{rank}-{train_name}"


def oos_labels(k: int) -> List[str]:
    return [OOS_LABELS[0], f"wf-fold-{k}", OOS_LABELS[1]]


def initial_population_for_fold(fold: Fold) -> Optional[Any]:
    """EXTENSION POINT (seeding, NOT implemented). Returns None today = every fold's GA starts
    from the optimize job's own initial population.

    The seed population will be defined later. When it is, it plugs in HERE: the returned value
    is rendered into the fold's ``ba2-test optimize`` command by ``train_command`` (the optimize
    CLI's seeding flag, whichever it ends up being) before the ``--start/--end/--name`` tail.
    ``train_command`` refuses (NotImplementedError) a non-None return until that rendering exists,
    so a half-wired seed can never be silently ignored."""
    return None


def train_command(launcher: Any, user_args: Sequence[str], prefix: str, fold: Fold) -> List[str]:
    """``launcher`` is the ba2-test executable, or an argv prefix list (``[python, script.py]``)."""
    seed = initial_population_for_fold(fold)
    if seed is not None:
        raise NotImplementedError(
            "initial_population_for_fold returned a seed but train_command does not render it "
            "into the optimize command yet")
    head = [launcher] if isinstance(launcher, str) else list(launcher)
    return [*head, "optimize", *user_args, "--start", fold.train_start.isoformat(),
            "--end", fold.train_end.isoformat(), "--name", train_job_name(prefix, fold.index)]


# --------------------------------------------------------------------------------- records


@dataclass(frozen=True)
class Thresholds:
    """All three REQUIRED (no defaults): a verdict is only as honest as its thresholds."""
    min_oos_return: float      # percent, per fold
    max_dd_mult: float         # OOS |max DD| <= mult * IS |max DD|
    min_folds: int             # folds whose rank-1 winner must pass

    def validate(self, n_folds: int) -> None:
        if self.max_dd_mult <= 0:
            raise WalkForwardError(f"--pass-max-dd-mult must be > 0, got {self.max_dd_mult}")
        if not 1 <= self.min_folds <= n_folds:
            raise WalkForwardError(
                f"--pass-min-folds {self.min_folds} must be between 1 and the number of folds "
                f"({n_folds})")


@dataclass(frozen=True)
class IsPick:
    """One selected genome with its in-sample (GA record) metrics."""
    rank: int
    fitness: float
    total_return: float
    car: float
    max_drawdown: float
    trades: int
    params: Dict[str, Any] = field(compare=False, repr=False, default_factory=dict)
    key: Optional[str] = None


@dataclass(frozen=True)
class OosMetrics:
    backtest_id: Optional[int]
    total_return: float
    max_drawdown: float
    trades: int
    reused: bool = False


@dataclass
class GenomeFold:
    fold: int
    rank: int
    is_fitness: float
    is_return: float
    is_car: float
    is_max_dd: float
    is_trades: int
    oos_return: float
    oos_car: float
    oos_max_dd: float
    oos_trades: int
    oos_backtest_id: Optional[int]
    efficiency: Optional[float]       # oos_car / is_car; None when is_car <= 0
    passed: bool
    fail_reasons: List[str]


def _need(v, what: str) -> float:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise MissingMetric(f"missing metric: {what} = {v!r}")
    return float(v)


def efficiency(oos_car: float, is_car: float) -> Optional[float]:
    """Walk-forward efficiency = annualised OOS return / annualised IS return. Undefined (None)
    when the IS return is not positive: a ratio against a loss or zero says nothing."""
    return oos_car / is_car if is_car > 0 else None


def evaluate_genome(fold: Fold, pick: IsPick, oos: OosMetrics, th: Thresholds) -> GenomeFold:
    where = f"fold {fold.index} rank {pick.rank}"
    is_ret = _need(pick.total_return, f"{where} IS total_return")
    is_car = _need(pick.car, f"{where} IS CAR")
    is_dd = _need(pick.max_drawdown, f"{where} IS max_drawdown")
    o_ret = _need(oos.total_return, f"{where} OOS total_return")
    o_dd = _need(oos.max_drawdown, f"{where} OOS max_drawdown")
    if oos.trades is None:
        raise MissingMetric(f"missing metric: {where} OOS trades = None")
    o_trades = int(oos.trades)
    o_car = annualise(o_ret, fold.test_years)
    if o_car is None:
        raise MissingMetric(f"missing metric: {where} OOS CAR")
    reasons: List[str] = []
    if o_trades == 0:
        reasons.append("zero OOS trades")
    if o_ret < th.min_oos_return:
        reasons.append(f"OOS return {o_ret:.2f}% < {th.min_oos_return:g}%")
    if abs(o_dd) > th.max_dd_mult * abs(is_dd):
        reasons.append(f"OOS |maxDD| {abs(o_dd):.2f}% > {th.max_dd_mult:g} x IS |maxDD| "
                       f"{abs(is_dd):.2f}%")
    return GenomeFold(
        fold=fold.index, rank=pick.rank, is_fitness=_need(pick.fitness, f"{where} IS fitness"),
        is_return=is_ret, is_car=is_car, is_max_dd=is_dd, is_trades=int(pick.trades),
        oos_return=o_ret, oos_car=o_car, oos_max_dd=o_dd, oos_trades=o_trades,
        oos_backtest_id=oos.backtest_id, efficiency=efficiency(o_car, is_car),
        passed=not reasons, fail_reasons=reasons)


# --------------------------------------------------------------------------------- aggregation


def compound(returns_pct: Sequence[float]) -> float:
    """Compound percent returns: [10, -50] -> -45.0."""
    g = 1.0
    for r in returns_pct:
        g *= 1.0 + r / 100.0
    return (g - 1.0) * 100.0


@dataclass
class Stitch:
    folds: List[int]
    total_return: float
    oos_car: float
    worst_fold_dd: float            # WORST SINGLE-FOLD drawdown (largest |dd|), NOT chained across
                                    # folds: fold equity curves are not read (summary columns only)
    trades: int
    is_car_mean: float              # arithmetic mean of the folds' IS CARs
    efficiency: Optional[float]     # oos_car / is_car_mean; None when is_car_mean <= 0
    years: float                    # total test years (embargo gaps excluded)


def stitch(items: Sequence[Tuple[Fold, GenomeFold]]) -> Stitch:
    """Chain the out-of-sample results of one genome per fold (in fold order)."""
    if not items:
        raise WalkForwardError("nothing to stitch")
    total = compound([g.oos_return for _, g in items])
    years = sum(f.test_years for f, _ in items)
    car = annualise(total, years)
    if car is None:
        raise MissingMetric("missing metric: stitched CAR")
    is_mean = sum(g.is_car for _, g in items) / len(items)
    return Stitch(folds=[f.index for f, _ in items], total_return=total, oos_car=car,
                  worst_fold_dd=max((g.oos_max_dd for _, g in items), key=abs),
                  trades=sum(g.oos_trades for _, g in items), is_car_mean=is_mean,
                  efficiency=efficiency(car, is_mean), years=years)


@dataclass
class WalkForwardResult:
    prefix: str
    thresholds: Thresholds
    top_n: int
    folds: List[Fold]                       # the folds RUN
    planned_folds: int
    genomes: List[GenomeFold]
    rank_stitches: Dict[int, Stitch]
    winner_stitch: Optional[Stitch]
    winners_passed: int
    selection: Dict[str, Any]               # the selection knobs USED (persist_distinct_topn's)
    complete: bool                          # every planned fold was run
    overall_passed: Optional[bool]          # None = incomplete (--only-fold)
    notes: List[str]

    def fold_winner(self, k: int) -> GenomeFold:
        return next(g for g in self.genomes if g.fold == k and g.rank == 1)


def aggregate(prefix: str, folds: Sequence[Fold], planned_folds: int, top_n: int,
              genomes: Sequence[GenomeFold], th: Thresholds,
              selection: Optional[Dict[str, Any]] = None) -> WalkForwardResult:
    by_fold = {f.index: f for f in folds}
    notes: List[str] = []
    ranks = sorted({g.rank for g in genomes})
    rank_stitches: Dict[int, Stitch] = {}
    for r in ranks:
        items = [(by_fold[g.fold], g) for g in sorted(genomes, key=lambda g: g.fold) if g.rank == r]
        if len(items) != len(folds):
            notes.append(f"rank {r} exists in only {len(items)}/{len(folds)} folds: not stitched")
            continue
        rank_stitches[r] = stitch(items)
    winners = [(by_fold[g.fold], g) for g in sorted(genomes, key=lambda g: g.fold) if g.rank == 1]
    if len(winners) != len(folds):
        raise WalkForwardError("a fold has no rank-1 genome")
    winner_stitch = stitch(winners)
    passed = sum(1 for _, g in winners if g.passed)
    complete = len(folds) == planned_folds
    if not complete:
        notes.append(f"PARTIAL RUN: {len(folds)} of {planned_folds} folds; no overall verdict")
    return WalkForwardResult(
        prefix=prefix, thresholds=th, top_n=top_n, folds=list(folds), planned_folds=planned_folds,
        genomes=list(genomes), rank_stitches=rank_stitches, winner_stitch=winner_stitch,
        winners_passed=passed, selection=dict(selection or {}), complete=complete,
        overall_passed=(passed >= th.min_folds) if complete else None, notes=notes)


# --------------------------------------------------------------------------------- runner


class Runner(Protocol):
    """Subprocess / DB side. The real one is tools/run_walk_forward.py:RealRunner."""

    def train_completed(self, name: str) -> bool: ...
    def run_train(self, cmd: List[str], name: str) -> None: ...
    def select(self, train_name: str, top_n: int) -> List[IsPick]: ...
    def run_oos(self, train_name: str, fold: Fold, picks: Sequence[IsPick], name_prefix: str,
                labels: Sequence[str], parallel: int) -> Dict[int, OosMetrics]: ...


@dataclass
class Plan:
    prefix: str
    folds: List[Fold]
    optimize_args: List[str]
    launcher: Any
    top_n: int
    thresholds: Thresholds
    test_parallel: int = 1
    skip_train: bool = False
    only_fold: Optional[int] = None
    embargo_days: int = 0
    selection: Dict[str, Any] = field(default_factory=dict)   # the selection knobs USED

    def selected_folds(self) -> List[Fold]:
        if self.only_fold is None:
            return list(self.folds)
        sel = [f for f in self.folds if f.index == self.only_fold]
        if not sel:
            raise WalkForwardError(
                f"--only-fold {self.only_fold}: plan has folds 1..{len(self.folds)}")
        return sel


def make_plan(prefix: str, folds: Sequence[Fold], optimize_args: Sequence[str], launcher: Any,
              top_n: int, thresholds: Thresholds, *, test_parallel: int, skip_train: bool,
              only_fold: Optional[int], embargo_days: int, selection: Dict[str, Any]) -> Plan:
    """Validate everything BEFORE any run: a refusal here costs nothing."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", prefix or ""):
        raise WalkForwardError(f"--prefix {prefix!r}: use letters, digits, '_', '.', '-'")
    if top_n < 1:
        raise WalkForwardError(f"--top-n must be >= 1, got {top_n}")
    if test_parallel < 1:
        raise WalkForwardError(f"--test-parallel must be >= 1, got {test_parallel}")
    check_user_optimize_args(optimize_args)
    validate_folds(folds, embargo_days)
    thresholds.validate(len(folds))
    plan = Plan(prefix, list(folds), list(optimize_args), launcher, top_n, thresholds,
                test_parallel, skip_train, only_fold, embargo_days, dict(selection))
    plan.selected_folds()          # refuses an unknown --only-fold
    return plan


def render_dry_run(plan: Plan) -> str:
    th = plan.thresholds
    lines = [f"walk-forward plan '{plan.prefix}': {len(plan.folds)} fold(s), top-n {plan.top_n}, "
             f"embargo {plan.embargo_days}d, test-parallel {plan.test_parallel}",
             f"thresholds: OOS return >= {th.min_oos_return:g}% per fold, OOS |maxDD| <= "
             f"{th.max_dd_mult:g} x IS |maxDD|, >= {th.min_folds} fold winner(s) must pass"]
    for f in plan.selected_folds():
        name = train_job_name(plan.prefix, f.index)
        lines.append(f"\nfold {f.index}: train {f.train_start}..{f.train_end} "
                     f"({f.train_years:.2f}y), test {f.test_start}..{f.test_end} "
                     f"({f.test_years:.2f}y)")
        if plan.skip_train:
            lines.append("  train: SKIPPED (--skip-train; the job must already be completed)")
        else:
            lines.append(f"  train job: {name}  (skipped when already completed)")
            lines.append("  train cmd: " + " ".join(train_command(plan.launcher, plan.optimize_args,
                                                                  plan.prefix, f)))
        lines.append(f"  select: top {plan.top_n} behaviour-distinct genomes "
                     f"(tools/persist_distinct_topn.py selection; {plan.selection})")
        lines.append(f"  test: re-run each once on {f.test_start}..{f.test_end} as "
                     f"{oos_name_prefix(f.index)}<rank>-{name}  labels={oos_labels(f.index)}")
    lines.append("\n--dry-run: nothing was run.")
    return "\n".join(lines)


def run_walk_forward(plan: Plan, runner: Runner, log: Callable[[str], None] = print
                     ) -> WalkForwardResult:
    folds = plan.selected_folds()
    genomes: List[GenomeFold] = []
    for f in folds:
        name = train_job_name(plan.prefix, f.index)
        if plan.skip_train:
            if not runner.train_completed(name):
                raise WalkForwardError(f"--skip-train: training job {name!r} is not completed")
        elif runner.train_completed(name):
            log(f"fold {f.index}: training job {name} already completed -- skipping the train run")
        else:
            log(f"fold {f.index}: training {name} on {f.train_start}..{f.train_end}")
            runner.run_train(train_command(plan.launcher, plan.optimize_args, plan.prefix, f), name)
            if not runner.train_completed(name):
                raise WalkForwardError(f"training job {name!r} did not reach status 'completed'")
        picks = runner.select(name, plan.top_n)
        if not picks:
            raise WalkForwardError(f"fold {f.index}: {name!r} has no eligible genome to test")
        if sorted(p.rank for p in picks)[0] != 1:
            raise WalkForwardError(f"fold {f.index}: selection has no rank 1")
        if len(picks) < plan.top_n:
            log(f"fold {f.index}: only {len(picks)} behaviour-distinct genome(s) (< --top-n "
                f"{plan.top_n})")
        log(f"fold {f.index}: testing {len(picks)} genome(s) on {f.test_start}..{f.test_end}")
        oos = runner.run_oos(name, f, picks, oos_name_prefix(f.index), oos_labels(f.index),
                             plan.test_parallel)
        for p in picks:
            if p.rank not in oos:
                raise WalkForwardError(
                    f"fold {f.index} rank {p.rank}: no out-of-sample result was produced")
            genomes.append(evaluate_genome(f, p, oos[p.rank], plan.thresholds))
    return aggregate(plan.prefix, folds, len(plan.folds), plan.top_n, genomes, plan.thresholds,
                     plan.selection)


# --------------------------------------------------------------------------------- report


def _f(v: Optional[float], spec: str = ".2f", suffix: str = "%") -> str:
    return "n/a" if v is None else f"{format(v, spec)}{suffix}"


def _eff(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def result_to_json(res: WalkForwardResult) -> Dict[str, Any]:
    return {
        "prefix": res.prefix,
        "thresholds": asdict(res.thresholds),
        "top_n": res.top_n,
        "selection": res.selection,
        "complete": res.complete,
        "overall_passed": res.overall_passed,
        "winners_passed": res.winners_passed,
        "notes": res.notes,
        "folds": [{"index": f.index, "train_start": f.train_start.isoformat(),
                   "train_end": f.train_end.isoformat(), "test_start": f.test_start.isoformat(),
                   "test_end": f.test_end.isoformat()} for f in res.folds],
        "genomes": [asdict(g) for g in res.genomes],
        "rank_stitches": {str(r): asdict(s) for r, s in res.rank_stitches.items()},
        "winner_stitch": asdict(res.winner_stitch) if res.winner_stitch else None,
    }


def render_markdown(res: WalkForwardResult) -> str:
    th = res.thresholds
    verdict = ("INCOMPLETE (partial run)" if res.overall_passed is None
               else "PASS" if res.overall_passed else "FAIL")
    out = [f"# Walk-forward report: {res.prefix}", "",
           f"**Overall verdict: {verdict}** -- {res.winners_passed} of {len(res.folds)} fold "
           f"winner(s) passed (need {th.min_folds}).", "",
           "## Thresholds used", "",
           f"- `--pass-min-oos-return` {th.min_oos_return:g}% (per-fold TOTAL OOS return over that "
           f"fold's test window, NOT annualised; window lengths are in each fold heading)",
           f"- `--pass-max-dd-mult` {th.max_dd_mult:g} (OOS |max DD| <= mult x IS |max DD|)",
           f"- `--pass-min-folds` {th.min_folds} (fold winners that must pass)",
           "- a genome with zero OOS trades FAILS",
           f"- top-n {res.top_n}; selection knobs used: "
           + (", ".join(f"{k}={v}" for k, v in res.selection.items()) or "n/a"), ""]
    for n in res.notes:
        out.append(f"> {n}")
    if res.notes:
        out.append("")
    out += ["Returns and drawdowns in percent. CAR = annualised total return over the window. "
            "Efficiency = OOS CAR / IS CAR (n/a when IS CAR <= 0).", ""]
    for f in res.folds:
        out += [f"## Fold {f.index}: train {f.train_start}..{f.train_end} ({f.train_years:.2f}y), "
                f"test {f.test_start}..{f.test_end} ({f.test_years:.2f}y)", "",
                "| rank | IS fitness | IS return | IS CAR | IS maxDD | IS trades | OOS return | "
                "OOS CAR | OOS maxDD | OOS trades | eff | verdict |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for g in sorted((g for g in res.genomes if g.fold == f.index), key=lambda g: g.rank):
            v = "PASS" if g.passed else "FAIL: " + "; ".join(g.fail_reasons)
            out.append(f"| {g.rank} | {g.is_fitness:.4f} | {_f(g.is_return)} | {_f(g.is_car)} | "
                       f"{_f(g.is_max_dd)} | {g.is_trades} | {_f(g.oos_return)} | {_f(g.oos_car)} "
                       f"| {_f(g.oos_max_dd)} | {g.oos_trades} | {_eff(g.efficiency)} | {v} |")
        out.append("")
    out += ["## Stitched out-of-sample, rank-1 per fold (deploying each fold's winner)", ""]
    s = res.winner_stitch
    out += [_stitch_table({"rank 1 / fold winners": s}), ""]
    if res.rank_stitches:
        out += ["## Stitched out-of-sample per rank (rank r of every fold)", "",
                _stitch_table({f"rank {r}": st for r, st in sorted(res.rank_stitches.items())}), ""]
    out.append("Stitch: fold OOS returns compounded; CAR over the summed test years; worst DD = "
               "worst single-fold drawdown (NOT a chained drawdown: fold equity curves are not "
               "read); IS CAR = mean of the folds' IS CARs.")
    return "\n".join(out) + "\n"


def _stitch_table(rows: Dict[str, Stitch]) -> str:
    lines = ["| genome | folds | OOS return | OOS CAR | worst single-fold DD | trades | IS CAR (mean) | eff |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for name, s in rows.items():
        lines.append(f"| {name} | {','.join(map(str, s.folds))} | {_f(s.total_return)} | "
                     f"{_f(s.oos_car)} | {_f(s.worst_fold_dd)} | {s.trades} | {_f(s.is_car_mean)} | "
                     f"{_eff(s.efficiency)} |")
    return "\n".join(lines)


def write_report(res: WalkForwardResult, out_dir: str) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    jp, mp = os.path.join(out_dir, "report.json"), os.path.join(out_dir, "report.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(result_to_json(res), fh, indent=2)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(res))
    return jp, mp
