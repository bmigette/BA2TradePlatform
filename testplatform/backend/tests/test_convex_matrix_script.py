"""``tools/run_convex_matrix.py`` -- the convex-harvest grid driver (plan Task 13).

Mirrors ``test_options2_matrix_script.py``'s shape (the SHAPE + JOB-LIST-GOLDEN split) for the
sibling driver: the script compiles, never offers grid 2's own keys, states its own chain-depth
threshold, and its job list / argv are pinned as goldens. The two Task-13-specific additions:

  * the fitness is ALWAYS ``option_convex`` (never option_car), and the launcher's own mutual
    refusal (``_refuse_convex_fitness_mismatch``) is the second line of defence if this driver
    is ever hand-edited to pass something else;
  * ``--stress-spread-bps`` is FORWARDED when non-zero. It was REFUSED (design §6 item 4)
    while ``stressed_results`` restated only annualized_return/max_drawdown, which left the
    stress inert for the end-of-window total return option_convex ranks on -- "the run would
    look stress-tested and would not be". Plan Task 14b item 6 restated total_return and wired
    ``_min_with_stressed`` into the _CONVEX_ALIASES branch, so the refusal is gone and the
    replacement is a test that the stress actually MOVES the metric (that one lives beside the
    metric, in test_strategy_fitness_convex_frozen.py).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/test_convex_matrix_script.py -q
"""
from __future__ import annotations

import importlib.util
import os
import py_compile
import sys

import pytest

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_REPO, "tools", "run_convex_matrix.py")
_LAUNCHER_PATH = os.path.join(_REPO, "testplatform", "ba2test_launcher.py")


def _driver():
    spec = importlib.util.spec_from_file_location("run_convex_matrix", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["run_convex_matrix"] = m
    spec.loader.exec_module(m)
    return m


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_convex_matrix", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_convex_matrix"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_the_script_compiles():
    """The ``bash -n`` equivalent: a syntax error is only discovered when an operator runs it,
    which is the moment they were expecting a week of compute to start."""
    py_compile.compile(_SCRIPT, doraise=True)


def test_the_driver_exists_beside_its_siblings():
    assert os.path.isfile(_SCRIPT)
    assert os.path.isfile(os.path.join(_REPO, "tools", "probe_option_chain_depth.py"))
    assert os.path.isfile(os.path.join(_REPO, "tools", "run_options2_matrix.py"))


def test_the_default_strategies_are_exactly_the_convex_grids_own_key_set():
    """Checked against the LAUNCHER's own set rather than a second list, so the two cannot
    drift (mirrors test_options2_matrix_script.py's identical grid-2 pin)."""
    d, m = _driver(), _launcher()
    assert set(d._DEFAULT_STRATEGIES) == m._CONVEX_OPTION_STRATEGIES


def test_the_driver_never_offers_a_grid_2_key():
    """This driver is a SEPARATE grid (design §8) -- it must never default to a key from the
    option_car matrix, which would invite scoring it under option_convex by accident."""
    d, m = _driver(), _launcher()
    for other in m._GRID2_OPTION_STRATEGIES:
        assert other not in d._DEFAULT_STRATEGIES


def test_every_offered_strategy_states_its_chain_depth():
    d = _driver()
    assert set(d._MIN_DTE) == set(d._DEFAULT_STRATEGIES)


def test_the_chain_depth_threshold_is_the_designs_270():
    """Design §4: "keep a stage-1-universe symbol iff the cache carries expiries with bars at
    DTE >= 270 in-window" -- broader than grid 2's LEAPS threshold (365)."""
    d = _driver()
    assert d._MIN_DTE == {"O_CONVEX": 270}


def test_an_unknown_strategy_fails_LOUDLY_rather_than_running_unfiltered():
    d = _driver()
    with pytest.raises(SystemExit, match="chain-depth threshold"):
        d._thresholds(["O_CONVEX", "O_NOPE"])


def test_the_search_budget_is_the_designs_and_is_env_overridable():
    """Design §5: "Pop 40 / gen 6, modest by design"."""
    d = _driver()
    assert (d._DEFAULT_POPULATION, d._DEFAULT_GENERATIONS) == (40, 6)


def test_the_population_override_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("BA2_CONVEX_POPULATION", "12")
    monkeypatch.setenv("BA2_CONVEX_GENERATIONS", "3")
    sys.modules.pop("run_convex_matrix", None)
    d = _driver()
    assert (d._DEFAULT_POPULATION, d._DEFAULT_GENERATIONS) == (12, 3)
    sys.modules.pop("run_convex_matrix", None)


def test_the_fitness_default_is_option_convex_and_nothing_else():
    d = _driver()
    assert d._FITNESS == "option_convex"
    args = d.build_parser().parse_args([])
    assert args.fitness == "option_convex"


# --------------------------------------------------------------------------- #
# The job list -- GOLDEN
# --------------------------------------------------------------------------- #
def test_the_default_experts_are_two_measured_stage_1_experts():
    """Design §5: "O_CONVEX x stage-1's experts, singles. 2-3 jobs." FMPRating AND
    DeterministicScorer both have measured large-cap results (DeterministicScorer's in the
    matrix3 grid) -- review addition, 2026-09-02."""
    d = _driver()
    assert d._DEFAULT_EXPERTS == ["FMPRating", "DeterministicScorer"]


def test_the_job_list_is_exactly_this():
    """THE GOLDEN, using the REAL default experts -- 2 jobs, inside design §5's 2-3 range."""
    d = _driver()
    jobs = list(d._jobs(d._DEFAULT_STRATEGIES, d._DEFAULT_EXPERTS))
    assert jobs == [
        ("convex-FMPRating-O_CONVEX", "FMPRating", "O_CONVEX"),
        ("convex-DeterministicScorer-O_CONVEX", "DeterministicScorer", "O_CONVEX"),
    ]


def test_a_single_expert_override_still_yields_one_job():
    d = _driver()
    jobs = list(d._jobs(["O_CONVEX"], ["FMPRating"]))
    assert jobs == [("convex-FMPRating-O_CONVEX", "FMPRating", "O_CONVEX")]


def test_the_name_suffix_reaches_every_job():
    d = _driver()
    jobs = list(d._jobs(["O_CONVEX"], ["FMPRating"], "-v2"))
    assert jobs == [("convex-FMPRating-O_CONVEX-v2", "FMPRating", "O_CONVEX")]


def _args(d, **over):
    """The driver's REAL parser, parsed with no arguments -- so the argv golden below is the
    argv a bare invocation actually produces, not a hand-written stand-in that can drift."""
    args = d.build_parser().parse_args([])
    for k, v in over.items():
        setattr(args, k, v)
    return args


def test_the_launched_command_is_exactly_this():
    """THE ARGV GOLDEN.

      --options-store parquet   same reasoning as grid 2's driver (2024-01-18 history floor).
      --fitness option_convex   design §3/§8: the ONLY fitness this driver may pass.
      --run-schedule daily      matches grid 2's driver.
      --universe <kept>         the PREFLIGHT's kept list for O_CONVEX's DTE>=270 depth.
    """
    d = _driver()
    args = _args(d)
    cmd = d.build_cmd(args, "/x/ba2-test", "convex-FMPRating-O_CONVEX", "FMPRating",
                      "O_CONVEX", ["AAPL", "MSFT"])
    assert cmd == [
        "/x/ba2-test", "optimize", "--expert", "FMPRating",
        "--universe", "AAPL,MSFT", "--strategy", "O_CONVEX",
        "--start", "2023-01-01", "--end", "2025-12-31",
        "--interval", "1d", "--population", "40", "--generations", "6",
        "--initial-capital", "20000.0",
        "--options-store", "parquet",
        "--fitness", "option_convex",
        "--run-schedule", "daily", "--name", "convex-FMPRating-O_CONVEX",
        "--parallel", "4",
        "--profit-cap-pct", "2000.0", "--profit-share-cap-pct", "25.0",
    ]


def test_a_zero_cap_is_FORWARDED_not_omitted():
    """Same shared falsy-zero rule (``tools/matrix_flags.cap_passthrough``) grid 2's driver
    forwards."""
    d = _driver()
    args = _args(d)
    args.profit_cap_pct = 0.0
    args.profit_share_cap_pct = 0.0
    cmd = d.build_cmd(args, "/x/ba2-test", "n", "FMPRating", "O_CONVEX", ["AAPL"])
    assert "--profit-cap-pct" in cmd and cmd[cmd.index("--profit-cap-pct") + 1] == "0.0"
    assert cmd[cmd.index("--profit-share-cap-pct") + 1] == "0.0"


def test_a_py_launcher_path_is_run_through_the_interpreter():
    d = _driver()
    cmd = d.build_cmd(_args(d), "/w/ba2test_launcher.py", "n", "FMPRating", "O_CONVEX",
                      ["AAPL"])
    assert cmd[0] == sys.executable
    assert cmd[1] == "/w/ba2test_launcher.py"
    assert cmd[2] == "optimize"


def test_the_window_stops_short_of_the_reserved_holdout():
    d = _driver()
    args = _args(d)
    assert args.end == "2025-12-31"


# --------------------------------------------------------------------------- #
# --stress-spread-bps: refused until 2026-09-02, forwarded since
# --------------------------------------------------------------------------- #
def test_stress_spread_bps_defaults_to_zero():
    """OFF by default, and that is a comparability rule, not shyness: scores either side of a
    non-zero stress are not comparable."""
    d = _driver()
    args = _args(d)
    assert args.stress_spread_bps == 0.0


def test_the_refusal_is_gone():
    """Plan Task 14b item 6: stressed_results restates total_return, so the stress is no longer
    inert for option_convex and the interim refusal has no reason to exist."""
    d = _driver()
    assert not hasattr(d, "_refuse_nonzero_stress")


def test_a_zero_stress_forwards_no_flag_at_all():
    """The launcher's own default is 0.0, so passing it would be noise -- and a flag present in
    the argv of every job is how a 'stress was on' misreading starts."""
    d = _driver()
    cmd = d.build_cmd(_args(d), "ba2-test", "job", "FMPRating", "O_CONVEX", ["AAPL"])
    assert "--stress-spread-bps" not in cmd


def test_a_nonzero_stress_reaches_the_launcher_argv():
    """The whole point of lifting the refusal: the flag must actually be forwarded, or the
    driver would accept it and drop it -- a quieter version of the same lie."""
    d = _driver()
    args = _args(d, stress_spread_bps=40.0)
    cmd = d.build_cmd(args, "ba2-test", "job", "FMPRating", "O_CONVEX", ["AAPL"])
    assert "--stress-spread-bps" in cmd
    assert cmd[cmd.index("--stress-spread-bps") + 1] == "40.0"


def test_main_accepts_a_nonzero_stress(capsys):
    """Through the real CLI parse: a non-zero value no longer exits."""
    d = _driver()
    assert d.main(["--stress-spread-bps", "5", "--dry-run"]) == 0


def test_the_dry_run_universe_count_is_LABELLED_unfiltered(capsys):
    """Review finding (2026-09-02): --dry-run short-circuits ``_preflight`` to the RAW
    universe -- the real probe subprocess never runs -- so an unlabelled symbol count reads
    as "this many passed the DTE>=270 probe", which is false for every --dry-run line."""
    d = _driver()
    rc = d.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(unfiltered, dry-run)" in out


# --------------------------------------------------------------------------- #
# _completed_names -- named DB exceptions, never a bare swallow
# --------------------------------------------------------------------------- #
def test_completed_names_warns_and_returns_empty_on_a_missing_table(tmp_path, monkeypatch,
                                                                    caplog):
    """A fresh db (no strategy_optimizations table yet) is a real, expected first-run shape --
    but review finding (2026-09-02): the old bare ``except Exception`` swallowed it (and a
    LOCKED db, the dangerous case) with zero trace. Must log at WARNING, not stay silent."""
    import logging
    import sqlite3

    empty_db = tmp_path / "empty.db"
    sqlite3.connect(str(empty_db)).close()  # a real sqlite file with NO tables
    monkeypatch.setenv("DB_FILE", str(empty_db))
    d = _driver()
    with caplog.at_level(logging.WARNING):
        names = d._completed_names()
    assert names == set()
    assert any("could not read completed job names" in r.message for r in caplog.records)


def test_completed_names_reraises_an_unexpected_exception_type(monkeypatch):
    """Only the named sqlite exceptions are swallowed-with-a-warning; anything else (a
    programming error) must NOT be hidden."""
    import sqlite3

    d = _driver()

    def _boom(*_a, **_k):
        raise TypeError("not a sqlite problem")

    monkeypatch.setattr(sqlite3, "connect", _boom)
    with pytest.raises(TypeError):
        d._completed_names()


def test_the_help_text_no_longer_advertises_a_refusal():
    """An operator reading --help must not be told the flag is refused when it is honoured."""
    d = _driver()
    action = next(a for a in d.build_parser()._actions
                  if a.dest == "stress_spread_bps")
    assert "REFUSED" not in (action.help or "")
    assert "total_return" in (action.help or "")
