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
#: F4(b) (option-program-review-findings.md, 2026-08-30): O_CC/O_PP are equity-ENTRY overlays
#: (no row in _OPTION_STRATS/_OPTION_GROUPS, hence absent from _PURE_OPTION_STRATEGIES) but
#: they carry a real option leg and belong to the same searched population/seeding target as
#: every pure-option job -- they now rank on option_car too. O_STK is the plain-equity BASELINE
#: control the option arms are measured against (see _rm_opt_for's docstring for the same O_STK
#: carve-out reasoning on a different knob) and is the only equity kind left on the stock default.
_OPTION_CAR_JOINS = ["O_CC", "O_PP"]


@pytest.mark.parametrize("kind", sorted(mod._PURE_OPTION_STRATEGIES))
def test_pure_option_kinds_default_to_the_option_metric(kind):
    assert mod._resolve_fitness(None, kind, "sharpe_ratio") == _OPTION_METRIC


@pytest.mark.parametrize("kind", ["O_STK", "S1", "S2", "S7"])
def test_everything_else_keeps_the_command_default(kind):
    """The plain-equity control (O_STK) and the stock strategies must reach the caller's own
    default, unchanged."""
    for default in ("sharpe_ratio", "calmar_ratio", "consistent_annual_return"):
        assert mod._resolve_fitness(None, kind, default) == default


@pytest.mark.parametrize("kind", _OPTION_CAR_JOINS)
def test_equity_entry_option_overlays_now_default_to_the_option_metric(kind):
    """F4(b): O_CC/O_PP used to keep the caller's stock default (sharpe_ratio for `optimize`) --
    a different metric FAMILY from every other stage-1 job, which breaks stage-2 seeding
    coherence (their "winners" are never scored the way the composition that seeds from them
    will rank them). Investigated (not just flipped): _trades_per_year's structures-not-legs
    accounting only collapses MULTIPLE option legs sharing one transaction_id into one bet
    (see _structure_count) -- O_CC/O_PP write a single option leg per overlay event, so there is
    nothing to collapse, and the equity entry is deliberately never folded into an option leg's
    count (the same equity carve-out _structure_count documents for O_CC's own shares). The
    metric therefore reads a sane, uninflated trade-frequency number for these two kinds -- no
    trade-counting defect blocks the switch."""
    for default in ("sharpe_ratio", "calmar_ratio", "consistent_annual_return"):
        assert mod._resolve_fitness(None, kind, default) == _OPTION_METRIC


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


# --- the CLI help must say what the code does -------------------------------------------------
#
# ``--help`` is what somebody reads before launching a multi-day grid, and both commands' help
# named the EQUITY metric as the pure-option default long after `_resolve_fitness` stopped
# returning it. Nothing else would ever catch that drift.

def _fitness_help(command: str) -> str:
    """The ``--fitness`` help block of ``command``, whitespace-collapsed.

    The parser is built inline in ``main()``, which chdirs into ``backend/``, so the help is
    exercised the way a user sees it: as a subprocess.
    """
    import re
    import subprocess

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    out = subprocess.run([sys.executable, _launcher, command, "--help"],
                         capture_output=True, text=True, env=env, timeout=300)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    flat = " ".join(out.stdout.split())
    # `--fitness FITNESS ... --generations` matches TWICE: once in the usage line (where the
    # span is empty) and once in the options list. Take the longest — the one with the help.
    blocks = re.findall(r"--fitness FITNESS(.*?)--generations", flat)
    assert blocks, f"no --fitness block in `{command} --help`: {flat[-2000:]}"
    return max(blocks, key=len)


@pytest.mark.parametrize("command,stock_default", [("optimize", "sharpe_ratio"),
                                                   ("optimize-batch", "calmar_ratio")])
def test_the_fitness_help_names_the_metric_resolve_fitness_actually_picks(command, stock_default):
    import re

    block = _fitness_help(command)
    first = re.search(r"\b(option_)?consistent_annual_return\b", block)
    assert first, f"`{command} --help` never names the option default at all: {block}"
    assert first.group(1) == "option_", (
        f"`{command} --help` announces the EQUITY metric as the option default; "
        f"_resolve_fitness returns {_OPTION_METRIC!r}. Help block: {block}")
    assert mod._resolve_fitness(None, "O_LC", stock_default) == _OPTION_METRIC
    assert stock_default in block, (
        f"`{command} --help` does not name its stock default {stock_default!r}: {block}")


# --------------------------------------------------------------------------------------------
# option_convex (Task 12): selectable by NAME, never a default
# --------------------------------------------------------------------------------------------
def test_an_explicit_convex_fitness_survives_the_resolution_for_every_kind():
    """The convex-harvest grid names its metric explicitly; the CLI short-circuit must carry it
    through untouched whatever strategy kind the job runs (design §5: fitness `option_convex`)."""
    for kind in sorted(mod._OPTION_STRATEGY_KEYS) + ["S1", "S2"]:
        assert mod._resolve_fitness(mod._CONVEX_FITNESS, kind, "sharpe_ratio") \
            == mod._CONVEX_FITNESS


def test_the_convex_metric_is_never_a_default():
    """Task 13 adds the O_CONVEX key and the mutual refusal. Until then nothing may DEFAULT to
    the convex metric -- an option_car job that silently drifted onto it would be scored on a
    metric its results are not comparable with."""
    for kind in sorted(mod._OPTION_STRATEGY_KEYS) + ["S1", "S2", "S7"]:
        for default in ("sharpe_ratio", "calmar_ratio", "consistent_annual_return"):
            assert mod._resolve_fitness(None, kind, default) != mod._CONVEX_FITNESS


def test_the_convex_metric_the_launcher_names_is_the_one_the_registry_accepts():
    """String-level drift guard: the launcher's constant must BE a registered fitness name."""
    from app.services import strategy_fitness as sf

    assert mod._CONVEX_FITNESS in sf.catalog_accepted_metrics()
