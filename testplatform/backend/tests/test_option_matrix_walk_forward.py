"""``run_options_matrix.py --walk-forward``: every option job becomes a walk-forward cell run by the
stock grid's engine (``app.services.wf_chain`` + ``run_walk_forward.RealRunner``), with the option
holdout rail respected. The real runner is replaced by a fake; nothing is optimized."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, os.path.dirname(__file__))

from app.services import walk_forward as WF  # noqa: E402
from test_walk_forward import FakeRunner, _oos, _pick  # noqa: E402

_spec = importlib.util.spec_from_file_location("rom_wf", _ROOT / "tools" / "run_options_matrix.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

LAUNCHER = str(_ROOT / "testplatform" / "ba2test_launcher.py")
GOOD, BAD = _oos(5.0, -10.0), _oos(-5.0, -10.0)
SEL = {"min_trade_rows": 30, "min_return_rel_pct": 5.0, "min_dd_pts": 2.0, "min_trades_rel_pct": 5.0}
_ENV = ("BA2_HOME", "BACKTEST_OPTIONS_STORE", "BACKTEST_OPTIONS_PARQUET_ROOT",
        "BACKTEST_OPTIONS_RISK_FREE_RATE", "TASTYTRADE_OPTIONS_HISTORY_FLOOR")


class Fake(FakeRunner):
    def __init__(self, oos_by_fold=(GOOD, GOOD, GOOD)):
        super().__init__()
        self.oos_by_fold = oos_by_fold
        self.order = []
        self.data_args = []

    def run_train(self, cmd, name):
        self.order.append(name)
        super().run_train(cmd, name)

    def select(self, train_name, top_n):
        return [_pick(1), _pick(2)][:top_n]

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        self.tested.append((train_name, fold.index))
        self.data_args.append(data_args)
        return {p.rank: self.oos_by_fold[fold.index - 1] for p in picks}


@pytest.fixture
def env(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or SimpleNamespace(returncode=0))
    monkeypatch.setenv("DB_FILE", str(tmp_path / "no-such.db"))
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    fake = Fake()
    made = []
    monkeypatch.setattr(M, "_make_wf_runner", lambda args, selection: made.append(selection) or fake)
    monkeypatch.setattr(M, "_wf_selection",
                        lambda args: dict(SEL, **({} if args.wf_min_trade_rows is None
                                                  else {"min_trade_rows": args.wf_min_trade_rows})))
    return SimpleNamespace(calls=calls, fake=fake, made=made, report=str(tmp_path / "rep"))


PHASE_A = ["--wf-defer-last-test", "--wf-data-end", "2025-12-31"]
THRESH = ["--pass-min-oos-return", "0", "--pass-max-dd-mult", "2.5", "--pass-min-folds", "2"]


def _argv(env, *extra, wf=True):
    base = ["--strategies", "O_BULLCS", "--experts", "DeterministicScorer", "--estimates-source", "fmp",
            "--launcher", LAUNCHER, "--universe-file", str(_ROOT / "tools" / "options_universe_top100.txt")]
    if wf:
        base += ["--walk-forward", "--wf-report-dir", env.report, "--wf-top-n", "2", *THRESH]
    return base + list(extra)


def _args(env, *extra):
    ap = M.build_parser()
    M.add_refused_decision_times_flag(ap)
    M.add_estimates_source_flag(ap)
    M.add_option_history_source_flag(ap)
    return M.resolve_args(ap, _argv(env, *extra))


def _cell(env, *extra):
    a = _args(env, *extra)
    specs = M.wf_cell_specs(a, LAUNCHER, ["DeterministicScorer"], ["O_BULLCS"], "AAPL,MSFT",
                            M._wf_selection(a))
    assert len(specs) == 1
    return specs[0]


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# ------------------------------------------------------------------ the chain


def test_phase_a_trains_three_folds_tests_two_and_leaves_the_cell_pending(env):
    assert M.main(_argv(env, *PHASE_A)) == 0
    assert env.calls == []                                   # nothing went through subprocess
    cell = env.fake.order[0][:-4]
    assert cell.startswith("optwf-DeterministicScorer-O_BULLCS-spow0922-d")
    assert env.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3"]     # no refit
    assert env.fake.tested == [(f"{cell}-wf1", 1), (f"{cell}-wf2", 2)]
    import json
    with open(os.path.join(env.report, cell, "cell.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["verdict"] == "PENDING" and doc["deferred"]["test"] == ["2026-01-01", "2026-12-31"]
    assert doc["refit_job"] is None


def test_every_fold_command_carries_the_option_job_flags_and_its_own_window(env):
    assert M.main(_argv(env, *PHASE_A)) == 0
    cmds = dict(env.fake.trained)
    cell = env.fake.order[0][:-4]
    one, two, three = (cmds[f"{cell}-wf{k}"] for k in (1, 2, 3))
    for c, (end, gens) in zip((one, two, three), (("2023-12-31", "30"), ("2024-12-31", "15"),
                                                   ("2025-12-31", "15"))):
        assert c[:2] == [sys.executable, LAUNCHER] and c[2] == "optimize"
        assert c.count("--start") == c.count("--end") == c.count("--generations") == 1
        assert (_flag(c, "--start"), _flag(c, "--end"), _flag(c, "--generations")) == ("2020-01-01", end, gens)
        assert _flag(c, "--strategy") == "O_BULLCS" and _flag(c, "--options-store") == "thetadata"
        assert _flag(c, "--option-spread-model") == "pow-2026-09-22"
        assert _flag(c, "--run-schedule") == "daily" and _flag(c, "--interval") == "1d"
        assert _flag(c, "--estimates-source") == "fmp"
    assert "--persist-final-population" in one and "--chain-seed-from" not in one
    assert _flag(two, "--chain-seed-from") == f"{cell}-wf1"
    assert _flag(three, "--chain-seed-from") == f"{cell}-wf2"


def test_the_cell_uses_the_plain_job_command_minus_the_per_job_flags(env):
    """The cell's base args ARE build_cmd's (the plain job's) without --start/--end/--name/
    --generations: no option flag is lost or added by the walk-forward mode."""
    a = _args(env, *PHASE_A)
    spec = M.wf_cell_specs(a, LAUNCHER, ["DeterministicScorer"], ["O_BULLCS"], "AAPL,MSFT", SEL)[0]
    plain = M.build_cmd(a, LAUNCHER, "x", "DeterministicScorer", "O_BULLCS", "AAPL,MSFT")
    assert spec.optimize_args == M.wf_optimize_args(plain)
    from matrix_flags import WF_PER_JOB_FLAGS
    assert not set(spec.optimize_args) & set(WF_PER_JOB_FLAGS)
    assert len(spec.optimize_args) == len(plain) - plain.index("optimize") - 1 - 2 * len(WF_PER_JOB_FLAGS)


def test_dry_run_prints_the_chain_and_runs_nothing(env, capsys):
    assert M.main(_argv(env, *PHASE_A, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert env.calls == [] and env.fake.trained == [] and env.made == []
    assert "fold 1: train 2020-01-01..2023-12-31" in out and "test 2024-01-01..2024-12-31" in out
    assert "TEST DEFERRED" in out and "--chain-seed-from" in out
    assert out.count("TODO  optwf-") == 3


def test_the_real_selection_is_persist_distinct_topns_with_the_floor_override():
    import persist_distinct_topn as P
    default = P._parse(["--opt-id", "0"]).min_trade_rows
    assert M._wf_selection(SimpleNamespace(wf_min_trade_rows=None))["min_trade_rows"] == default == 30
    assert M._wf_selection(SimpleNamespace(wf_min_trade_rows=7))["min_trade_rows"] == 7


def test_selection_floor_reaches_the_cell_and_the_runner(env):
    assert M.main(_argv(env, *PHASE_A, "--wf-min-trade-rows", "12")) == 0
    assert env.made == [dict(SEL, min_trade_rows=12)]
    spec = _cell(env, *PHASE_A, "--wf-min-trade-rows", "12")
    assert spec.selection["min_trade_rows"] == 12


# ------------------------------------------------------------------ the holdout rail


def test_without_deferral_the_refit_into_the_holdout_is_refused_before_anything_runs(env, capsys):
    with pytest.raises(SystemExit):
        M.main(_argv(env))
    assert "refit would train 2020-01-01..2026-12-31" in capsys.readouterr().err
    assert env.fake.trained == [] and env.made == []


def test_a_fold_training_into_the_holdout_is_refused(env, capsys):
    with pytest.raises(SystemExit):
        M.main(_argv(env, "--wf-test-years", "2025,2026,2027", "--wf-defer-last-test",
                     "--wf-data-end", "2026-12-31"))
    assert "fold 3 trains to 2026-12-31" in capsys.readouterr().err


def test_the_refusal_reads_the_rail_of_the_launcher_the_jobs_run(env, monkeypatch):
    """Phase B: once _OPTION_HOLDOUT_START has moved (a reviewed change), the same command without
    --wf-data-end tests the 2026 fold and refits."""
    monkeypatch.setattr(M._launcher_module(LAUNCHER), "_OPTION_HOLDOUT_START", date(2027, 1, 1))
    assert M.main(_argv(env, "--wf-defer-last-test")) == 0
    cell = env.fake.order[0][:-4]
    assert env.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3", f"{cell}-refit"]
    refit = dict(env.fake.trained)[f"{cell}-refit"]
    assert (_flag(refit, "--start"), _flag(refit, "--end")) == ("2020-01-01", "2026-12-31")
    assert _flag(refit, "--chain-seed-from") == f"{cell}-wf3"


def test_holdout_refusal_function():
    folds = WF.build_year_folds(date(2020, 1, 1), [2024, 2025, 2026])
    h = date(2026, 1, 1)
    assert M.wf_holdout_refusal(folds, folds[-1], h) is None              # deferred: no refit now
    assert "refit" in M.wf_holdout_refusal(folds, None, h)
    assert M.wf_holdout_refusal(folds[:2], None, h) is None               # tests 2024, 2025 only


# ------------------------------------------------------------------ flags


@pytest.mark.parametrize("drop", ["--wf-top-n", "--wf-report-dir", "--pass-min-oos-return",
                                  "--pass-max-dd-mult", "--pass-min-folds"])
def test_required_flags_have_no_hidden_defaults(env, drop, capsys):
    argv = _argv(env, *PHASE_A)
    i = argv.index(drop)
    del argv[i:i + 2]
    with pytest.raises(SystemExit):
        M.main(argv)
    assert drop in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--start", "2021-01-01"], ["--end", "2024-12-31"],
                                  ["--generations", "10"], ["--start", "2020-01-01"],    # = the default
                                  ["--end=2025-12-31"]])
def test_an_explicit_job_window_or_budget_is_refused(env, flag, capsys):
    with pytest.raises(SystemExit):
        M.main(_argv(env, *PHASE_A, *flag))
    assert f"{flag[0].split('=')[0]} cannot be given" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--wf-top-n", "2"], ["--wf-defer-last-test"], ["--pass-require-fold", "2025"],
                                  ["--wf-min-trade-rows", "5"], ["--pass-max-oos-dd", "30"]])
def test_walk_forward_flags_without_walk_forward_are_refused(env, flag, capsys):
    with pytest.raises(SystemExit):
        M.main(_argv(env, *flag, wf=False))
    assert "need --walk-forward" in capsys.readouterr().err


def test_plain_mode_defaults_are_unchanged(env):
    ap = M.build_parser()
    M.add_estimates_source_flag(ap)
    a = M.resolve_args(ap, ["--estimates-source", "fmp"])
    assert (a.start, a.end, a.walk_forward) == ("2020-01-01", "2025-12-31", False)
    with pytest.raises(SystemExit):
        M.resolve_args(ap, ["--end", "2026-01-31"])          # the plain holdout check still holds


# ------------------------------------------------------------------ cell names


def test_cell_names_never_collide_with_plain_jobs(env):
    spec = _cell(env, *PHASE_A)
    assert spec.name.startswith("optwf-") and not spec.name.startswith("optm-")


@pytest.mark.parametrize("change", [["--wf-top-n", "3"], ["--wf-first-generations", "20"],
                                    ["--wf-test-years", "2023,2024,2025,2026"], ["--seed", "7"],
                                    ["--pass-min-folds", "1"], ["--initial-capital", "10000"]])
def test_a_changed_spec_or_search_changes_the_cell_name(env, change):
    base = _cell(env, *PHASE_A).name
    assert _cell(env, *PHASE_A, *change).name != base       # argparse: the later value wins


def test_labels_and_the_selection_floor_do_not_move_the_name(env):
    base = _cell(env, *PHASE_A).name
    assert _cell(env, *PHASE_A, "--labels", "goal-x").name == base
    assert _cell(env, *PHASE_A, "--wf-min-trade-rows", "5").name == base
    assert _cell(env, *PHASE_A, "--pass-max-oos-dd", "35").name == base


def test_phase_a_and_phase_b_share_every_job_name(env, monkeypatch):
    a_name = _cell(env, *PHASE_A).name
    monkeypatch.setattr(M._launcher_module(LAUNCHER), "_OPTION_HOLDOUT_START", date(2027, 1, 1))
    assert _cell(env, "--wf-defer-last-test").name == a_name


MC = ["--market-condition-profile", "ohlcv-v1,ta-structure-v1"]


def test_under_defer_new_manifests_and_data_locations_keep_the_names(env, monkeypatch):
    phase_a = _cell(env, *PHASE_A, *MC, "--market-condition-manifest", "ohlcv-v1=aaaa,ta-structure-v1=bbbb").name
    monkeypatch.setenv("BA2_HOME", "/elsewhere")
    january = _cell(env, *PHASE_A, *MC, "--market-condition-manifest", "ohlcv-v1=cccc,ta-structure-v1=dddd").name
    assert january == phase_a


def test_without_defer_new_manifests_mean_a_new_cell(env):
    a = ["--wf-test-years", "2024,2025"]
    one = _cell(env, *a, *MC, "--market-condition-manifest", "ohlcv-v1=aaaa,ta-structure-v1=bbbb").name
    two = _cell(env, *a, *MC, "--market-condition-manifest", "ohlcv-v1=cccc,ta-structure-v1=dddd").name
    assert one != two


def test_under_defer_a_new_gate_store_is_a_new_cell(env):
    """The test re-run does NOT rebase the gate store (it would read the train job's), so its path
    stays in the name even under --wf-defer-last-test."""
    one = _cell(env, *PHASE_A, "--screener-gate-store", "STORE-A", "--max-stock-price", "0").name
    two = _cell(env, *PHASE_A, "--screener-gate-store", "STORE-B", "--max-stock-price", "0").name
    assert one != two


def test_under_defer_the_tests_read_the_current_command(env):
    assert M.main(_argv(env, *PHASE_A)) == 0
    assert env.fake.data_args and all(d is not None and "--universe" in d for d in env.fake.data_args)


def test_plain_job_names_are_unchanged_by_the_refactor(env):
    """planned_jobs (plain mode) still yields optm- names; discovery names still digest."""
    ap = M.build_parser()
    M.add_estimates_source_flag(ap)
    a = M.resolve_args(ap, ["--profile", "discovery", "--screener-gate-store", "S", "--max-stock-price", "0",
                            "--strategies", "O_IC", "--experts", "FMPRating",
                            "--neutral-entry-modes", "hold,low_confidence"])
    names = [n for n, *_ in M.planned_jobs(a, "ba2-test", ["FMPRating"], ["O_IC"], "AAPL")]
    assert len(names) == 2
    assert names[0].startswith("optm-FMPRating-O_IC-spow0922-hold-rules1-d")
    assert names[1].startswith("optm-FMPRating-O_IC-spow0922-low_confidence-rules1-d")
