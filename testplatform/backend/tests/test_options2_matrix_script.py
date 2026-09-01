"""``tools/run_options2_matrix.py`` -- the grid-2 driver (plan Task 10).

A matrix driver is a script nobody unit-tests and everybody trusts, and its failure mode is
the expensive one: a job list that is subtly wrong launches days of compute against the wrong
strategy/expert pairing, the wrong universe or the wrong fitness, and the result LOOKS like a
finding. So this pins the two things that decide what gets spent:

  * the SHAPE of the script -- it compiles, it exposes the flags the runbook will use, and it
    never offers a phase-gated key (the ``bash -n``-equivalent for a Python driver);
  * the JOB LIST and the argv each job produces, as a GOLDEN -- expert pairing, per-strategy
    chain-depth threshold, universe, fitness, store and window.

The script is loaded BY ABSOLUTE PATH (the ``_LAUNCHER_PATH`` pattern the other launcher tests
use) rather than imported, because ``tools/`` is not a package and a CWD-relative import
resolves differently under ``pytest`` from the repo root than from ``testplatform/backend``.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/test_options2_matrix_script.py -q
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
_SCRIPT = os.path.join(_REPO, "tools", "run_options2_matrix.py")
_LAUNCHER_PATH = os.path.join(_REPO, "testplatform", "ba2test_launcher.py")


def _driver():
    spec = importlib.util.spec_from_file_location("run_options2_matrix", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["run_options2_matrix"] = m
    spec.loader.exec_module(m)
    return m


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_matrix2", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_matrix2"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_the_script_compiles():
    """The ``bash -n`` equivalent: a syntax error in a driver is only discovered when an
    operator runs it, which is the moment they were expecting a week of compute to start."""
    py_compile.compile(_SCRIPT, doraise=True)


def test_the_driver_exists_beside_its_siblings():
    assert os.path.isfile(_SCRIPT)
    assert os.path.isfile(os.path.join(_REPO, "tools", "probe_option_chain_depth.py"))


def test_the_default_strategies_are_grid_2_phase_1_exactly():
    """Design section 7 phase 1 = {O_LEAPC, O_LEAPP, O_ERN, O_CBS, O_PBS}. Checked against the
    LAUNCHER's own set rather than a second list, so the two cannot drift."""
    d, m = _driver(), _launcher()
    assert set(d._DEFAULT_STRATEGIES) == m._GRID2_OPTION_STRATEGIES


def test_the_driver_never_offers_a_phase_gated_key():
    """O_PMCC/O_CAL refuse at the launcher, so offering them here would only ever produce a
    run that dies job by job."""
    d, m = _driver(), _launcher()
    for bad in m._PHASE_GATED_OPTION_STRATEGIES:
        assert bad not in d._DEFAULT_STRATEGIES
        assert bad not in d._MIN_DTE


def test_every_offered_strategy_states_its_chain_depth():
    """The preflight is keyed on this table, so a key with no entry would run on an
    unfiltered universe -- silently, which is the failure the probe exists to prevent."""
    d = _driver()
    assert set(d._MIN_DTE) == set(d._DEFAULT_STRATEGIES)


def test_the_chain_depth_thresholds_are_the_designs():
    """Design section 5: LEAPS keys need DTE >= 365, the backspreads >= 180, O_ERN >= 7."""
    d = _driver()
    assert d._MIN_DTE == {"O_LEAPC": 365, "O_LEAPP": 365, "O_ERN": 7,
                          "O_CBS": 180, "O_PBS": 180}


def test_an_unknown_strategy_fails_LOUDLY_rather_than_running_unfiltered():
    d = _driver()
    with pytest.raises(SystemExit, match="chain-depth threshold"):
        d._thresholds(["O_LEAPC", "O_NOPE"])


def test_the_preflight_runs_once_per_DISTINCT_threshold():
    """Three probes for five keys: the probe walks the whole parquet tree, so running it per
    STRATEGY would pay for the same scan twice."""
    d = _driver()
    groups = d._thresholds(d._DEFAULT_STRATEGIES)
    assert set(groups) == {365, 180, 7}
    assert sorted(groups[365]) == ["O_LEAPC", "O_LEAPP"]
    assert sorted(groups[180]) == ["O_CBS", "O_PBS"]
    assert groups[7] == ["O_ERN"]


def test_the_search_budget_is_the_designs_and_is_env_overridable():
    """Design section 7: "Pop 40 / gen 6, modest by design". Env-overridable so a follow-up
    deeper search needs no code edit (and so this test can prove the override works)."""
    d = _driver()
    assert (d._DEFAULT_POPULATION, d._DEFAULT_GENERATIONS) == (40, 6)


def test_the_population_override_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("BA2_GRID2_POPULATION", "12")
    monkeypatch.setenv("BA2_GRID2_GENERATIONS", "3")
    sys.modules.pop("run_options2_matrix", None)
    d = _driver()
    assert (d._DEFAULT_POPULATION, d._DEFAULT_GENERATIONS) == (12, 3)
    sys.modules.pop("run_options2_matrix", None)


# --------------------------------------------------------------------------- #
# The job list -- GOLDEN
# --------------------------------------------------------------------------- #
def test_the_job_list_is_exactly_this():
    """THE GOLDEN. Strategy-major order (a partial run finishes whole structures), one job per
    (strategy x expert), and the earnings expert on O_ERN AND NOWHERE ELSE."""
    d = _driver()
    jobs = list(d._jobs(d._DEFAULT_STRATEGIES, ["FMPRating"], "FMPEarningsEvent"))
    assert jobs == [
        ("opt2-FMPRating-O_LEAPC", "FMPRating", "O_LEAPC"),
        ("opt2-FMPRating-O_LEAPP", "FMPRating", "O_LEAPP"),
        ("opt2-FMPEarningsEvent-O_ERN", "FMPEarningsEvent", "O_ERN"),
        ("opt2-FMPRating-O_CBS", "FMPRating", "O_CBS"),
        ("opt2-FMPRating-O_PBS", "FMPRating", "O_PBS"),
    ]


def test_the_event_key_never_runs_under_a_non_event_expert():
    """WHY THE PAIRING IS PER-STRATEGY. O_ERN's entry gate reads a stamp only
    ``FMPEarningsEvent`` writes; under any other expert the gate can never fire (absence
    never reads as firing, by design) and the job trades NOTHING while looking healthy.
    Adding more general experts must not change that.
    """
    d = _driver()
    jobs = list(d._jobs(d._DEFAULT_STRATEGIES, ["FMPRating", "DeterministicScorer"],
                        "FMPEarningsEvent"))
    ern = [j for j in jobs if j[2] == "O_ERN"]
    assert [j[1] for j in ern] == ["FMPEarningsEvent"]
    # ... and the earnings expert never drives a long-dated key: gating a 400-day position
    # on a 10-day event look-ahead would be a different strategy wearing O_LEAPC's name.
    assert not [j for j in jobs if j[1] == "FMPEarningsEvent" and j[2] != "O_ERN"]


def test_the_name_suffix_reaches_every_job():
    d = _driver()
    jobs = list(d._jobs(["O_LEAPC"], ["FMPRating"], "FMPEarningsEvent", "-v2"))
    assert jobs == [("opt2-FMPRating-O_LEAPC-v2", "FMPRating", "O_LEAPC")]


def _args(d, **over):
    """The driver's REAL parser, parsed with no arguments -- so the argv golden below is the
    argv a bare invocation actually produces, not a hand-written stand-in that can drift from
    the defaults it is supposed to be pinning."""
    args = d.build_parser().parse_args([])
    for k, v in over.items():
        setattr(args, k, v)
    return args


def test_the_launched_command_is_exactly_this():
    """THE ARGV GOLDEN. Every token here decides what the compute is spent on:

      --options-store parquet   the sqlite/Alpaca store's history floor is 2024-01-18, so a
                                2023 start RAISES on the default store (design section 7's
                                window is only servable by parquet).
      --fitness option_car      design section 7: option_car for EVERY key in THIS grid. The
                                convex grid scores overlapping structures with a DIFFERENT
                                metric and the two must never be silently crossed.
      --run-schedule daily      O_ERN's entry window is 1-5 days wide; a weekly scan misses it.
      --universe <kept>         the PREFLIGHT's kept list for THIS key's depth, not the raw
                                universe.
    """
    d = _driver()
    args = _args(d)
    cmd = d.build_cmd(args, "/x/ba2-test", "opt2-FMPRating-O_LEAPC", "FMPRating",
                      "O_LEAPC", ["AAPL", "MSFT"])
    assert cmd == [
        "/x/ba2-test", "optimize", "--expert", "FMPRating",
        "--universe", "AAPL,MSFT", "--strategy", "O_LEAPC",
        "--start", "2023-01-01", "--end", "2025-12-31",
        "--interval", "1d", "--population", "40", "--generations", "6",
        "--initial-capital", "20000.0",
        "--options-store", "parquet",
        "--fitness", "option_car",
        "--run-schedule", "daily", "--name", "opt2-FMPRating-O_LEAPC",
        "--parallel", "4",
        "--profit-cap-pct", "2000.0", "--profit-share-cap-pct", "25.0",
    ]


def test_a_zero_cap_is_FORWARDED_not_omitted():
    """The shared falsy-zero rule (``tools/matrix_flags.cap_passthrough``): omitting the flag
    makes the launcher re-apply its OWN 2000/25 default, i.e. the opposite of "disable"."""
    d = _driver()
    args = _args(d)
    args.profit_cap_pct = 0.0
    args.profit_share_cap_pct = 0.0
    cmd = d.build_cmd(args, "/x/ba2-test", "n", "FMPRating", "O_LEAPC", ["AAPL"])
    assert "--profit-cap-pct" in cmd and cmd[cmd.index("--profit-cap-pct") + 1] == "0.0"
    assert cmd[cmd.index("--profit-share-cap-pct") + 1] == "0.0"


def test_a_py_launcher_path_is_run_through_the_interpreter():
    """``--launcher <worktree>/ba2test_launcher.py`` is how a branch is exercised without
    touching the editable install -- it must be invoked as a script, not exec'd."""
    d = _driver()
    cmd = d.build_cmd(_args(d), "/w/ba2test_launcher.py", "n", "FMPRating", "O_LEAPC",
                      ["AAPL"])
    assert cmd[0] == sys.executable
    assert cmd[1] == "/w/ba2test_launcher.py"
    assert cmd[2] == "optimize"


def test_the_window_stops_short_of_the_reserved_holdout():
    """2026 is the walk-forward holdout; the launcher refuses to search into it, and a driver
    default that reached past the boundary would make every job die at argument parsing."""
    d = _driver()
    m = _launcher()
    args = _args(d)
    assert args.end == "2025-12-31"
    m._assert_option_window_excludes_holdout(["O_LEAPC"], args.end)  # must not raise
