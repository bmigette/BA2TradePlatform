"""tools/run_options_matrix.py --ohlcv-history-source: forwarded only when given, and part of the
job identity (discovery digest) then, so an Alpaca re-run never shares a name or checkpoint with the
FMP stage-1 job."""
from __future__ import annotations

import importlib.util
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DRIVER = os.path.join(_ROOT, "tools", "run_options_matrix.py")
_GRID_ARGV = ["--profile", "discovery", "--start", "2020-01-01", "--end", "2025-12-31",
              "--screener-gate-store", "/x/metric_store", "--max-stock-price", "0",
              "--early-stop", "8"]


def _driver():
    spec = importlib.util.spec_from_file_location("rom_history_source", _DRIVER)
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
    assert "--ohlcv-history-source" not in cmd
    assert _name_and_cmd(mod)[0] == name


@pytest.mark.parametrize("source", ["alpaca", "fmp"])
def test_given_is_forwarded_and_changes_the_name(source):
    mod = _driver()
    legacy, _ = _name_and_cmd(mod)
    name, cmd = _name_and_cmd(mod, "--ohlcv-history-source", source)
    assert cmd[cmd.index("--ohlcv-history-source") + 1] == source
    assert name != legacy, "a job on another history source must never share a name (checkpoint key)"


def test_alpaca_and_fmp_get_different_names():
    mod = _driver()
    assert (_name_and_cmd(mod, "--ohlcv-history-source", "alpaca")[0]
            != _name_and_cmd(mod, "--ohlcv-history-source", "fmp")[0])


def test_unknown_source_is_refused():
    mod = _driver()
    with pytest.raises(SystemExit):
        mod.resolve_args(mod.build_parser(), _GRID_ARGV + ["--ohlcv-history-source", "yahoo"])
