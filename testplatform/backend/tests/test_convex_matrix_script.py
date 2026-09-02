"""``tools/run_convex_matrix.py`` -- the convex-harvest grid driver (plan Task 13).

Mirrors ``test_options2_matrix_script.py``'s shape (the SHAPE + JOB-LIST-GOLDEN split) for the
sibling driver: the script compiles, never offers grid 2's own keys, states its own chain-depth
threshold, and its job list / argv are pinned as goldens. The two Task-13-specific additions:

  * the fitness is ALWAYS ``option_convex`` (never option_car), and the launcher's own mutual
    refusal (``_refuse_convex_fitness_mismatch``) is the second line of defence if this driver
    is ever hand-edited to pass something else;
  * a non-zero ``--stress-spread-bps`` is REFUSED (design §6 item 4 / strategy_fitness.py's
    TASK 14 CARRY note): the stress overlay is inert for option_convex's return term until
    Task 14 restates ``total_return`` in ``stressed_results``.

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
def test_the_job_list_is_exactly_this():
    d = _driver()
    jobs = list(d._jobs(d._DEFAULT_STRATEGIES, ["FMPRating"]))
    assert jobs == [("convex-FMPRating-O_CONVEX", "FMPRating", "O_CONVEX")]


def test_a_second_expert_adds_a_second_job():
    d = _driver()
    jobs = list(d._jobs(["O_CONVEX"], ["FMPRating", "DeterministicScorer"]))
    assert jobs == [
        ("convex-FMPRating-O_CONVEX", "FMPRating", "O_CONVEX"),
        ("convex-DeterministicScorer-O_CONVEX", "DeterministicScorer", "O_CONVEX"),
    ]


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
# The --stress-spread-bps refusal (design §6 item 4)
# --------------------------------------------------------------------------- #
def test_stress_spread_bps_defaults_to_zero():
    d = _driver()
    args = _args(d)
    assert args.stress_spread_bps == 0.0


def test_zero_stress_spread_bps_is_accepted():
    d = _driver()
    d._refuse_nonzero_stress(0.0)  # must not raise


def test_nonzero_stress_spread_bps_is_REFUSED():
    d = _driver()
    with pytest.raises(SystemExit, match="stress-spread-bps"):
        d._refuse_nonzero_stress(15.0)


def test_main_refuses_before_touching_the_universe_file_or_preflight(tmp_path, monkeypatch):
    """The refusal must fire in main() itself, before any file I/O or subprocess spend --
    driven through the real CLI parse, not just the helper function directly."""
    d = _driver()
    with pytest.raises(SystemExit, match="stress-spread-bps"):
        d.main(["--stress-spread-bps", "5", "--dry-run"])


def test_the_refusal_message_names_the_inert_reason():
    """The message must name WHY -- total_return not restated under stress -- not just say
    'refused', so an operator knows this is a Task-14 gap, not a permanent rule."""
    d = _driver()
    with pytest.raises(SystemExit, match="total_return"):
        d._refuse_nonzero_stress(25.0)
