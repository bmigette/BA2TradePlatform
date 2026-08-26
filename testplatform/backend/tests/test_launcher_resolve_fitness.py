"""``_resolve_fitness`` must point the option grid at the OPTION metric, and nothing else.

There are non-option backtests running against ``consistent_annual_return`` right now and
their scores must not move. A metric that is not SELECTED is never called; a flag inside a
shared metric has to be read correctly on every path and can be read wrongly. So the option
grid selects its own metric and the equity metric is left alone.

These are STRING assertions on purpose: ``option_consistent_annual_return`` is implemented on
the ``option-fitness`` branch, which is not merged here, so calling ``compute_fitness`` with
that name would fail in this worktree while the selection logic under test is already correct.
"""
import importlib.util
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_OPTION_METRIC = "option_consistent_annual_return"
#: Kinds the launcher handles that are NOT pure-option: equity-entry overlays + plain equity.
_EQUITY_KINDS = sorted(mod._OPTION_STRATEGY_KEYS - mod._PURE_OPTION_STRATEGIES)


@pytest.mark.parametrize("kind", sorted(mod._PURE_OPTION_STRATEGIES))
def test_pure_option_kinds_default_to_the_option_metric(kind):
    assert mod._resolve_fitness(None, kind, "sharpe_ratio") == _OPTION_METRIC


@pytest.mark.parametrize("kind", _EQUITY_KINDS + ["S1", "S2", "S7"])
def test_everything_else_keeps_the_command_default(kind):
    """Equity-entry overlays (O_CC / O_PP / O_STK) and the stock strategies must reach the
    caller's own default, unchanged."""
    for default in ("sharpe_ratio", "calmar_ratio", "consistent_annual_return"):
        assert mod._resolve_fitness(None, kind, default) == default


def test_the_equity_half_is_non_empty():
    """Otherwise the test above is vacuous."""
    assert _EQUITY_KINDS == ["O_CC", "O_PP", "O_STK"], _EQUITY_KINDS


@pytest.mark.parametrize("kind", ["O_LC", "OS2", "O_STK", "S2"])
def test_an_explicit_cli_fitness_always_wins(kind):
    assert mod._resolve_fitness("sortino_ratio", kind, "sharpe_ratio") == "sortino_ratio"


def test_the_option_grid_never_reaches_the_equity_metric_by_default():
    """The whole point of the split: no default path from a pure-option kind to the metric the
    running equity backtests are scored on."""
    picked = {mod._resolve_fitness(None, k, "sharpe_ratio")
              for k in mod._PURE_OPTION_STRATEGIES}
    assert picked == {_OPTION_METRIC}
    assert "consistent_annual_return" not in picked
