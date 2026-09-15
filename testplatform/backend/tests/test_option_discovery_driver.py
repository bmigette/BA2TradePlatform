"""Discovery must run separate, reproducible experiments without launching real jobs here."""
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("option_discovery_driver", ROOT / "tools/run_options_matrix.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def discovery_args(*extra):
    return M.resolve_args(M.build_parser(), [
        "--profile", "discovery", "--screener-gate-store", "test-store",
        "--max-stock-price", "0", *extra])


def test_discovery_is_32_jobs_after_the_later_risk_exclusions():
    args = discovery_args()
    strategies = args.strategies.split(",")
    assert set(strategies) == {
        "O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BULLPS", "O_BEARCS", "O_BF",
        "O_IC", "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG",
        "O_CC", "O_PP", "O_WHEEL",
    }
    assert args.experts == "FMPRating,DeterministicScorer"
    jobs = list(M._jobs(args.experts.split(","), strategies))
    assert len(jobs) == len({job[0] for job in jobs}) == 32
    assert (args.population, args.generations, args.early_stop) == (200, 60, 8)
    assert args.initial_capital == 20_000
    assert args.equity_cap is None  # Do not silently change the existing compounding policy.
    for name, expert, strategy in jobs:
        cmd = M.build_cmd(args, "launcher.py", name, expert, strategy, "AAPL,F")
        assert cmd[cmd.index("--fitness") + 1] == "option_consistent_annual_return"
        assert cmd[cmd.index("--max-stock-price") + 1] == "0.0"
        assert cmd[cmd.index("--elitism-percent") + 1] == "10.0"


def test_discovery_policy_matches_the_actual_launcher_refusals():
    launcher_spec = importlib.util.spec_from_file_location("discovery_policy_launcher", ROOT / "testplatform/ba2test_launcher.py")
    launcher = importlib.util.module_from_spec(launcher_spec)
    launcher_spec.loader.exec_module(launcher)
    assert M._DISCOVERY_EXCLUDED == launcher._UNDEFINED_RISK_MEMBERS
    assert set(M._DISCOVERY_STRATEGIES) <= launcher._OPTION_STRATEGY_KEYS
    launcher._refuse_unbounded_strategy_request("optimize", M._DISCOVERY_STRATEGIES)


def test_matrix_profile_keeps_the_existing_defaults():
    args = M.resolve_args(M.build_parser(), [])
    assert args.strategies == "OS1,OS2,OS3,OS4,O_CC,O_PP,O_STK"
    assert args.experts == "FMPRating"
    assert (args.population, args.generations, args.early_stop) == (40, 8, None)
    assert args.fitness is None


@pytest.mark.parametrize("extra", [
    ["--strategies", "OS1"], ["--strategies", "O_LC,O_LC"], ["--strategies", ""],
    ["--strategies", "O_SSTD"], ["--strategies", "O_SSTG"],
    ["--experts", "FactorRanker"], ["--population", "0"], ["--initial-capital", "nan"],
    ["--mutation-prob", "1.1"], ["--elitism-percent", "inf"],
    ["--start", "2025-12-31", "--end", "2025-01-01"],
    ["--start", "bad-date"], ["--equity-cap", "-1"], ["--max-stock-price", "100"],
])
def test_invalid_discovery_never_reaches_a_job(extra):
    with pytest.raises(SystemExit):
        discovery_args(*extra)


@pytest.mark.parametrize("strategy", ["O_CC", "O_PP", "O_STK", "O_LC"])
def test_grid_holdout_rail_also_covers_equity_entry_overlays(strategy):
    with pytest.raises(SystemExit, match="2"):
        M.resolve_args(M.build_parser(), ["--strategies", strategy, "--end", "2026-01-01"])


def test_discovery_requires_affordability_gate_configuration():
    with pytest.raises(SystemExit):
        M.resolve_args(M.build_parser(), ["--profile", "discovery"])


def name_for(args, universe="AAPL,F"):
    return M.discovery_name(args, "launcher.py", "optm-FMPRating-O_LC-st1", "FMPRating", "O_LC", universe)


@pytest.mark.parametrize("extra", [
    ["--population", "140"], ["--seed", "7"], ["--early-stop", "10"],
    ["--start", "2024-02-01"], ["--equity-cap", "20000"],
    ["--profit-cap-pct", "0"], ["--fitness", "calmar_ratio"],
    ["--fitness-trade-scale"], ["--fitness-win-rate-factor"],
])
def test_changed_experiment_does_not_reuse_completion_or_checkpoint_name(extra):
    assert name_for(discovery_args()) != name_for(discovery_args(*extra))


def test_same_job_can_resume_after_selecting_a_subset_or_changing_workers():
    original = name_for(discovery_args())
    assert name_for(discovery_args("--strategies", "O_LC", "--experts", "FMPRating",
                                    "--parallel", "1", "--workers", "remote150")) == original
    assert name_for(discovery_args(), universe="F,AAPL") != original  # Allocation order matters.


def test_different_store_changes_identity(monkeypatch):
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "sqlite")
    original = name_for(discovery_args())
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    assert name_for(discovery_args()) != original


def test_optional_cap_and_zero_profit_caps_reach_the_launcher():
    args = discovery_args("--equity-cap", "20000", "--profit-cap-pct", "0",
                          "--profit-share-cap-pct", "0", "--seed", "9")
    cmd = M.build_cmd(args, "launcher.py", "test", "FMPRating", "O_LC", "F")
    for flag, expected in (("--equity-cap", "20000.0"), ("--profit-cap-pct", "0.0"),
                           ("--profit-share-cap-pct", "0.0"), ("--seed", "9")):
        assert cmd[cmd.index(flag) + 1] == expected


def test_dry_run_has_resolved_commands_without_creating_db_or_launching(monkeypatch, tmp_path, capsys):
    db = tmp_path / "absent.sqlite"
    monkeypatch.setenv("DB_FILE", str(db))
    universe = tmp_path / "symbols.txt"
    universe.write_text("f AAPL f\n")
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **kw: pytest.fail("dry-run launched a process"))
    assert M.main(["--profile", "discovery", "--screener-gate-store", "missing-store",
                   "--max-stock-price", "0", "--universe-file", str(universe),
                   "--launcher", "launcher.py", "--dry-run"]) == 0
    assert not db.exists()
    out = capsys.readouterr().out
    assert out.count("    ") == 32
    assert "--population 200" in out and "--early-stop 8" in out
    assert "--universe F,AAPL" in out
    assert "have NOT been validated" in out
    # The default window is 2020-01-01 on the ThetaData store (2026-09-14), so the default
    # dry-run is NOT a limited-window experiment; an explicit 2023 start still is.
    assert "LIMITED WINDOW" not in out
    assert M.main(["--profile", "discovery", "--screener-gate-store", "missing-store",
                   "--max-stock-price", "0", "--universe-file", str(universe),
                   "--launcher", "launcher.py", "--start", "2023-01-01", "--dry-run"]) == 0
    assert "LIMITED WINDOW" in capsys.readouterr().out


def test_unreadable_or_invalid_completion_db_is_not_treated_as_no_prior_jobs(monkeypatch, tmp_path):
    db = tmp_path / "invalid.sqlite"
    db.write_text("not a database")
    monkeypatch.setenv("DB_FILE", str(db))
    with pytest.raises(sqlite3.DatabaseError):
        M._completed_names()


def test_completed_names_are_read_without_writing(monkeypatch, tmp_path):
    db = tmp_path / "jobs.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_optimizations(name TEXT, status TEXT)")
        conn.executemany("INSERT INTO strategy_optimizations VALUES (?, ?)",
                         [("old", "completed"), ("retry", "failed")])
    before = db.read_bytes()
    monkeypatch.setenv("DB_FILE", str(db))
    assert M._completed_names() == {"old"}
    assert db.read_bytes() == before


def test_first_failed_job_stops_matrix_and_returns_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "absent.sqlite"))
    universe = tmp_path / "symbols.txt"
    universe.write_text("F\n")
    calls = []

    def fail(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(M.subprocess, "run", fail)
    assert M.main(["--strategies", "O_LC,O_LP", "--universe-file", str(universe),
                   "--launcher", "launcher.py"]) == 7
    assert len(calls) == 1


def test_stage1_wrapper_uses_the_shared_discovery_profile():
    source = (ROOT / "tools/stage1_run.sh").read_text()
    assert "--profile discovery" in source
    assert 'POP="${POP:-200}"' in source
    assert "--strategies O_LC" not in source  # One matrix definition, not two drifting copies.
    assert "set -euo pipefail" in source
