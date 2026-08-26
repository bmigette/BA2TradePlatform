"""The option grid searches 2023-01-01 .. 2025-12-31; 2026 is the reserved holdout.

Walk-forward validation on 2026 is a separate exercise and is worth nothing if the search has
already seen the data. The grid driver's default END was ``2026-06-30`` -- six months INSIDE
the holdout -- so a default run spent half the validation set on the search::

    $ grep -n 'add_argument("--end"' tools/run_options_matrix.py
    113:    ap.add_argument("--end", default="2026-06-30")

A default is not enough on its own (anyone can pass ``--end``), so the launcher carries a rail
that refuses a PURE-OPTION job reaching past the boundary. It is scoped to option jobs because
non-option backtests are running against 2026 windows right now and must not be disturbed.
"""
import importlib.util
import os
import re
import sys
from datetime import date, timedelta

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_MATRIX = os.path.normpath(os.path.join(_root, "..", "..", "tools", "run_options_matrix.py"))


def _default(flag):
    src = open(_MATRIX).read()
    m = re.search(rf'add_argument\("{re.escape(flag)}",\s*default="([0-9-]+)"', src)
    assert m, f"no literal default found for {flag} in run_options_matrix.py"
    return m.group(1)


# ---------------------------------------------------------------------------
# 1. The configured window
# ---------------------------------------------------------------------------
def test_the_grid_window_is_2023_through_2025():
    assert _default("--start") == "2023-01-01"
    assert _default("--end") == "2025-12-31"


def test_the_holdout_boundary_is_the_start_of_2026():
    assert mod._OPTION_HOLDOUT_START == date(2026, 1, 1)


def test_the_default_end_is_the_last_day_before_the_holdout():
    """Off by one in either direction is a real error: a day short wastes data, a day long
    spends the validation set."""
    assert date.fromisoformat(_default("--end")) == (
        mod._OPTION_HOLDOUT_START - timedelta(days=1))


# ---------------------------------------------------------------------------
# 2. The rail
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("end", ["2026-01-01", "2026-06-30", "2027-01-01"])
def test_a_pure_option_job_cannot_reach_into_the_holdout(end):
    with pytest.raises(SystemExit) as exc:
        mod._assert_option_window_excludes_holdout(["OS1"], end)
    assert "holdout" in str(exc.value)


@pytest.mark.parametrize("end", ["2025-12-31", "2025-01-01", "2024-06-30"])
def test_a_window_inside_the_search_period_is_allowed(end):
    mod._assert_option_window_excludes_holdout(["OS1"], end)  # must not raise


@pytest.mark.parametrize("kind", sorted(mod._PURE_OPTION_STRATEGIES))
def test_every_pure_option_kind_is_railed(kind):
    with pytest.raises(SystemExit):
        mod._assert_option_window_excludes_holdout([kind], "2026-03-01")


@pytest.mark.parametrize("kind", ["O_CC", "O_PP", "O_STK", "S1", "S2", "FACTOR"])
def test_non_option_jobs_are_untouched(kind):
    """Equity work is running against 2026 windows right now. An option-grid policy must not
    reach it."""
    mod._assert_option_window_excludes_holdout([kind], "2026-06-30")  # must not raise


def test_a_mixed_batch_is_refused_because_of_its_option_members():
    with pytest.raises(SystemExit) as exc:
        mod._assert_option_window_excludes_holdout(["S2", "O_STK", "OS3"], "2026-06-30")
    assert "OS3" in str(exc.value)
    assert "S2" not in str(exc.value)


def test_an_unparseable_end_is_refused_not_ignored():
    """Silently skipping a date it cannot read is how a rail stops being one."""
    with pytest.raises(SystemExit) as exc:
        mod._assert_option_window_excludes_holdout(["OS1"], "not-a-date")
    assert "ISO date" in str(exc.value)


def test_both_optimize_entrypoints_call_the_rail():
    """A rail wired into one of the two commands is not a rail."""
    src = open(_launcher).read()
    assert src.count("_assert_option_window_excludes_holdout(") == 3, (
        "expected the definition plus exactly two call sites (optimize and optimize-batch)")
