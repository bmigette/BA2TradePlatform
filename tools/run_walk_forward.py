#!/usr/bin/env python
"""Walk-forward validation of ANY ``ba2-test optimize`` job (stock or option).

Per fold (anchored, expanding): train the optimize job on the fold's train window, take the top-N
BEHAVIOUR-DISTINCT genomes (the selection of tools/persist_distinct_topn.py), re-run each ONCE on
the fold's test window and compare in-sample with out-of-sample. Writes JSON + Markdown to
reports/walk_forward/<prefix>/ with a pass/fail verdict against EXPLICIT thresholds. Design and
semantics: docs/plans/2026-10-02-walk-forward-engine-design.md. The pure logic (folds, aggregation,
verdict, report) is testplatform/backend/app/services/walk_forward.py.

The optimize args go after ``--`` and must NOT contain --start/--end/--name:

  python tools/run_walk_forward.py --prefix wf-fr-large --train-start 2020-01-01 \\
      --test-years 2024,2025,2026 --test-end 2026-06-30 --top-n 3 \\
      --pass-min-oos-return 0 --pass-max-dd-mult 1.5 --pass-min-folds 2 --dry-run \\
      -- --expert FMPRating --strategy S2 --universe AAPL,MSFT

  --fold TRAIN_START:TRAIN_END:TEST_START:TEST_END   explicit folds instead of --test-years
                                                    (repeatable); not combinable with it
  --embargo-days N     N calendar days between train end and test start (year folds: the train
                       end moves back, the test year stays whole)
  --dry-run            print the plan, the exact commands and names; run nothing
  --only-fold K        run fold K only (no overall verdict)
  --skip-train         the train jobs must already be completed; steps 2-4 only
  --test-parallel P    out-of-sample re-runs at a time (default 1; ~14 GB RAM each)
  --min-trade-rows / --min-return-rel-pct / --min-dd-pts / --min-trades-rel-pct
                       the persist_distinct_topn selection knobs (same defaults as that tool)
  --min-free-gb / --force-memory                     same memory floor as that tool

RESUME. A training job whose StrategyOptimization of that name is already ``completed`` is skipped
(the rule tools/run_options2_matrix.py uses). A completed out-of-sample Backtest of the same name
and window is reused rather than re-run. Exit codes: 0 verdict PASS / partial / dry-run, 1 a run
failed, 2 refused, 3 verdict FAIL.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import date
from typing import Dict, List, Optional, Sequence

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO, "testplatform", "backend")
for _p in (_BACKEND, os.path.join(_REPO, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.services import walk_forward as WF  # noqa: E402


def selection_defaults() -> Dict[str, object]:
    """The selection knobs' DECLARED defaults, read from persist_distinct_topn's own parser so the
    two tools can never drift apart."""
    import persist_distinct_topn as P
    ns = P._parse(["--opt-id", "0"])
    return {"min_trade_rows": ns.min_trade_rows, "min_return_rel_pct": ns.min_return_rel_pct,
            "min_dd_pts": ns.min_dd_pts, "min_trades_rel_pct": ns.min_trades_rel_pct}


class RealRunner:
    """Subprocess + test-DB side of the engine. Reuses ``tools/persist_distinct_topn.py`` for the
    selection and ``ba2test_launcher._persist_top_backtests`` (``window=`` override) for the
    out-of-sample re-run + Backtest row, so nothing about the trial build is reimplemented."""

    def __init__(self, args):
        self.args = args
        self._persist_tool = None
        self._launcher = None
        self._opt: Dict[str, tuple] = {}       # train name -> (opt_id, expert)

    # -- lazy bootstrap: dry-run and unit tests never touch the backend
    def _tool(self):
        if self._persist_tool is None:
            import persist_distinct_topn as P
            self._persist_tool = P
            self._launcher = P._bootstrap()
        return self._persist_tool

    def launcher_module(self):
        self._tool()
        return self._launcher

    def _opts_named(self, name: str):
        """(id, status) of every optimization of this name (name/status columns only)."""
        self._tool()
        from app.models.database import SessionLocal
        from app.models.strategy_optimization import StrategyOptimization as SO
        db = SessionLocal()
        try:
            return [(i, st) for i, st in db.query(SO.id, SO.status).filter(SO.name == name)
                    .order_by(SO.id).all()]
        finally:
            db.close()

    def train_completed(self, name: str) -> bool:
        """True when the job exists and is completed (the rule of the grid drivers). More than
        one optimization of the name is REFUSED up front: ``persist_distinct_topn._load_opt`` would
        refuse it later, after the expensive part."""
        rows = self._opts_named(name)
        if len(rows) > 1:
            raise WF.WalkForwardError(
                f"{len(rows)} optimizations are named {name!r} "
                f"({', '.join(f'id={i} {st}' for i, st in rows)}). A failed train plus a re-run "
                f"under the same name does this. Delete the unwanted row(s) from the test DB "
                f"(or use a new --prefix) so exactly one remains.")
        return bool(rows) and rows[0][1] == "completed"

    def run_train(self, cmd: List[str], name: str) -> None:
        stale = self._opts_named(name)
        if stale:
            raise WF.WalkForwardError(
                f"optimization {name!r} already exists and is not completed "
                f"({', '.join(f'id={i} {st}' for i, st in stale)}): re-running would create a "
                f"duplicate name. Delete that row (or let a running job finish), or use a new "
                f"--prefix.")
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        if rc != 0:
            raise WF.WalkForwardError(f"training job {name!r} exited with code {rc}")

    def select(self, train_name: str, top_n: int) -> List[WF.IsPick]:
        P = self._tool()
        from app.models.database import SessionLocal
        from app.services.distinct_topn import Tolerances
        a = self.args           # selection knobs already resolved by main()
        tol = Tolerances(return_rel_pct=a.min_return_rel_pct, dd_pts=a.min_dd_pts,
                         trades_rel_pct=a.min_trades_rel_pct)
        db = SessionLocal()
        try:
            try:
                opt = P._load_opt(db, None, train_name)
                if opt.status != "completed":
                    raise P.Refused(f"optimization {opt.id} ({opt.name}) is {opt.status!r}")
                _bt, expert, _years, picks, stats = P.select_picks(
                    opt, top_n, a.min_trade_rows, tol)
            except P.Refused as e:
                raise WF.WalkForwardError(str(e)) from e
            self._opt[train_name] = (opt.id, expert)
        finally:
            db.close()
        out = []
        for p in picks:
            if p.car is None:
                raise WF.MissingMetric(f"{train_name} rank {p.rank}: no IS CAR")
            out.append(WF.IsPick(rank=p.rank, fitness=p.fitness, total_return=p.total_return,
                                 car=p.car, max_drawdown=p.max_drawdown, trades=p.trades,
                                 params=p.params, key=p.key))
        print(f"{train_name}: {stats['eligible']} eligible genomes, "
              f"{stats['distinct_behaviours']} distinct behaviours, {len(out)} selected")
        return out

    def _summaries(self, db, opt_id: int, names: Sequence[str]) -> Dict[str, tuple]:
        """Summary columns only -- never the curve/trade blobs (backtests blob-layout note). Every
        column read here is part of the covering index ``ix_backtests_summary``, so the planner
        answers from the index. ALL statuses are returned: the caller refuses a non-completed
        row (a crash can leave a ``running`` one) instead of creating a duplicate."""
        from app.models.backtest import Backtest
        rows = (db.query(Backtest.id, Backtest.name, Backtest.status, Backtest.start_date,
                         Backtest.end_date, Backtest.ga_fitness, Backtest.total_return,
                         Backtest.max_drawdown, Backtest.total_trades)
                  .filter(Backtest.optimization_id == opt_id, Backtest.name.in_(list(names)))
                  .order_by(Backtest.id).all())
        return {r[1]: r for r in rows}

    @staticmethod
    def _metrics(row, reused: bool) -> WF.OosMetrics:
        return WF.OosMetrics(backtest_id=row[0], total_return=row[6], max_drawdown=row[7],
                             trades=row[8], reused=reused)

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel):
        # Standalone re-runs bypass the GA's logging suppression (10x+ slower), as in
        # tools/run_genome_once.py. The caller's level is restored afterwards.
        prior = logging.root.manager.disable
        logging.disable(logging.WARNING)
        try:
            return self._run_oos(train_name, fold, picks, name_prefix, labels, parallel)
        finally:
            logging.disable(prior)

    def _run_oos(self, train_name, fold, picks, name_prefix, labels, parallel):
        P = self._tool()
        L = self._launcher
        from app.models.database import SessionLocal
        opt_id, expert = self._opt[train_name]
        window = (fold.test_start.isoformat(), fold.test_end.isoformat())
        names = {p.rank: WF.oos_backtest_name(fold.index, p.rank, train_name) for p in picks}
        db = SessionLocal()
        try:
            have = self._summaries(db, opt_id, list(names.values()))
        finally:
            db.close()
        out: Dict[int, WF.OosMetrics] = {}
        todo = []
        for p in picks:
            row = have.get(names[p.rank])
            if row is None:
                todo.append(p)
                continue
            if row[2] != "completed":
                raise WF.WalkForwardError(
                    f"Backtest {names[p.rank]!r} (id {row[0]}) exists with status {row[2]!r} (a "
                    f"crashed or running earlier attempt). Delete it before resuming; re-running "
                    f"would create a duplicate.")
            same_window = (row[3].date().isoformat(), row[4].date().isoformat()) == window
            if not same_window or row[5] is None or abs(row[5] - p.fitness) > 1e-9:
                raise WF.WalkForwardError(
                    f"completed Backtest {names[p.rank]!r} (id {row[0]}) exists but is not this "
                    f"fold/genome (window {row[3].date()}..{row[4].date()}, ga_fitness {row[5]}); "
                    f"delete it or use a new --prefix")
            print(f"  reusing {names[p.rank]} (backtest {row[0]})")
            out[p.rank] = self._metrics(row, True)
        if todo:
            try:
                P._check_memory(self.args)
            except P.Refused as e:
                raise WF.WalkForwardError(str(e)) from e
        self._rerun_chunks(todo, parallel, L, opt_id, expert, name_prefix, labels, window,
                           names, train_name, out)
        return out

    def _rerun_chunks(self, todo, parallel, L, opt_id, expert, name_prefix, labels, window,
                      names, train_name, out):
        from app.models.database import SessionLocal
        for i in range(0, len(todo), parallel):
            chunk = todo[i:i + parallel]
            ids: List[tuple] = []
            L._persist_top_backtests(
                opt_id, expert, n=len(chunk), parallel=parallel,
                candidates=[(p.params, p.key, p.fitness) for p in chunk],
                ranks=[p.rank for p in chunk], name_prefix=name_prefix, extra_labels=list(labels),
                use_remote_workers=False, persisted_ids=ids, window=window)
            db = SessionLocal()
            try:
                got = {rank: bid for rank, bid in ids}
                missing = [p.rank for p in chunk if p.rank not in got]
                if missing:
                    raise WF.WalkForwardError(
                        f"{train_name}: out-of-sample re-run for rank(s) {missing} was not "
                        f"persisted (failed or timed out; see above)")
                rows = self._summaries(db, opt_id, [names[p.rank] for p in chunk])
                for p in chunk:
                    row = rows.get(names[p.rank])
                    if row is None:
                        raise WF.WalkForwardError(f"persisted Backtest {names[p.rank]!r} not found")
                    out[p.rank] = self._metrics(row, False)
            finally:
                db.close()


def _positive_int(v: str) -> int:
    i = int(v)
    if i < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {i}")
    return i


def _parse(argv: Sequence[str]):
    argv = list(argv)
    opt_args: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, opt_args = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--train-start")
    ap.add_argument("--test-years", help="comma-separated, e.g. 2024,2025,2026")
    ap.add_argument("--test-end", help="end of the LAST test year when partial (ISO date)")
    ap.add_argument("--fold", action="append", default=[])
    ap.add_argument("--embargo-days", type=int, default=0)
    ap.add_argument("--top-n", type=_positive_int, required=True)
    ap.add_argument("--pass-min-oos-return", type=float, required=True,
                    help="percent: the fold's TOTAL out-of-sample return over its test window "
                         "(NOT annualised; mind the window length)")
    ap.add_argument("--pass-max-dd-mult", type=float, required=True)
    ap.add_argument("--pass-min-folds", type=int, required=True)
    ap.add_argument("--test-parallel", type=_positive_int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-fold", type=_positive_int)
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--launcher")
    ap.add_argument("--out-dir")
    # Selection knobs: default None = persist_distinct_topn's own declared default (resolved in
    # main via selection_defaults()); recorded in the report either way.
    ap.add_argument("--min-trade-rows", type=int, default=None)
    ap.add_argument("--min-return-rel-pct", type=float, default=None)
    ap.add_argument("--min-dd-pts", type=float, default=None)
    ap.add_argument("--min-trades-rel-pct", type=float, default=None)
    ap.add_argument("--min-free-gb", type=float, default=20.0)
    ap.add_argument("--force-memory", action="store_true")
    return ap.parse_args(argv), opt_args


def _folds_from_args(a) -> List[WF.Fold]:
    if a.fold and (a.train_start or a.test_years or a.test_end):
        raise WF.WalkForwardError("--fold cannot be combined with --train-start/--test-years/"
                                  "--test-end")
    if a.fold:
        return [WF.parse_explicit_fold(t, i) for i, t in enumerate(a.fold, start=1)]
    if not (a.train_start and a.test_years):
        raise WF.WalkForwardError("give --train-start and --test-years, or --fold")
    try:
        years = [int(y) for y in a.test_years.split(",") if y.strip()]
    except ValueError as e:
        raise WF.WalkForwardError(f"--test-years {a.test_years!r} is not a year list") from e
    test_end = WF._iso(a.test_end, "--test-end") if a.test_end else None
    return WF.build_year_folds(WF._iso(a.train_start, "--train-start"), years, test_end,
                               a.embargo_days)


def _launcher_path(a):
    if a.launcher:
        return [sys.executable, a.launcher] if a.launcher.endswith(".py") else a.launcher
    cand = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    return cand if os.path.exists(cand) else os.path.join(os.path.dirname(sys.executable),
                                                         "ba2-test")


def main(argv=None, runner: Optional[WF.Runner] = None) -> int:
    a, opt_args = _parse(sys.argv[1:] if argv is None else argv)
    defaults = selection_defaults()
    for k, v in defaults.items():
        if getattr(a, k) is None:
            setattr(a, k, v)
    selection = {k: getattr(a, k) for k in defaults}
    try:
        plan = WF.make_plan(
            a.prefix, _folds_from_args(a), opt_args, _launcher_path(a), a.top_n,
            WF.Thresholds(a.pass_min_oos_return, a.pass_max_dd_mult, a.pass_min_folds),
            test_parallel=a.test_parallel, skip_train=a.skip_train, only_fold=a.only_fold,
            embargo_days=a.embargo_days, selection=selection)
        runner = runner or RealRunner(a)
        # Option-holdout rail, REUSED unchanged: refuse a fold whose TRAIN window reaches 2026
        # for a pure-option strategy BEFORE any run (the launcher would refuse it mid-run).
        strategy = WF.optimize_arg_value(opt_args, "--strategy")
        if strategy and not a.skip_train and isinstance(runner, RealRunner):
            L = runner.launcher_module()
            for f in plan.selected_folds():
                L._assert_option_window_excludes_holdout([strategy], f.train_end.isoformat())
        if a.dry_run:
            print(WF.render_dry_run(plan))
            return 0
        res = WF.run_walk_forward(plan, runner)
    except WF.WalkForwardError as e:
        print(f"run_walk_forward: {e}", file=sys.stderr)
        return 2
    out_dir = a.out_dir or os.path.join(_REPO, "reports", "walk_forward", a.prefix)
    jp, mp = WF.write_report(res, out_dir)
    print(WF.render_markdown(res))
    print(f"report: {mp}\n        {jp}")
    return 0 if res.overall_passed is not False else 3


if __name__ == "__main__":
    sys.exit(main())
