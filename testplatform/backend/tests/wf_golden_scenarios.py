"""Walk-forward default-behaviour scenarios, rendered to one JSON-able dict.

``data/wf_golden_default.json`` was written by ``run_all()`` from the code BEFORE the restarts /
no-chain-seed / set-score / min-train-years options existed (TEST 2026.11.0205, dev fc1e1c5c);
tests/test_wf_golden_default.py re-runs the scenarios and compares, so the defaults keep every name,
command, report file and DB report byte-identical. Fake runners only; nothing is optimized.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
for _p in (os.path.dirname(_HERE), os.path.join(_REPO, "tools"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.services import walk_forward as WF  # noqa: E402
from app.services import wf_chain as C  # noqa: E402
from app.services import wf_store as S  # noqa: E402
from tests.test_walk_forward import FakeRunner, SEL, _oos, _pick  # noqa: E402

D = WF._iso
GOOD, BAD = _oos(5.0, -10.0), _oos(-5.0, -10.0)
TH = WF.Thresholds(0.0, 1.5, 2)
BASE = ["--expert", "FMPRating", "--strategy", "S2", "--population", "200"]


class _Runner(FakeRunner):
    def __init__(self, oos_by_fold, prefix="cell", completed=()):
        picks = {f"{prefix}-wf{k}": [_pick(1), _pick(2, ret=60.0, car=15.0, dd=-12.0)]
                 for k in (1, 2, 3)}
        oos = {(k, r): oos_by_fold[k - 1] for k in (1, 2, 3) for r in (1, 2)}
        super().__init__(completed=completed, is_picks=picks, oos=oos)
        self.prefix = prefix

    def seed_lineage(self, name):
        p = self.prefix
        return {f"{p}-wf1": None, f"{p}-wf2": f"{p}-wf1", f"{p}-wf3": f"{p}-wf2",
                f"{p}-refit": f"{p}-wf3"}[name]

    def train_identity(self, name):
        return {"id": len(name), "completed_at": "2026-10-25T10:00:00"}


def _spec(**kw):
    a = dict(name="cell", optimize_args=BASE, launcher="ba2-test", train_start=D("2020-01-01", "x"),
             test_years=[2024, 2025, 2026], data_end=None, first_generations=30,
             next_generations=15, top_n=2, thresholds=TH, require_test_years=(2026,),
             test_parallel=1, selection=SEL, expert="FMPRating", strategy="S2", band="large")
    a.update(kw)
    return C.CellSpec(**a)


def _files(d):
    out = {}
    for root, _dirs, files in os.walk(d):
        for fn in sorted(files):
            if fn == ".lock":
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, d).replace(os.sep, "/")
            with open(p, encoding="utf-8") as fh:
                out[rel] = fh.read()
    return out


def _cell(spec, runner, phase2=None):
    with tempfile.TemporaryDirectory() as d:
        res = C.run_cell(spec, runner, d, log=lambda *_: None)
        out = {"verdict": res.verdict, "refit_job": res.refit_job, "trained": runner.trained,
               "tested": runner.tested}
        if phase2 is not None:
            spec2, runner2 = phase2
            res2 = C.run_cell(spec2, runner2, d, log=lambda *_: None)
            out["phase2"] = {"verdict": res2.verdict, "trained": runner2.trained,
                             "tested": runner2.tested}
        C.write_summary([res.report], d)
        out["files"] = _files(d)
        values, report = S.report_from_files(spec.name, os.path.join(d, spec.name), None, d)
        values["report_dir"] = "<DIR>"
        out["import"] = json.loads(json.dumps(values, default=str))
    return out


def _plan_texts(spec):
    plan, folds = C.make_cell_plan(spec)
    return {"dry_run": WF.render_dry_run(plan), "refit": C.refit_command(spec, folds),
            "spec_json": S.spec_to_json(spec),
            "commands": [plan.command_for(f) for f in folds]}


def _matrix(argv_extra, experts):
    import run_screener_capband_matrix as M
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # the driver opens DB_FILE
        plan = os.path.join(d, "plan.json")
        with open(plan, "w", encoding="utf-8") as fh:
            json.dump({"experts": {"FMPRating": ["S2"], "DeterministicScorer": ["S1"]}}, fh)
        skip = ",".join(e for e in ("FMPRating", "DeterministicScorer", "FMPEarningsDrift",
                                    "FMPInsiderClusterBuy", "FactorRanker") if e not in experts)
        argv = ["run_screener_capband_matrix.py", "--bands", "mid", "--skip-experts", skip,
                "--strategy-plan", plan, "--population", "200", "--no-budget-overrides",
                "--fmp-population-bonus", "0", "--early-stop", "5", "--early-stop-min-rel", "0.01",
                "--name-suffix=-goal2027", "--walk-forward", "--wf-report-dir", os.path.join(d, "rep"),
                "--pass-min-oos-return", "0", "--pass-max-dd-mult", "1.5", "--pass-min-folds", "2",
                "--pass-require-fold", "2026", "--wf-top-n", "2", "--store", "C:/golden/metric_store",
                *argv_extra]

        class Fake(FakeRunner):
            def select(self, train_name, top_n):
                return [_pick(1), _pick(2)][:top_n]

            def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
                self.tested.append((train_name, fold.index))
                return {p.rank: GOOD for p in picks}
        fake = Fake()
        sel = {"min_trade_rows": 30, "min_return_rel_pct": 5.0, "min_dd_pts": 2.0,
               "min_trades_rel_pct": 5.0}
        with mock.patch.object(subprocess, "run",
                               lambda cmd, **kw: SimpleNamespace(returncode=0)), \
                mock.patch.dict(os.environ, {"DB_FILE": os.path.join(d, "no-such.db")}), \
                mock.patch.object(M, "_make_wf_runner", lambda args: fake), \
                mock.patch.object(M, "_wf_selection_defaults", lambda: sel), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.print", lambda *a, **k: None):
            rc = M.main()
        trained = [(n, [t.replace(d, "<D>") for t in c]) for n, c in fake.trained]
        # the launcher path is host-specific; the job name and every other token are not
        trained = [(n, c[1:]) for n, c in trained]
        return {"rc": rc, "trained": trained, "tested": fake.tested}


def run_all():
    out = {}
    out["pass"] = _cell(_spec(), _Runner([GOOD, GOOD, GOOD]))
    out["fail_required"] = _cell(_spec(), _Runner([GOOD, GOOD, BAD]))
    out["eliminated"] = _cell(_spec(), _Runner([BAD, BAD, GOOD]))
    out["floor"] = _cell(_spec(floor=D("2022-01-01", "x")), _Runner([GOOD, BAD, GOOD]))
    out["deferred"] = _cell(
        _spec(data_end=D("2025-12-31", "x"), defer_last_test=True), _Runner([GOOD, GOOD, GOOD]),
        phase2=(_spec(defer_last_test=True),
                _Runner([GOOD, GOOD, GOOD], completed={"cell-wf1", "cell-wf2", "cell-wf3"})))
    out["plan_default"] = _plan_texts(_spec())
    out["plan_floor_defer"] = _plan_texts(_spec(floor=D("2022-01-01", "x"),
                                                data_end=D("2025-12-31", "x"), defer_last_test=True))
    out["matrix_ds"] = _matrix([], ("DeterministicScorer",))
    out["matrix_fr_defer"] = _matrix(["--wf-defer-last-test", "--wf-data-end", "2025-12-31"],
                                     ("FMPRating",))
    out["matrix_ds_years"] = _matrix(["--wf-test-years", "2022,2023,2024,2025,2026",
                                      "--wf-defer-last-test", "--wf-data-end", "2025-12-31"],
                                     ("DeterministicScorer",))
    return json.loads(json.dumps(out, default=str))
