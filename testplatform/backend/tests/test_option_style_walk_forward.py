"""``tools/run_option_style_wf.py`` (``tools/option_style_job.sh --walk-forward``): an option STYLE job
becomes a walk-forward cell run by the stock grid's engine (``app.services.wf_chain`` +
``run_walk_forward.RealRunner``), UNSEEDED, with the option holdout rail respected. The real runner is
replaced by a fake; nothing is optimized."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, os.path.dirname(__file__))

from test_walk_forward import FakeRunner, _oos, _pick  # noqa: E402

_spec = importlib.util.spec_from_file_location("style_wf", _ROOT / "tools" / "run_option_style_wf.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

LAUNCHER = str(_ROOT / "testplatform" / "ba2test_launcher.py")
STYLES = str(_ROOT / "tools" / "option_styles.stage2.json")
UFILE = str(_ROOT / "tools" / "options_universe_stage2.txt")
GOOD = _oos(5.0, -10.0)
SEL = {"min_trade_rows": 30, "min_return_rel_pct": 5.0, "min_dd_pts": 2.0, "min_trades_rel_pct": 5.0}
_ENV = ("BA2_HOME", "BACKTEST_OPTIONS_STORE", "BACKTEST_OPTIONS_PARQUET_ROOT",
        "BACKTEST_OPTIONS_RISK_FREE_RATE", "TASTYTRADE_OPTIONS_HISTORY_FLOOR")
PLAIN_NAME = "optm-DeterministicScorer-O_STYLE-long10k-s3L10"


def recipe(bundle=None, style_config=STYLES, ufile=UFILE, manifests="ohlcv-v1=aaaa,ta-structure-v1=bbbb",
           store="/h/common/cache/screener/metric_store_alpaca_20261022",
           panel="/h/common/cache/screener/daily_panel/03d21a4a2f176c69", worker="remote227",
           screen=("--screener-cap-band", "large"), name=PLAIN_NAME, strategy="O_STYLE"):
    """The command tools/option_style_job.sh execs for a plain job (its exec line; pinned against the
    script itself by test_the_recipe_copy_matches_the_style_job_script when bash is available)."""
    cmd = [sys.executable, LAUNCHER, "optimize", "--expert", "DeterministicScorer",
           "--universe", "AAPL,MSFT", "--strategy", strategy,
           "--style-config", style_config, "--style", "long10k",
           "--start", "2020-01-02", "--end", "2025-12-31", "--interval", "1d",
           "--population", "200", "--generations", "30", "--initial-capital", "20000.0", "--run-schedule", "daily",
           "--name", name, "--parallel", "0", "--worker", worker, "--seed", "42", "--elitism-percent", "10.0",
           "--options-store", "thetadata", "--option-spread-model", "pow-2026-09-22",
           "--fitness", "option_car_target_soft30", "--early-stop", "5", "--early-stop-min-rel", "0.01",
           "--mutation-prob", "0.3", "--ohlcv-history-source", "alpaca", "--profit-cap-pct", "2000.0",
           "--profit-share-cap-pct", "25.0", "--option-size-within-fill-volume", "--equity-cap", "20000",
           "--benchmark-regime-gates", "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
           "--market-condition-manifest", manifests, "--screener", "--screener-store", store,
           "--screener-panel", panel, *screen, "--screener-universe-file", ufile, "--screener-cadence-days", "7"]
    return cmd + (["--seed-population", bundle] if bundle else [])


PHASE_A = ["--wf-defer-last-test", "--wf-data-end", "2025-12-31"]
THRESH = ["--pass-min-oos-return", "0", "--pass-max-dd-mult", "2.5", "--pass-min-folds", "2"]
L10 = ["--fitness", "option_long_dd40_50", "--estimates-source", "fmp", "--initial-capital", "10000",
       "--equity-cap", "10000"]


class Fake(FakeRunner):
    def __init__(self, fail_train=()):
        super().__init__()
        self.order, self.data_args, self.fail_train = [], [], set(fail_train)

    def run_train(self, cmd, name):
        self.order.append(name)
        if name in self.fail_train:
            self.fail_train.discard(name)
            raise S.ROM._wf_import()[0].WalkForwardError(f"training job {name!r} exited with code -9")
        super().run_train(cmd, name)

    def select(self, train_name, top_n):
        return [_pick(1), _pick(2)][:top_n]

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        self.tested.append((train_name, fold.index))
        self.data_args.append(data_args)
        return {p.rank: GOOD for p in picks}


@pytest.fixture
def env(monkeypatch, tmp_path):
    calls = []
    fred_rc = SimpleNamespace(rc=0)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)) or SimpleNamespace(
        returncode=fred_rc.rc))
    monkeypatch.setenv("DB_FILE", str(tmp_path / "no-such.db"))
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    box = SimpleNamespace(fake=Fake(), made=[])
    monkeypatch.setattr(S, "_make_wf_runner", lambda args, selection: box.made.append(selection) or box.fake)
    monkeypatch.setattr(S, "_wf_selection", lambda args: dict(SEL, **({} if args.wf_min_trade_rows is None
                                                                      else {"min_trade_rows": args.wf_min_trade_rows})))
    box.calls, box.fred, box.report = calls, fred_rc, str(tmp_path / "rep")
    return box


def _argv(env, *extra, cmd=None, wf=True):
    head = ["--walk-forward", "--wf-report-dir", env.report, "--wf-top-n", "2", *THRESH] if wf else []
    return [*L10, *head, *extra, "--", *(cmd or recipe())]


def _resolve(env, *extra, cmd=None):
    return S.resolve(_argv(env, *extra, cmd=cmd))


def _cell(env, *extra, cmd=None):
    args, full = _resolve(env, *extra, cmd=cmd)
    return S.cell_spec(args, full, SEL)


def _flag(cmd, flag):
    """The LAST value of ``flag`` (what argparse reads)."""
    idx = [i for i, t in enumerate(cmd) if t == flag]
    return cmd[idx[-1] + 1] if idx else None


def _cells_json(env, cell):
    with open(os.path.join(env.report, cell, "cell.json"), encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ the chain


def test_phase_a_trains_three_folds_tests_two_and_leaves_the_cell_pending(env):
    assert S.main(_argv(env, *PHASE_A)) == 0
    cell = env.fake.order[0][:-4]
    assert cell.startswith("optwf-DeterministicScorer-O_STYLE-long10k-s3L10-d")
    assert env.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3"]          # no refit
    assert env.fake.tested == [(f"{cell}-wf1", 1), (f"{cell}-wf2", 2)]
    doc = _cells_json(env, cell)
    assert doc["verdict"] == "PENDING" and doc["deferred"]["test"] == ["2026-01-01", "2026-12-31"]
    assert doc["strategy"] == "O_STYLE" and doc["band"] == "style/long10k" and doc["expert"] == "DeterministicScorer"
    # the risk-free rate is checked for the whole span the cell reads, before anything trains
    assert env.calls == [[sys.executable, str(_ROOT / "tools" / "refresh_fred_cache.py"), "--check-rate-window",
                          "2020-01-01", "2025-12-31"]]


def test_every_fold_command_carries_the_whole_style_recipe_and_its_own_window(env):
    assert S.main(_argv(env, *PHASE_A)) == 0
    cmds = dict(env.fake.trained)
    cell = env.fake.order[0][:-4]
    one, two, three = (cmds[f"{cell}-wf{k}"] for k in (1, 2, 3))
    for c, (end, gens) in zip((one, two, three), (("2023-12-31", "30"), ("2024-12-31", "15"),
                                                   ("2025-12-31", "15"))):
        assert c[:3] == [sys.executable, LAUNCHER, "optimize"]
        assert c.count("--start") == c.count("--end") == c.count("--generations") == c.count("--name") == 1
        assert (_flag(c, "--start"), _flag(c, "--end"), _flag(c, "--generations")) == ("2020-01-01", end, gens)
        # the style, the screen with its restriction, market conditions, SPY gates, sizing and the store
        assert (_flag(c, "--strategy"), _flag(c, "--style"), _flag(c, "--style-config")) == ("O_STYLE", "long10k", STYLES)
        assert _flag(c, "--market-condition-profile") == "ohlcv-v1,ta-structure-v1"
        assert _flag(c, "--market-condition-manifest") == "ohlcv-v1=aaaa,ta-structure-v1=bbbb"
        assert "--screener" in c and _flag(c, "--screener-cap-band") == "large"
        assert _flag(c, "--screener-universe-file") == UFILE and _flag(c, "--screener-cadence-days") == "7"
        assert _flag(c, "--screener-panel").endswith("03d21a4a2f176c69")
        assert "--benchmark-regime-gates" in c and "--option-size-within-fill-volume" in c
        assert _flag(c, "--options-store") == "thetadata" and _flag(c, "--option-spread-model") == "pow-2026-09-22"
        # the job's overrides win exactly as in the plain job (argparse: last value)
        assert (_flag(c, "--fitness"), _flag(c, "--equity-cap"), _flag(c, "--initial-capital")) == \
            ("option_long_dd40_50", "10000", "10000")
        assert _flag(c, "--estimates-source") == "fmp" and _flag(c, "--worker") == "remote227"
        assert "--seed-population" not in c                            # UNSEEDED
    assert "--persist-final-population" in one and "--chain-seed-from" not in one   # fold 1: cold start
    assert _flag(two, "--chain-seed-from") == f"{cell}-wf1"
    assert _flag(three, "--chain-seed-from") == f"{cell}-wf2"


def test_the_cell_uses_the_plain_job_command_minus_the_per_job_flags(env):
    spec = _cell(env, *PHASE_A)
    from matrix_flags import WF_PER_JOB_FLAGS, wf_optimize_args
    plain = recipe() + L10
    assert spec.optimize_args == wf_optimize_args(plain)
    assert not set(spec.optimize_args) & set(WF_PER_JOB_FLAGS)
    assert spec.launcher == [sys.executable, LAUNCHER]


def test_under_defer_the_tests_read_the_current_commands_data(env):
    assert S.main(_argv(env, *PHASE_A)) == 0
    for d in env.fake.data_args:
        assert d is not None and _flag(d, "--screener-panel").endswith("03d21a4a2f176c69")
        assert _flag(d, "--market-condition-manifest") == "ohlcv-v1=aaaa,ta-structure-v1=bbbb"


def test_dry_run_prints_the_chain_and_runs_nothing(env, capsys):
    assert S.main(_argv(env, *PHASE_A, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert env.calls == [] and env.fake.trained == [] and env.made == []
    assert "fold 1: train 2020-01-01..2023-12-31" in out and "TEST DEFERRED" in out
    assert out.count("TODO  optwf-DeterministicScorer-O_STYLE-long10k-s3L10-d") == 3
    assert "unseeded" in out


def test_a_missing_risk_free_rate_stops_before_anything_trains(env):
    env.fred.rc = 1
    with pytest.raises(SystemExit, match="DGS3MO"):
        S.main(_argv(env, *PHASE_A))
    assert env.made == [] and env.fake.trained == []


def test_selection_floor_reaches_the_cell_and_the_runner(env):
    assert S.main(_argv(env, *PHASE_A, "--wf-min-trade-rows", "12")) == 0
    assert env.made == [dict(SEL, min_trade_rows=12)]
    assert env.fake.trained                                            # the floor is not part of the name


def test_default_floor_is_persist_distinct_topns_30():
    import persist_distinct_topn as P
    assert S.ROM._wf_selection(SimpleNamespace(wf_min_trade_rows=None))["min_trade_rows"] == \
        P._parse(["--opt-id", "0"]).min_trade_rows == 30


# ------------------------------------------------------------------ resume


def test_a_rerun_reuses_the_completed_folds_and_trains_only_what_is_missing(env):
    """A driver killed in fold 2's training (cell ERROR): the same command again reuses fold 1's stored
    report (nothing re-trained or re-tested there) and continues with fold 2. (The killed fold-2 ROW is
    continued by RealRunner.run_train with optimize --resume-interrupted: the engine's, tested with the
    real runner in the resume suites.)"""
    cell = S.style_cell_name(*_resolve(env, *PHASE_A))
    env.fake = first = Fake(fail_train={f"{cell}-wf2"})
    assert S.main(_argv(env, *PHASE_A)) == 1                            # cell ERROR, reported
    assert first.tested == [(f"{cell}-wf1", 1)]
    env.fake = second = Fake()
    second.completed = set(first.completed)                             # wf1 is completed in the DB
    assert S.main(_argv(env, *PHASE_A)) == 0
    assert second.order == [f"{cell}-wf2", f"{cell}-wf3"]               # wf1 not trained again
    assert second.tested == [(f"{cell}-wf2", 2)]                        # wf1 not tested again
    assert _cells_json(env, cell)["verdict"] == "PENDING"


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("cmd,extra", [
    (recipe(bundle="/grid/bundle_jobL10.json"), []),                    # the style job's own bundle
    (recipe(), ["--seed-population", "b.json"]),
    (recipe(), ["--seed-genomes", "live.json"]),
    (recipe(), ["--seed-check-only"]),
    (recipe(), ["--warm-start-from", "12"]),
    (recipe(), ["--chain-seed-from", "optm-DeterministicScorer-O_STYLE-long10k-s3L10"]),
])
def test_any_external_seed_is_refused_with_the_owner_reason(env, capsys, cmd, extra):
    with pytest.raises(SystemExit):
        S.main(_argv(env, *PHASE_A, *extra, cmd=cmd))
    err = capsys.readouterr().err
    assert "UNSEEDED" in err and "look-ahead" in err and "none" in err
    assert env.calls == [] and env.made == []


@pytest.mark.parametrize("flag", [["--generations", "10"], ["--start", "2021-01-01"], ["--end=2024-12-31"],
                                  ["--name", "x"]])
def test_an_explicit_job_window_budget_or_name_is_refused(env, capsys, flag):
    with pytest.raises(SystemExit):
        S.main(_argv(env, *PHASE_A, *flag))
    assert f"{flag[0].split('=')[0]} cannot be given" in capsys.readouterr().err


@pytest.mark.parametrize("cmd,extra,why", [
    (recipe(screen=("--screen-preset", "ndx30")), [], "--screen-preset"),
    (recipe(), ["--print-universe"], "--print-universe"),
    (recipe(strategy="O_BULLCS"), [], "O_STYLE style jobs"),
    (recipe(name="mine-x"), [], "must start with 'optm-'"),
    (recipe(), ["--wf-tpo-n", "3"], "unknown walk-forward flag"),
])
def test_other_refusals_name_their_reason(env, capsys, cmd, extra, why):
    with pytest.raises(SystemExit):
        S.main(_argv(env, *PHASE_A, *extra, cmd=cmd))
    assert why in capsys.readouterr().err
    assert env.made == []


def test_without_walk_forward_the_driver_refuses(env, capsys):
    with pytest.raises(SystemExit):
        S.main(_argv(env, wf=False))
    assert "walk-forward cells only" in capsys.readouterr().err


@pytest.mark.parametrize("drop", ["--wf-top-n", "--wf-report-dir", "--pass-min-oos-return",
                                  "--pass-max-dd-mult", "--pass-min-folds"])
def test_required_flags_have_no_hidden_defaults(env, drop, capsys):
    argv = _argv(env, *PHASE_A)
    i = argv.index(drop)
    del argv[i:i + 2]
    with pytest.raises(SystemExit):
        S.main(argv)
    assert drop in capsys.readouterr().err


def test_an_engine_forbidden_flag_is_refused_before_anything_runs(env, capsys):
    with pytest.raises(SystemExit, match="--submit"):
        S.main(_argv(env, *PHASE_A, "--submit"))
    assert env.made == []


# ------------------------------------------------------------------ the holdout rail


def test_without_deferral_the_refit_into_the_holdout_is_refused(env):
    with pytest.raises(SystemExit, match="refit would train 2020-01-01..2026-12-31"):
        S.main(_argv(env))
    assert env.fake.trained == [] and env.made == []


def test_phase_b_tests_2026_and_refits_once_the_rail_has_moved(env, monkeypatch):
    monkeypatch.setattr(S.ROM._launcher_module(LAUNCHER), "_OPTION_HOLDOUT_START", date(2027, 1, 1))
    assert S.main(_argv(env, "--wf-defer-last-test")) == 0
    cell = env.fake.order[0][:-4]
    assert env.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3", f"{cell}-refit"]
    refit = dict(env.fake.trained)[f"{cell}-refit"]
    assert (_flag(refit, "--start"), _flag(refit, "--end")) == ("2020-01-01", "2026-12-31")
    assert _flag(refit, "--chain-seed-from") == f"{cell}-wf3" and _flag(refit, "--style") == "long10k"
    assert env.calls[0][-2:] == ["2020-01-01", "2026-12-31"]           # the 2026 rate is checked too


# ------------------------------------------------------------------ names


def test_the_cell_name_is_the_plain_name_with_optwf_and_a_digest(env):
    name = _cell(env, *PHASE_A).name
    assert name.startswith("optwf-DeterministicScorer-O_STYLE-long10k-s3L10-d") and not name.startswith("optm-")
    assert len(name.rsplit("-d", 1)[1]) == 12
    assert _cell(env, *PHASE_A).name == name                            # deterministic


def test_the_vendor_tag_of_the_plain_name_is_kept(env):
    av = recipe(name=PLAIN_NAME + "-optalphavantage")
    assert _cell(env, *PHASE_A, cmd=av).name.startswith(
        "optwf-DeterministicScorer-O_STYLE-long10k-s3L10-optalphavantage-d")


@pytest.mark.parametrize("change", [["--wf-top-n", "3"], ["--wf-first-generations", "20"],
                                    ["--seed", "7"], ["--pass-min-folds", "1"], ["--fitness", "option_long"],
                                    ["--wf-test-years", "2023,2024,2025,2026"]])
def test_a_changed_spec_or_search_changes_the_name(env, change):
    assert _cell(env, *PHASE_A, *change).name != _cell(env, *PHASE_A).name


def test_where_it_runs_and_its_labels_do_not_move_the_name(env):
    base = _cell(env, *PHASE_A).name
    assert _cell(env, *PHASE_A, cmd=recipe(worker="other")).name == base
    assert _cell(env, *PHASE_A, "--labels", "x", "--parallel", "4").name == base
    assert _cell(env, *PHASE_A, "--wf-min-trade-rows", "5", "--pass-max-oos-dd", "35").name == base


def test_the_style_is_identified_by_its_content_not_its_path(env, tmp_path):
    base = _cell(env, *PHASE_A).name
    moved = tmp_path / "styles.json"
    shutil.copyfile(STYLES, moved)
    assert _cell(env, *PHASE_A, cmd=recipe(style_config=str(moved))).name == base
    doc = json.loads(moved.read_text(encoding="utf-8"))
    doc["styles"]["long10k"]["note"] += " (edited)"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(doc), encoding="utf-8")
    assert _cell(env, *PHASE_A, cmd=recipe(style_config=str(edited))).name != base


def test_without_defer_the_universe_file_counts_by_content(env, tmp_path):
    a = ["--wf-test-years", "2024,2025"]
    base = _cell(env, *a).name
    same = tmp_path / "u.txt"
    shutil.copyfile(UFILE, same)
    assert _cell(env, *a, cmd=recipe(ufile=str(same))).name == base
    other = tmp_path / "v.txt"
    other.write_text("AAPL\nMSFT\n", encoding="utf-8")
    assert _cell(env, *a, cmd=recipe(ufile=str(other))).name != base


def test_phase_a_and_phase_b_share_the_name_on_new_data_and_locations(env, monkeypatch, tmp_path):
    a = _cell(env, *PHASE_A).name
    monkeypatch.setattr(S.ROM._launcher_module(LAUNCHER), "_OPTION_HOLDOUT_START", date(2027, 1, 1))
    monkeypatch.setenv("BA2_HOME", "/elsewhere")
    january = recipe(manifests="ohlcv-v1=cccc,ta-structure-v1=dddd", store="/e/metric_store_2027",
                     panel="/e/daily_panel/ffff", ufile=str(tmp_path / "u2.txt"))
    (tmp_path / "u2.txt").write_text("AAPL\n", encoding="utf-8")
    assert _cell(env, "--wf-defer-last-test", cmd=january).name == a


def test_without_defer_new_manifests_mean_a_new_cell(env):
    a = ["--wf-test-years", "2024,2025"]
    assert _cell(env, *a).name != _cell(env, *a, cmd=recipe(manifests="ohlcv-v1=cccc,ta-structure-v1=dddd")).name


# ------------------------------------------------------------------ abbreviations (review 2026-11-15, item 1)


def _real_parser():
    return S.ROM._launcher_module(LAUNCHER)._optimize_parser()


#: (refused flag, a value for it or None for a switch, what the refusal says)
_REFUSED = [("--seed-population", "b.json", "UNSEEDED"), ("--seed-genomes", "g.json", "UNSEEDED"),
            ("--seed-check-only", None, "UNSEEDED"), ("--warm-start-from", "3", "UNSEEDED"),
            ("--chain-seed-from", "x", "UNSEEDED"), ("--start", "2021-01-01", "cannot be given"),
            ("--end", "2024-12-31", "cannot be given"), ("--generations", "5", "cannot be given"),
            ("--name", "foo", "cannot be given"), ("--screen-preset", "xl", "--screen-preset"),
            ("--print-universe", None, "--print-universe"), ("--strategy", "O_BULLCS", "O_STYLE style jobs")]


def _abbreviations(flag):
    return [flag[:n] for n in range(3, len(flag) + 1)]


@pytest.mark.parametrize("flag,value,why", _REFUSED)
def test_every_abbreviation_of_a_refused_flag_is_refused_before_training(env, capsys, flag, value, why):
    parser = _real_parser()
    resolved = 0
    for abbr in _abbreviations(flag):
        if abbr == "--seed":
            continue                                                    # the RNG seed (exact match)
        hits = [s for s in parser._option_string_actions if s.startswith(abbr)]
        unique = abbr in parser._option_string_actions or len(hits) == 1
        resolved += unique
        for toks in ([abbr] if value is None else [abbr, value],
                     [f"{abbr}={value if value is not None else '1'}"]):
            with pytest.raises(SystemExit):
                S.main(_argv(env, *PHASE_A, *toks))
            err = capsys.readouterr().err
            # a prefix argparse resolves is refused for ITS reason (a value given to a switch as such);
            # an ambiguous prefix, which argparse refuses too, is refused as ambiguous
            if not unique:
                assert "ambiguous" in err, (toks, err)
            elif value is None and "=" in toks[0]:
                assert "takes no value" in err, (toks, err)
            else:
                assert why in err, (toks, err)
    assert resolved >= 1
    assert env.made == [] and env.fake.trained == [] and env.calls == []


def test_the_real_parser_reads_those_abbreviations_as_the_refused_flags():
    """The evidence that the refusals above are needed: the launcher accepts the abbreviations."""
    p = _real_parser()
    base = ["--expert", "DeterministicScorer", "--universe", "AAPL", "--start", "2020-01-01", "--end", "2020-12-31"]
    assert p.parse_args(base + ["--seed-pop", "b.json"]).seed_population == "b.json"
    assert p.parse_args(base + ["--nam", "foo"]).name == "foo"
    assert p.parse_args(base + ["--gener", "5"]).generations == 5
    assert p.parse_args(base + ["--warm-start", "3"]).warm_start_from == 3


def test_valid_abbreviations_and_the_rng_seed_pass_and_are_spelled_out(env):
    assert S.main(_argv(env, *PHASE_A, "--mutation-p", "0.2", "--seed=7", "--equity-c=9000")) == 0
    c = dict(env.fake.trained)[env.fake.order[0]]
    assert (_flag(c, "--mutation-prob"), _flag(c, "--seed"), _flag(c, "--equity-cap")) == ("0.2", "7", "9000")
    assert not any(t.startswith(("--seed=", "--equity-cap=")) or t in ("--mutation-p", "--equity-c") for t in c)


def test_an_unknown_optimize_option_is_refused_by_name(env, capsys):
    with pytest.raises(SystemExit):
        S.main(_argv(env, *PHASE_A, "--no-such-option", "1"))
    assert "unknown" in capsys.readouterr().err and env.made == []


# ------------------------------------------------------------------ data flags read like argparse (item 2)


def test_an_override_of_the_panel_reaches_the_deferred_test_whatever_its_spelling(env):
    L = S.ROM._launcher_module(LAUNCHER)
    for override in (["--screener-panel", "/h/daily_panel/P2"], ["--screener-panel=/h/daily_panel/P2"],
                     ["--screener-pan", "/h/daily_panel/P2"]):
        env.fake = Fake()
        assert S.main(_argv(env, *PHASE_A, *override)) == 0
        for d in env.fake.data_args:
            assert L._data_rebase_from_optimize_args(d)["screener_panel"] == "/h/daily_panel/P2"
        c = dict(env.fake.trained)[env.fake.order[0]]
        assert _flag(c, "--screener-panel") == "/h/daily_panel/P2"          # trained on the same panel


def test_under_defer_the_panel_is_masked_in_every_spelling(env):
    base = _cell(env, *PHASE_A).name
    assert _cell(env, *PHASE_A, "--screener-panel=/e/daily_panel/ffff").name == base
    assert _cell(env, *PHASE_A, "--screener-pan", "/e/daily_panel/ffff").name == base


# ------------------------------------------------------------------ data identity across runs (item 3)


def test_a_cell_records_its_data_identity_on_the_first_run(env):
    assert S.main(_argv(env, *PHASE_A)) == 0
    cell = env.fake.order[0][:-4]
    with open(os.path.join(env.report, cell, "data_identity.json"), encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec["strict"]["universe_file_sha256"].startswith("sha256:")
    assert rec["first"]["market_condition_manifest"] == "ohlcv-v1=aaaa,ta-structure-v1=bbbb"
    assert _cells_json(env, cell)["data_identity"]["strict"] == rec["strict"]


def test_a_dry_run_writes_no_identity_record(env):
    assert S.main(_argv(env, *PHASE_A, "--dry-run")) == 0
    assert not os.path.exists(env.report)


def test_same_name_with_another_universe_file_is_refused_loudly(env, tmp_path):
    """Under defer the universe file is masked in the name: a second cell with another universe gets
    the SAME name, and without the record it would reuse the first cell's stored folds."""
    u1, u2 = tmp_path / "u1.txt", tmp_path / "u2.txt"
    u1.write_text("AAPL\nMSFT\n", encoding="utf-8")
    u2.write_text("AAPL\nNVDA\n", encoding="utf-8")
    assert S.main(_argv(env, *PHASE_A, cmd=recipe(ufile=str(u1)))) == 0
    assert _cell(env, *PHASE_A, cmd=recipe(ufile=str(u2))).name == _cell(env, *PHASE_A, cmd=recipe(ufile=str(u1))).name
    env.fake = Fake()
    with pytest.raises(SystemExit, match="REFUSED: the data that defines this cell changed.*universe_file_sha256"):
        S.main(_argv(env, *PHASE_A, cmd=recipe(ufile=str(u2))))
    assert env.fake.trained == [] and env.fake.tested == []


def test_phase_b_on_extended_data_warns_and_keeps_both_fingerprints(env, monkeypatch, capsys):
    assert S.main(_argv(env, *PHASE_A)) == 0
    cell = env.fake.order[0][:-4]
    monkeypatch.setattr(S.ROM._launcher_module(LAUNCHER), "_OPTION_HOLDOUT_START", date(2027, 1, 1))
    env.fake = Fake()
    env.fake.completed = {f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3"}
    january = recipe(manifests="ohlcv-v1=cccc,ta-structure-v1=dddd", panel="/h/daily_panel/2027aaaa",
                     store="/h/metric_store_2027")
    capsys.readouterr()
    assert S.main(_argv(env, "--wf-defer-last-test", cmd=january)) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "market_condition_manifest" in out and "screener_panel" in out
    rec = _cells_json(env, cell)["data_identity"]
    assert rec["first"]["market_condition_manifest"] == "ohlcv-v1=aaaa,ta-structure-v1=bbbb"
    assert rec["latest"]["market_condition_manifest"] == "ohlcv-v1=cccc,ta-structure-v1=dddd"
    assert rec["first"]["screener_panel"]["name"] == "03d21a4a2f176c69"
    assert rec["latest"]["screener_panel"]["name"] == "2027aaaa" and len(rec["changes"]) == 1


def test_the_matrix_walk_forward_has_the_same_guard(env, monkeypatch, tmp_path):
    rom = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
        "rom_guard", _ROOT / "tools" / "run_options_matrix.py"))
    importlib.util.spec_from_file_location("rom_guard", _ROOT / "tools" / "run_options_matrix.py").loader.exec_module(rom)
    fake = Fake()
    monkeypatch.setattr(rom, "_make_wf_runner", lambda args, selection: fake)
    monkeypatch.setattr(rom, "_wf_selection", lambda args: dict(SEL))
    u1, u2 = tmp_path / "u1.txt", tmp_path / "u2.txt"
    u1.write_text("AAPL MSFT", encoding="utf-8")
    u2.write_text("AAPL NVDA", encoding="utf-8")
    argv = ["--strategies", "O_BULLCS", "--experts", "DeterministicScorer", "--estimates-source", "fmp",
            "--launcher", LAUNCHER, "--walk-forward", "--wf-report-dir", env.report, "--wf-top-n", "2", *THRESH,
            *PHASE_A]
    assert rom.main(argv + ["--universe-file", str(u1)]) == 0
    with pytest.raises(SystemExit, match="universe_sha256"):
        rom.main(argv + ["--universe-file", str(u2)])


# ------------------------------------------------------------------ merge dependency preflight (item 5)


def test_a_style_missing_from_the_styles_file_is_refused_with_the_merge_order(env, tmp_path, capsys):
    cmd = recipe()
    cmd[cmd.index("--style") + 1] = "premium_stop"
    with pytest.raises(SystemExit, match="premium_stop.*not defined.*feat/premium-mandatory-loss-exit"):
        S.main(_argv(env, *PHASE_A, cmd=cmd))
    assert env.made == [] and env.calls == []


def test_a_missing_universe_file_is_refused_with_the_merge_order(env, tmp_path):
    with pytest.raises(SystemExit, match="does not exist.*feat/premium-mandatory-loss-exit"):
        S.main(_argv(env, *PHASE_A, cmd=recipe(ufile=str(tmp_path / "options_universe_nasdaq.txt"))))
    assert env.made == []


def test_the_ds_cell_carries_the_option_fixed_settings_and_nothing_forces_fmprating(env):
    spec = _cell(env, *PHASE_A)
    assert spec.expert == "DeterministicScorer" and _flag(spec.optimize_args, "--expert") == "DeterministicScorer"
    assert S.ROM.option_fixed_settings(LAUNCHER, "DeterministicScorer", "O_STYLE")       # folded into the digest


def test_identity_config_reads_switches_followed_by_flags():
    cfg = S.identity_config(["--screener", "--screener-store", "S", "--benchmark-regime-gates", "--x=1",
                             "--fitness", "a", "--fitness", "b", "--worker", "w", "--last"])
    assert cfg == {"--screener": True, "--screener-store": "S", "--benchmark-regime-gates": True, "--x": "1",
                   "--fitness": "b", "--last": True}


# ------------------------------------------------------------------ the shell side


def _git_bash():
    """Git's bash (never WSL's System32 bash, which reads Windows paths differently)."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    for root in (list(Path(git).parents)[:3] if git else []):
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.exists():
                return str(cand)
    return None


def _posix(p):
    return str(p).replace("\\", "/")


_FAKE_PY = r"""#!/usr/bin/env bash
# a stand-in interpreter: the key query and the FRED check succeed, everything else is RECORDED
case "$1" in
  -c) echo fake-key; exit 0 ;;
  tools/refresh_fred_cache.py|*/download_av_option_chains.py|tools/download_av_option_chains.py) exit 0 ;;
esac
for a in "$@"; do printf '%s\n' "$a"; done >> "$REC"
printf '%s\n' "----" >> "$REC"
"""


def _run_script(tmp_path, script_text, args, extra_env=None, name="job.sh"):
    bash = _git_bash()
    if bash is None:
        pytest.skip("no bash")
    script = tmp_path / name
    script.write_text(script_text, encoding="utf-8", newline="\n")
    fake = tmp_path / "fakepy"
    fake.write_text(_FAKE_PY, encoding="utf-8", newline="\n")
    rec = tmp_path / f"{name}.rec"
    if rec.exists():
        rec.unlink()
    env = dict(os.environ, REPO=_posix(_ROOT), PY=_posix(fake), BA2_HOME=_posix(tmp_path / "home"),
               REC=_posix(rec), **(extra_env or {}))
    out = subprocess.run([bash, _posix(script), *args], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    runs, cur = [], []
    for line in rec.read_text(encoding="utf-8").splitlines():
        if line == "----":
            runs.append(cur)
            cur = []
        else:
            cur.append(line)
    return runs


def _original(path):
    """The script as imported verbatim from feat/premium-mandatory-loss-exit (commit 5b6f2f17)."""
    r = subprocess.run(["git", "-C", str(_ROOT), "show", f"5b6f2f17:{path}"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("the import commit is not in this clone")
    return r.stdout


def _job_script():
    return (_ROOT / "tools" / "option_style_job.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("args,env_extra", [
    (["long10k", "s3L10", "/g/bundle_jobL10.json", "--fitness", "option_long_dd40_50", "--estimates-source", "fmp"], {}),
    (["premium_stop", "s3P20", "/g/b.json", "--estimates-source", "fmp"], {"OPTION_HISTORY_SOURCE": "alphavantage"}),
    (["wheel50k_ndx", "s3N50", "/g/b.json", "--seed-check-only"], {"SCREEN": "--screen-preset ndx30"}),
])
def test_plain_style_jobs_emit_byte_identical_commands(tmp_path, args, env_extra):
    old = _run_script(tmp_path, _original("tools/option_style_job.sh"), args, env_extra, "old.sh")
    new = _run_script(tmp_path, _job_script(), args, env_extra, "new.sh")
    assert new == old and len(new) == 1
    assert new[0][:2] == ["testplatform/ba2test_launcher.py", "optimize"]


def test_walk_forward_mode_hands_the_driver_the_extras_and_the_unseeded_recipe(tmp_path):
    extras = ["--estimates-source", "fmp", "--walk-forward", "--wf-defer-last-test", "--wf-data-end", "2025-12-31"]
    plain = _run_script(tmp_path, _job_script(), ["long10k", "s3L10", "/g/b.json", *extras[:2]], {}, "p.sh")[0]
    wf = _run_script(tmp_path, _job_script(), ["long10k", "s3L10", "none", *extras], {}, "w.sh")[0]
    assert wf[0] == "tools/run_option_style_wf.py"
    k = wf.index("--")
    assert wf[1:k] == extras
    # after '--': the interpreter, then exactly the plain command without its seed bundle and extras
    rec = wf[k + 1:]
    assert rec[0] == _posix(tmp_path / "fakepy")
    assert rec[1:] == plain[:plain.index("--seed-population")]


def test_walk_forward_mode_hands_a_real_bundle_on_so_the_driver_refuses_it(tmp_path):
    wf = _run_script(tmp_path, _job_script(), ["long10k", "s3L10", "/g/b.json", "--walk-forward"], {}, "w.sh")[0]
    assert wf[wf.index("--seed-population") + 1] == "/g/b.json"


def test_the_recipe_copy_matches_the_style_job_script(tmp_path):
    """The hand-written ``recipe()`` above is the script's exec line (same flags, same order)."""
    got = _run_script(tmp_path, _job_script(), ["long10k", "s3L10", "/g/b.json"], {}, "r.sh")[0]
    mine = recipe(bundle="/g/b.json")[1:]
    assert [t for t in got if t.startswith("--")] == [t for t in mine if t.startswith("--")]


def _chain_env(tmp_path, **kw):
    """A fake REPO whose tools/option_style_job.sh records its argv; GRID_DIR a scratch dir."""
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "option_style_job.sh").write_text(
        'for a in "$@"; do printf "%s\\n" "$a"; done >> "$REC"; printf "%s\\n" "----" >> "$REC"\n',
        encoding="utf-8", newline="\n")
    grid = tmp_path / "grid"
    grid.mkdir(exist_ok=True)
    return {"REPO": _posix(repo), "GRID_DIR": _posix(grid), **kw}


def _run_chain(tmp_path, text, env_extra, name):
    bash = _git_bash()
    if bash is None:
        pytest.skip("no bash")
    script = tmp_path / name
    script.write_text(text, encoding="utf-8", newline="\n")
    fake = tmp_path / "fakepy"
    fake.write_text(_FAKE_PY, encoding="utf-8", newline="\n")
    rec = tmp_path / f"{name}.rec"
    env = dict(os.environ, PY=_posix(fake), BA2_HOME=_posix(tmp_path / "home"), REC=_posix(rec),
               **env_extra)
    out = subprocess.run([bash, _posix(script)], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    runs, cur = [], []
    for line in rec.read_text(encoding="utf-8").splitlines():
        if line == "----":
            runs.append(cur)
            cur = []
        else:
            cur.append(line)
    return runs


def test_the_plain_small_account_chain_emits_byte_identical_jobs(tmp_path):
    env = _chain_env(tmp_path)
    old = _run_chain(tmp_path, _original("tools/grid_option_small_accounts.sh"), env, "old_chain.sh")
    new = _run_chain(tmp_path, (_ROOT / "tools" / "grid_option_small_accounts.sh").read_text(encoding="utf-8"),
                     env, "new_chain.sh")
    assert new == old and len(new) == 7


def test_the_small_account_chain_in_walk_forward_mode(tmp_path):
    wf = "--walk-forward --wf-defer-last-test --wf-data-end 2025-12-31 --wf-top-n 3 --wf-report-dir /r"
    env = _chain_env(tmp_path, WF_ARGS=wf)
    runs = _run_chain(tmp_path, (_ROOT / "tools" / "grid_option_small_accounts.sh").read_text(encoding="utf-8"),
                      env, "wf_chain.sh")
    # jobs 6 and 7 (fixed screens) are not launched in walk-forward mode; the status file says why
    assert [r[:2] for r in runs] == [["long10k", "s3L10"], ["premium_stop", "s3P20"], ["mid_share", "s3A20"],
                                     ["long", "s3C20"], ["burst10k", "s3B10"]]
    for r in runs:
        assert r[2] == "none" and r[-len(wf.split()):] == wf.split()
    status = (tmp_path / "grid" / "s3chain_small-wf.status").read_text(encoding="utf-8")
    assert "jobN50 SKIPPED" in status and "--screen-preset" in status and "jobW10 SKIPPED" in status
    assert not (tmp_path / "grid" / "s3chain_small.status").exists()          # the plain chain's file untouched
