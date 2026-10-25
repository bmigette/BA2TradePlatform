"""``run_screener_capband_matrix.py --walk-forward`` (goal2027 item 2.5): every cell becomes a chain
``<cell>-wf1/-wf2/-wf3`` (+ ``-refit`` when it passes) built from the cell's OWN flags. The real
runner (ba2-test subprocess + test DB) is replaced by a fake; nothing is optimized."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import run_screener_capband_matrix as M  # noqa: E402
from app.services import walk_forward as WF  # noqa: E402
from test_walk_forward import FakeRunner, _oos, _pick  # noqa: E402

PLAN = {"experts": {"FMPRating": ["S2"], "DeterministicScorer": ["S1"]}}
GOOD, BAD = _oos(5.0, -10.0), _oos(-5.0, -10.0)
THRESH = ["--pass-min-oos-return", "0", "--pass-max-dd-mult", "1.5", "--pass-min-folds", "2",
          "--pass-require-fold", "2026"]


class Fake(FakeRunner):
    """Records every train command in order, whatever the cell."""

    def __init__(self, oos_by_fold=(GOOD, GOOD, GOOD)):
        super().__init__()
        self.oos_by_fold = oos_by_fold
        self.order = []

    def run_train(self, cmd, name):
        self.order.append(name)
        super().run_train(cmd, name)

    def select(self, train_name, top_n):
        return [_pick(1), _pick(2)][:top_n]

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel):
        return {p.rank: self.oos_by_fold[fold.index - 1] for p in picks}


@pytest.fixture
def env(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or SimpleNamespace(returncode=0))
    monkeypatch.setenv("DB_FILE", str(tmp_path / "no-such.db"))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN), encoding="utf-8")
    fake = Fake()
    monkeypatch.setattr(M, "_make_wf_runner", lambda args: fake)
    monkeypatch.setattr(M, "_wf_selection_defaults",
                        lambda: {"min_trade_rows": 30, "min_return_rel_pct": 5.0,
                                 "min_dd_pts": 2.0, "min_trades_rel_pct": 5.0})
    return SimpleNamespace(calls=calls, fake=fake, plan=str(plan), report=str(tmp_path / "rep"))


def _argv(env, *extra, experts=("FMPRating",)):
    skip = ",".join(e for e in ("FMPRating", "DeterministicScorer", "FMPEarningsDrift",
                                "FMPInsiderClusterBuy", "FactorRanker") if e not in experts)
    return ["run_screener_capband_matrix.py", "--bands", "mid", "--skip-experts", skip,
            "--strategy-plan", env.plan, "--population", "200", "--no-budget-overrides",
            "--fmp-population-bonus", "0", "--early-stop", "5", "--early-stop-min-rel", "0.01",
            "--name-suffix=-goal2027", "--walk-forward", "--wf-report-dir", env.report,
            *THRESH, "--wf-top-n", "2", *extra]


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _cmds(fake):
    return {n: c for n, c in fake.trained}


def test_chain_names_order_and_per_fold_flags(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, experts=("DeterministicScorer",)))
    assert M.main() == 0
    assert env.calls == []                                     # nothing went through subprocess
    cell = next(n for n in env.fake.order if n.endswith("-wf1"))[:-4]
    assert cell.startswith("scr-mid-DeterministicScorer-S1-goal2027")
    assert env.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3", f"{cell}-refit"]
    c = _cmds(env.fake)
    assert (_flag(c[f"{cell}-wf1"], "--start"), _flag(c[f"{cell}-wf1"], "--end")) == (
        "2020-01-01", "2023-12-31")
    assert _flag(c[f"{cell}-wf3"], "--end") == "2025-12-31"
    assert _flag(c[f"{cell}-refit"], "--end") == "2026-12-31"
    assert [_flag(c[f"{cell}-{s}"], "--generations") for s in ("wf1", "wf2", "wf3", "refit")] == [
        "30", "15", "15", "15"]
    for s in ("wf1", "wf2", "wf3", "refit"):                   # the cell's own flags on every job
        j = c[f"{cell}-{s}"]
        assert _flag(j, "--population") == "200" and _flag(j, "--screener-cap-band") == "mid"
        assert _flag(j, "--early-stop") == "5" and _flag(j, "--early-stop-min-rel") == "0.01"
        assert _flag(j, "--strategy") == "S1" and "--screener" in j
    assert _flag(c[f"{cell}-wf1"], "--chain-seed-from") is None
    assert _flag(c[f"{cell}-wf2"], "--chain-seed-from") == f"{cell}-wf1"
    assert _flag(c[f"{cell}-refit"], "--chain-seed-from") == f"{cell}-wf3"


def test_fmprating_floor_keeps_from2022_and_trains_from_2022(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, experts=("FMPRating",)))
    assert M.main() == 0
    assert env.fake.order[0] == "scr-mid-FMPRating-S2-goal2027-from2022-wf1" or \
        env.fake.order[0].startswith("scr-mid-FMPRating-S2-goal2027-from2022")
    assert all("-from2022" in n for n in env.fake.order)
    c = _cmds(env.fake)
    assert {_flag(v, "--start") for v in c.values()} == {"2022-01-01"}
    assert _flag(c[env.fake.order[0]], "--end") == "2023-12-31"


def test_failing_cell_gets_no_refit_and_the_summary_lists_it(env, monkeypatch):
    env.fake.oos_by_fold = (GOOD, GOOD, BAD)                   # 2026 is required
    monkeypatch.setattr(sys, "argv", _argv(env, experts=("DeterministicScorer",)))
    assert M.main() == 0                                       # a FAIL verdict is a result, not an error
    assert not any(n.endswith("-refit") for n in env.fake.order)
    summ = json.loads(open(os.path.join(env.report, "summary.json"), encoding="utf-8").read())
    assert [c["verdict"] for c in summ["cells"]] == ["FAIL"]


def test_data_end_clamps_the_last_test_window_and_the_refit(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-data-end", "2026-10-12",
                                           experts=("DeterministicScorer",)))
    assert M.main() == 0
    c = _cmds(env.fake)
    assert _flag(next(v for n, v in c.items() if n.endswith("-refit")), "--end") == "2026-10-12"
    cell = json.loads(open(os.path.join(env.report, env.fake.order[0][:-4], "cell.json"),
                           encoding="utf-8").read())
    assert cell["folds"][2]["test"] == ["2026-01-01", "2026-10-12"] and cell["data_end"] == "2026-10-12"


def test_empty_last_window_is_refused_before_anything_runs(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-data-end", "2026-01-01",
                                           experts=("DeterministicScorer",)))
    with pytest.raises(SystemExit, match="empty"):
        M.main()
    assert env.fake.order == []


def test_resume_skips_completed_folds(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, experts=("DeterministicScorer",)))
    assert M.main() == 0
    first = list(env.fake.order)
    again = Fake()
    again.completed = set(first)                               # everything is completed in the DB
    monkeypatch.setattr(M, "_make_wf_runner", lambda args: again)
    assert M.main() == 0
    assert again.order == []                                   # nothing retrained, refit not rerun


def test_dry_run_prints_the_chain_and_runs_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", _argv(env, "--dry-run", experts=("DeterministicScorer",)))
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "-wf1" in out and "-wf3" in out and "-refit" in out
    assert "--chain-seed-from" in out and "--generations 30" in out
    assert env.fake.order == [] and env.calls == []
    assert not os.path.exists(env.report)


def test_a_cell_with_a_failing_job_makes_the_driver_exit_nonzero_but_continues(env, monkeypatch):
    class Boom(Fake):
        def run_train(self, cmd, name):
            if "DeterministicScorer" in name and name.endswith("-wf1"):
                raise WF.WalkForwardError("training job exited with code 1")
            super().run_train(cmd, name)
    boom = Boom()
    monkeypatch.setattr(M, "_make_wf_runner", lambda args: boom)
    monkeypatch.setattr(sys, "argv", _argv(env, experts=("FMPRating", "DeterministicScorer")))
    assert M.main() == 1
    assert any("FMPRating" in n and n.endswith("-refit") for n in boom.order)   # the other cell ran
    summ = json.loads(open(os.path.join(env.report, "summary.json"), encoding="utf-8").read())
    assert sorted(c["verdict"] for c in summ["cells"]) == ["ERROR", "PASS"]


@pytest.mark.parametrize("drop", ["--pass-min-oos-return", "--pass-max-dd-mult",
                                  "--pass-min-folds", "--wf-report-dir", "--wf-top-n"])
def test_required_flags_have_no_hidden_defaults(env, monkeypatch, drop):
    argv = _argv(env)
    i = argv.index(drop)
    del argv[i:i + 2]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        M.main()


def test_wf_flags_without_walk_forward_are_refused(env, monkeypatch):
    argv = [a for a in _argv(env) if a != "--walk-forward"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        M.main()


def test_require_fold_for_a_year_outside_the_plan_is_refused(env, monkeypatch):
    argv = _argv(env, experts=("DeterministicScorer",))
    argv[argv.index("2026")] = "2030"
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match="2030"):
        M.main()
