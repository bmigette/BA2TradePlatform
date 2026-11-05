"""tools/run_options_matrix.py --benchmark-regime-gates: forwarded only when given, and part of the
job identity (discovery digest) then, so a SPY-gated job never shares a name or checkpoint with an
ungated one. Structure mirrors test_run_options_matrix_history_source.py."""
from __future__ import annotations

import importlib.util
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DRIVER = os.path.join(_ROOT, "tools", "run_options_matrix.py")
_GRID_ARGV = ["--profile", "discovery", "--start", "2020-01-01", "--end", "2025-12-31",
              "--screener-gate-store", "/x/metric_store", "--max-stock-price", "0",
              "--early-stop", "8", "--strategies", "O_BULLPS,O_CSP"]
FLAG = "--benchmark-regime-gates"


def _driver():
    spec = importlib.util.spec_from_file_location("rom_benchmark_regime", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _name_and_cmd(mod, *extra, strat="O_BULLPS"):
    a = mod.resolve_args(mod.build_parser(), _GRID_ARGV + list(extra))
    name = mod.discovery_name(a, _DRIVER, "optm-X-" + strat, "DeterministicScorer", strat, "AAPL",
                              "legacy")
    return name, mod.build_cmd(a, _DRIVER, name, "DeterministicScorer", strat, "AAPL", "legacy")


def test_absent_forwards_nothing_and_keeps_the_name():
    mod = _driver()
    name, cmd = _name_and_cmd(mod)
    assert FLAG not in cmd
    assert _name_and_cmd(mod)[0] == name
    # an explicit history source alone must not add the flag either
    assert FLAG not in _name_and_cmd(mod, "--ohlcv-history-source", "alpaca")[1]


def test_given_is_forwarded_and_changes_the_name():
    mod = _driver()
    legacy, _ = _name_and_cmd(mod)
    name, cmd = _name_and_cmd(mod, FLAG)
    assert FLAG in cmd and name != legacy
    assert _name_and_cmd(mod, FLAG)[0] == name, "stable across runs"


def test_flag_does_not_swallow_the_next_token():
    mod = _driver()
    name, cmd = _name_and_cmd(mod, FLAG, "--ohlcv-history-source", "alpaca")
    assert FLAG in cmd and cmd[cmd.index("--ohlcv-history-source") + 1] == "alpaca"
    assert _name_and_cmd(mod, FLAG, "--ohlcv-history-source", "alpaca")[0] == name
    # the history source still moves the identity under the flag (it was not eaten as its value)
    assert name != _name_and_cmd(mod, FLAG)[0]
    assert name != _name_and_cmd(mod, "--ohlcv-history-source", "alpaca")[0]


def test_flag_and_market_profile_get_distinct_names():
    mod = _driver()
    flag_name = _name_and_cmd(mod, FLAG)[0]
    prof_name = _name_and_cmd(mod, "--market-condition-profile", "ohlcv-v1",
                              "--market-condition-manifest", "abc123")[0]
    both = _name_and_cmd(mod, FLAG, "--market-condition-profile", "ohlcv-v1",
                         "--market-condition-manifest", "abc123")[0]
    assert len({flag_name, prof_name, both}) == 3


@pytest.mark.parametrize("strategy", ["O_CC", "O_PP", "O_STK", "S1"])
def test_refused_for_a_strategy_without_spy_leaves(strategy):
    # the DEFAULT discovery list holds O_CC/O_PP, so the flag needs an explicit --strategies
    mod = _driver()
    with pytest.raises(SystemExit):
        mod.resolve_args(mod.build_parser(),
                         ["--start", "2024-02-01", "--end", "2025-12-31", "--strategies", strategy,
                          FLAG])


def test_refused_with_the_default_discovery_list_which_holds_ungated_keys():
    mod = _driver()
    argv = [a for a in _GRID_ARGV if a not in ("--strategies", "O_BULLPS,O_CSP")]
    with pytest.raises(SystemExit):
        mod.resolve_args(mod.build_parser(), argv + [FLAG])
    mod.resolve_args(mod.build_parser(), argv)       # without the flag it is fine
