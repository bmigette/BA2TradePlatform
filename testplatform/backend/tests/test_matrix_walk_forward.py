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

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        self.tested.append((train_name, fold.index))
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


def _cell_name(env, monkeypatch, *extra):
    env.fake.order.clear()
    env.fake.completed.clear()
    monkeypatch.setattr(sys, "argv", _argv(env, *extra, experts=("DeterministicScorer",)))
    assert M.main() == 0
    return env.fake.order[0][:-4]


def test_exclusive_return_flag_is_forwarded(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--pass-min-oos-return-exclusive",
                                           experts=("DeterministicScorer",)))
    env.fake.oos_by_fold = (_oos(0.0, -5.0),) * 3
    assert M.main() == 0
    assert not any(n.endswith("-refit") for n in env.fake.order)   # 0.0% is not ABOVE 0%


def test_exclusive_flag_without_walk_forward_is_refused(env, monkeypatch):
    argv = [a for a in _argv(env) if a != "--walk-forward"]
    # keep only the flag under test among the wf ones so the refusal is about it
    monkeypatch.setattr(sys, "argv", ["run_screener_capband_matrix.py", "--pass-min-oos-return-exclusive"])
    with pytest.raises(SystemExit):
        M.main()


@pytest.mark.parametrize("change", [
    ["--wf-first-generations", "31"], ["--wf-next-generations", "16"], ["--wf-top-n", "5"],
    ["--wf-train-start", "2021-01-01"], ["--wf-test-years", "2025,2026"],
    ["--wf-data-end", "2026-10-12"], ["--pass-min-folds", "3"], ["--pass-min-oos-return-exclusive"],
])
def test_a_changed_walk_forward_budget_or_spec_changes_the_cell_name(env, monkeypatch, change):
    base = _cell_name(env, monkeypatch)
    base_argv = _argv(env, experts=("DeterministicScorer",))
    if change[0] in base_argv and len(change) == 2:
        i = base_argv.index(change[0])
        argv = base_argv[:i] + change + base_argv[i + 2:]
    else:
        argv = base_argv + change
    env.fake.order.clear()
    monkeypatch.setattr(sys, "argv", argv)
    assert M.main() == 0
    assert env.fake.order[0][:-4] != base


def test_same_walk_forward_spec_gives_the_same_cell_name(env, monkeypatch):
    assert _cell_name(env, monkeypatch) == _cell_name(env, monkeypatch)


# ------------------------------------------------------------------ two-phase year (--wf-defer-last-test)


def test_phase_a_trains_wf3_without_testing_it_and_reports_pending(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-defer-last-test", "--wf-data-end", "2025-12-31",
                                           experts=("DeterministicScorer",)))
    assert M.main() == 0                                       # PENDING is a result, not an error
    cell = env.fake.order[0][:-4]
    assert env.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3"]
    assert _flag(_cmds(env.fake)[f"{cell}-wf3"], "--end") == "2025-12-31"
    assert [t[1] for t in env.fake.tested] == [1, 2]          # wf3 is never tested
    summ = json.loads(open(os.path.join(env.report, "summary.json"), encoding="utf-8").read())
    assert [c["verdict"] for c in summ["cells"]] == ["PENDING"]


def test_phase_a_dry_run_says_the_last_test_is_deferred(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-defer-last-test", "--wf-data-end", "2025-12-31", "--dry-run",
                                           experts=("DeterministicScorer",)))
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "fold 3: TEST DEFERRED -- the data ends 2025-12-31" in out
    assert "refit (only if the cell passes)" not in out
    assert env.fake.order == []


def test_phase_a_and_the_january_run_share_every_job_name(env, monkeypatch):
    """The January command (full data: no --wf-data-end) must resume phase A's jobs, not start over."""
    a = _cell_name(env, monkeypatch, "--wf-defer-last-test", "--wf-data-end", "2025-12-31")
    jan = _cell_name(env, monkeypatch, "--wf-defer-last-test")
    assert a == jan
    assert a != _cell_name(env, monkeypatch)                   # but never a plain cell's name


def test_january_run_after_phase_a_tests_wf3_and_refits(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-defer-last-test", "--wf-data-end", "2025-12-31",
                                           experts=("DeterministicScorer",)))
    assert M.main() == 0
    done = set(env.fake.order)
    jan = Fake()
    jan.completed = set(done)
    monkeypatch.setattr(M, "_make_wf_runner", lambda args: jan)
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-defer-last-test", experts=("DeterministicScorer",)))
    assert M.main() == 0
    assert [n[-6:] for n in jan.order] == ["-refit"]           # only the refit trains
    assert [t[1] for t in jan.tested] == [3]                   # wf1/wf2 reports reused, wf3 tested


def test_defer_with_a_data_end_inside_the_last_year_is_refused(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-defer-last-test", "--wf-data-end", "2026-10-12",
                                           experts=("DeterministicScorer",)))
    with pytest.raises(SystemExit, match="inside the last test year"):
        M.main()
    assert env.fake.order == []


@pytest.mark.parametrize("flag", ["--wf-defer-last-test", "--wf-run-all-folds"])
def test_phase_flags_without_walk_forward_are_refused(env, monkeypatch, flag):
    monkeypatch.setattr(sys, "argv", ["run_screener_capband_matrix.py", flag])
    with pytest.raises(SystemExit):
        M.main()


def test_run_all_folds_keeps_running_a_cell_that_cannot_pass(env, monkeypatch):
    env.fake.oos_by_fold = (BAD, BAD, GOOD)
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-run-all-folds", experts=("DeterministicScorer",)))
    assert M.main() == 0
    assert [n[-4:] for n in env.fake.order] == ["-wf1", "-wf2", "-wf3"]


@pytest.fixture
def senate(env, tmp_path):
    plan = tmp_path / "plan_sen.json"
    plan.write_text(json.dumps({"experts": {**PLAN["experts"], "FMPSenateTraderWeight": ["S1", "S3"]}}),
                    encoding="utf-8")
    uni = tmp_path / "senate_universe.txt"
    uni.write_text("# provenance header\nAAPL\nNVDA\n\nXOM\n", encoding="utf-8")
    env.plan, env.uni = str(plan), str(uni)
    return env


def _sen_argv(env, bands, *extra, experts=("DeterministicScorer",)):
    argv = _argv(env, *extra, experts=experts)
    argv[argv.index("--bands") + 1] = bands
    return argv + ["--senate-universe-file", env.uni]


def test_senate_band_runs_senate_cells_on_its_static_universe(senate, monkeypatch):
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "senate"))
    assert M.main() == 0
    cells = sorted({n.rsplit("-", 1)[0] for n in senate.fake.order})
    assert [c.split("-goal2027")[0] for c in cells] == ["sen-S1", "sen-S3"]
    c = _cmds(senate.fake)
    for name, cmd in c.items():
        assert _flag(cmd, "--expert") == "FMPSenateTraderWeight"
        assert _flag(cmd, "--universe") == "AAPL,NVDA,XOM"
        for screener_flag in ("--screener", "--screener-store", "--screener-cap-band",
                              "--screen-preset", "--screener-cadence-days"):
            assert screener_flag not in cmd, (name, screener_flag)
        assert "-sup1" not in name and "-lds2" not in name     # no screener universe-rule tokens
    assert {_flag(cmd, "--strategy") for cmd in c.values()} == {"S1", "S3"}


def test_senate_and_cap_bands_mix_in_one_run_without_crossing(senate, monkeypatch):
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "mid,senate",
                                               experts=("DeterministicScorer", "FactorRanker")))
    assert M.main() == 0
    experts_by_band = {}
    for cmd in _cmds(senate.fake).values():
        band = "senate" if "--screener" not in cmd else _flag(cmd, "--screener-cap-band")
        experts_by_band.setdefault(band, set()).add(_flag(cmd, "--expert"))
    assert experts_by_band == {"mid": {"DeterministicScorer", "FactorRanker"},
                               "senate": {"FMPSenateTraderWeight"}}


def test_scalar_stress_reaches_the_senate_band(senate, monkeypatch):
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "senate", "--stress-spread-bps", "13.5"))
    assert M.main() == 0
    assert {_flag(cmd, "--stress-spread-bps") for cmd in _cmds(senate.fake).values()} == {"13.5"}


def test_exclude_uncached_is_not_forwarded_to_senate(senate, monkeypatch):
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "senate", "--exclude-uncached"))
    assert M.main() == 0
    assert all("--screener-exclude-uncached" not in cmd for cmd in _cmds(senate.fake).values())


def test_senate_band_without_a_universe_file_is_refused(senate, monkeypatch):
    argv = _sen_argv(senate, "senate")
    monkeypatch.setattr(sys, "argv", argv[:-2])
    with pytest.raises(SystemExit):
        M.main()


def test_universe_file_without_the_senate_band_is_refused(senate, monkeypatch):
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "mid"))
    with pytest.raises(SystemExit):
        M.main()


def test_senate_band_with_a_plan_lacking_senate_is_refused(senate, monkeypatch):
    senate.plan = env_plan = senate.plan.replace("plan_sen", "plan_nosen")
    with open(env_plan, "w", encoding="utf-8") as fh:
        json.dump(PLAN, fh)
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "senate"))
    with pytest.raises(SystemExit, match="FMPSenateTraderWeight"):
        M.main()


def test_one_grid_summary_across_band_invocations(senate, monkeypatch):
    """The wrapper runs the driver once per band (per-band spread); every run adds to ONE summary."""
    monkeypatch.setattr(sys, "argv", _argv(senate, experts=("DeterministicScorer",)))
    assert M.main() == 0
    monkeypatch.setattr(sys, "argv", _sen_argv(senate, "senate"))
    assert M.main() == 0
    summ = json.loads(open(os.path.join(senate.report, "summary.json"), encoding="utf-8").read())
    names = [c["cell"] for c in summ["cells"]]
    assert len(names) == 3 and names[0].startswith("scr-mid-DeterministicScorer")
    assert [n.split("-goal2027")[0] for n in names[1:]] == ["sen-S1", "sen-S3"]
    # a rerun of a band REPLACES its cells in place, it never duplicates them
    monkeypatch.setattr(sys, "argv", _argv(senate, experts=("DeterministicScorer",)))
    assert M.main() == 0
    again = json.loads(open(os.path.join(senate.report, "summary.json"), encoding="utf-8").read())
    assert [c["cell"] for c in again["cells"]] == names


_MC = ["--market-condition-profile", "ohlcv-v1"]


@pytest.mark.parametrize("phase_a,january", [
    (_MC + ["--market-condition-manifest", "aaa"], _MC + ["--market-condition-manifest", "bbb"]),
    (["--exclude-symbols", "XX"], ["--exclude-symbols", "XX,YY"]),
    (["--store", "C:/old/metric_store"], ["--store", "C:/new/metric_store"]),
])
def test_under_defer_the_january_data_keeps_phase_a_job_names(env, monkeypatch, phase_a, january):
    a = _cell_name(env, monkeypatch, "--wf-defer-last-test", "--wf-data-end", "2025-12-31", *phase_a)
    assert _cell_name(env, monkeypatch, "--wf-defer-last-test", *january) == a


def test_without_defer_new_data_still_means_a_new_job(env, monkeypatch):
    a = _cell_name(env, monkeypatch, *_MC, "--market-condition-manifest", "aaa")
    assert _cell_name(env, monkeypatch, *_MC, "--market-condition-manifest", "bbb") != a


def test_under_defer_a_regenerated_senate_universe_keeps_the_job_names(senate, monkeypatch):
    def name(*extra):
        senate.fake.order.clear()
        senate.fake.completed.clear()
        monkeypatch.setattr(sys, "argv", _sen_argv(senate, "senate", "--wf-defer-last-test", *extra))
        assert M.main() == 0
        return sorted({n.rsplit("-", 1)[0] for n in senate.fake.order})
    a = name("--wf-data-end", "2025-12-31")
    with open(senate.uni, "a", encoding="utf-8") as fh:
        fh.write("NEW2026\n")
    assert name() == a


def test_under_defer_the_tests_get_the_current_command(env, monkeypatch):
    seen = []

    class Rec(Fake):
        def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
            seen.append(data_args)
            return super().run_oos(train_name, fold, picks, name_prefix, labels, parallel)
    rec = Rec()
    monkeypatch.setattr(M, "_make_wf_runner", lambda args: rec)
    monkeypatch.setattr(sys, "argv", _argv(env, "--wf-defer-last-test", "--exclude-symbols", "ZZ",
                                           experts=("DeterministicScorer",)))
    assert M.main() == 0
    assert len(seen) == 3 and all(_flag(a, "--exclude-symbols") == "ZZ" for a in seen)


# ------------------------------------------------------------------ SPY regime gate (goal2027 2.3)


def _plain_argv(env, *extra, experts=("DeterministicScorer",)):
    """The cell argv WITHOUT walk-forward (the flag is a matrix flag; the chain is not involved)."""
    skip = ",".join(e for e in ("FMPRating", "DeterministicScorer", "FMPEarningsDrift",
                                "FMPInsiderClusterBuy", "FactorRanker") if e not in experts)
    return ["run_screener_capband_matrix.py", "--bands", "mid", "--skip-experts", skip,
            "--strategy-plan", env.plan, "--name-suffix=-goal2027-spyreg", *extra]


def test_spy_gate_flag_reaches_classic_jobs_only(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _plain_argv(env, "--benchmark-regime-gates",
                                                 experts=("DeterministicScorer", "FactorRanker")))
    assert M.main() == 0
    by_expert = {_flag(c, "--expert"): c for c in env.calls}
    assert "--benchmark-regime-gates" in by_expert["DeterministicScorer"]
    assert "--benchmark-regime-gates" not in by_expert["FactorRanker"]   # bypass: no entry rules


def test_spy_gate_flag_is_not_given_to_sqm(env, monkeypatch):
    argv = _plain_argv(env, "--benchmark-regime-gates", "--strategies", "SQM")
    argv[argv.index("--strategy-plan"):argv.index("--strategy-plan") + 2] = []
    argv[argv.index("--bands") + 1] = "xl"
    monkeypatch.setattr(sys, "argv", argv)
    assert M.main() == 0
    assert env.calls and all("--benchmark-regime-gates" not in c for c in env.calls)


def test_spy_gate_flag_requires_its_name_token(env, monkeypatch):
    argv = [a if a != "--name-suffix=-goal2027-spyreg" else "--name-suffix=-goal2027"
            for a in _plain_argv(env, "--benchmark-regime-gates")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        M.main()


def test_spy_gate_flag_changes_the_job_name(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _plain_argv(env))
    assert M.main() == 0
    plain = _flag(env.calls[-1], "--name")
    monkeypatch.setattr(sys, "argv", _plain_argv(env, "--benchmark-regime-gates"))
    assert M.main() == 0
    assert _flag(env.calls[-1], "--name") != plain


def test_by_default_a_cell_that_cannot_pass_stops_early(env, monkeypatch):
    env.fake.oos_by_fold = (BAD, BAD, GOOD)
    monkeypatch.setattr(sys, "argv", _argv(env, experts=("DeterministicScorer",)))
    assert M.main() == 0
    assert [n[-4:] for n in env.fake.order] == ["-wf1", "-wf2"]
