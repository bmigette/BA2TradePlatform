"""``tools/backtest_parity.py`` -- the private-vs-shared acceptance tool (Task 9a of
docs/plans/2026-09-14-shared-arrays-across-workers.md).

WHAT THIS TOOL IS, AND THEREFORE WHAT THESE TESTS PIN. The operator's acceptance criterion for
memory-mapped shared arrays is "ensure byte comparison of known backtest with new shared cache":
one known genome, re-run twice (``BA2_SHARED_ARRAYS=0`` then ``=1``), and the two persisted rows
must be the SAME BYTES. The comparison is therefore the load-bearing part of the tool, and it is
the part these tests cover: ``compare_rows`` is pure and importable precisely so the verdict can
be pinned without a backtest.

Everything the comparison MUST forgive is enumerated (``_IDENTITY_KEYS``: the run's identity and
its clock) and everything else is a FAIL -- including a key that exists on one side only, and
including a difference in the last decimal of a metric column. There is no tolerance: a shared
mapping that returns "nearly" the same floats is a bug in the mapping, not a rounding question.

The REAL runs are a later task and are never triggered from here: no test in this file may touch
the operator's database or run a backtest. The parent-side tests drive ``main`` with the source
resolver monkeypatched and ``subprocess.run`` rigged to raise, so a regression that silently
started a child fails loudly instead of running for an hour.

Run from the backend dir:
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m pytest tests/test_backtest_parity_tool.py -q
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "tools" / "backtest_parity.py"


def _tool():
    """The tool as a module. Imported by PATH (it is a script, not an installed package), and
    re-executed per call so a test cannot inherit another's module-level state."""
    spec = importlib.util.spec_from_file_location("backtest_parity", str(_SCRIPT))
    m = importlib.util.module_from_spec(spec)
    sys.modules["backtest_parity"] = m
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------------------------
# Fakes. A row is built through the tool's own ``_row_view`` so the tests exercise the same
# ORM-row -> dict projection the real comparison uses (the real rows are SQLAlchemy Backtest
# objects; a SimpleNamespace has the attributes _row_view reads and nothing else).
# --------------------------------------------------------------------------------------------
def _fake_row(**over):
    row = dict(
        name="PARITY-private-TOP1-x",
        id=4242,
        created_at="2026-09-14T10:00:00",
        started_at="2026-09-14T10:00:01",
        completed_at="2026-09-14T10:41:07",
        results={
            "total_return": 31.25,
            "sharpe_ratio": 1.1,
            "run_seconds": 2466.0,
            "per_symbol": {"AAPL": {"pnl": 120.5, "created_at": "2026-09-14T10:00:00"}},
        },
        trades=[
            {"symbol": "AAPL", "entry_price": 10.0, "exit_price": 12.34, "pnl": 2.34,
             "backtest_id": 4242, "id": 1},
            {"symbol": "MSFT", "entry_price": 20.0, "exit_price": 19.0, "pnl": -1.0,
             "backtest_id": 4242, "id": 2},
        ],
        equity_curve=[{"date": "2020-01-02", "equity": 100000.0},
                      {"date": "2020-01-03", "equity": 100120.5}],
        drawdown_curve=[{"date": "2020-01-02", "drawdown": 0.0},
                        {"date": "2020-01-03", "drawdown": -0.4}],
        total_return=31.25,
        sharpe_ratio=1.1,
        max_drawdown=-12.5,
        total_trades=2,
        winning_trades=1,
        losing_trades=1,
        final_equity=131250.0,
        ga_fitness=5.5741,
        optimization_id=487,
        model_id=None,
        strategy_id=9,
    )
    row.update(over)
    return SimpleNamespace(**row)


def _view(m, **over):
    return m._row_view(_fake_row(**over))


# --------------------------------------------------------------------------------------------
# compare_rows -- the acceptance verdict
# --------------------------------------------------------------------------------------------
def test_identical_rows_have_no_differences():
    m = _tool()
    assert m.compare_rows(_view(m), _view(m)) == []


def test_a_changed_trade_exit_price_is_exactly_one_reported_difference():
    """The canonical FAIL: one number in one leg moved. Reported once, naming the path, so the
    operator can go straight to the trade instead of diffing two 10 MB blobs."""
    m = _tool()
    a = _view(m)
    b = _view(m)
    b["trades"] = copy.deepcopy(b["trades"])
    b["trades"][1]["exit_price"] = 12.35
    diffs = m.compare_rows(a, b)
    assert len(diffs) == 1, diffs
    assert "trades[1].exit_price" in diffs[0]
    assert "19.0" in diffs[0] and "12.35" in diffs[0]


def test_key_order_and_json_string_storage_are_not_differences():
    """``results`` is a SQLAlchemy JSON column: it can come back as a dict OR (older rows / a
    text column) as a JSON string, and a dict has no wire order. Both are canonicalised."""
    m = _tool()
    a = _view(m)
    b = _view(m)
    reordered = {k: b["results"][k] for k in reversed(list(b["results"]))}
    b["results"] = json.dumps(reordered, sort_keys=False)
    assert m.compare_rows(a, b) == []


def test_nan_on_both_sides_of_a_metric_column_is_equal():
    """A run with no trades leaves NaN metrics. ``nan != nan`` in Python, so a naive == would
    call every empty run a parity failure."""
    m = _tool()
    a = _view(m, sharpe_ratio=float("nan"))
    b = _view(m, sharpe_ratio=float("nan"))
    assert math.isnan(a["sharpe_ratio"])
    assert m.compare_rows(a, b) == []


def test_run_identity_and_timing_differ_freely_at_any_depth():
    """The two runs are two runs: different row id, different clock, different name. Those are
    the ONLY forgiven keys, and they are forgiven wherever they appear -- including nested inside
    the results blob, where the engine stamps its own timings."""
    m = _tool()
    a = _view(m)
    b = _view(m, name="PARITY-shared-TOP1-x", id=4243,
              created_at="2026-09-14T11:00:00", started_at="2026-09-14T11:00:01",
              completed_at="2026-09-14T11:44:02")
    b["results"] = copy.deepcopy(b["results"])
    b["results"]["run_seconds"] = 2601.0
    b["results"]["per_symbol"]["AAPL"]["created_at"] = "2026-09-14T11:00:00"
    b["trades"] = copy.deepcopy(b["trades"])
    for t in b["trades"]:
        t["backtest_id"] = 4243
    assert m.compare_rows(a, b) == []


def test_an_extra_key_in_one_results_is_reported():
    """A key present on one side only is a FAIL, not a shrug: the shared path producing an extra
    (or missing) field is exactly the kind of divergence this gate exists to catch."""
    m = _tool()
    a = _view(m)
    b = _view(m)
    b["results"] = dict(b["results"])
    b["results"]["shared_arrays_note"] = "mapped"
    diffs = m.compare_rows(a, b)
    assert len(diffs) == 1, diffs
    assert "results.shared_arrays_note" in diffs[0]


def test_a_numeric_column_difference_is_reported():
    m = _tool()
    diffs = m.compare_rows(_view(m), _view(m, total_return=31.26))
    assert any(d.startswith("total_return:") for d in diffs), diffs
    assert "31.25" in " ".join(diffs) and "31.26" in " ".join(diffs)


def test_identity_columns_are_not_compared_as_numbers():
    """id / optimization_id / model_id / strategy_id / the dataset ids identify the ROW, not the
    result; two parity rows always differ on id."""
    m = _tool()
    assert m.compare_rows(_view(m), _view(m, id=1, strategy_id=99)) == []


def test_every_metric_column_is_actually_compared():
    """Guard against the column list silently emptying (a typo in the Float/Integer filter would
    make every comparison pass)."""
    m = _tool()
    cols = m.numeric_columns()
    assert "total_return" in cols and "max_drawdown" in cols and "total_trades" in cols
    assert "id" not in cols and "optimization_id" not in cols and "model_id" not in cols
    assert "strategy_id" not in cols and "prediction_dataset_id" not in cols
    assert "execution_dataset_id" not in cols


# --------------------------------------------------------------------------------------------
# Parent CLI -- must never start a child by accident
# --------------------------------------------------------------------------------------------
def _fake_source():
    return {"opt_id": 487, "rank": 1, "name": "TOP1-sen-S6-goal2020-notional",
            "expert": "SenateTrading", "ga_fitness": 5.5741,
            "start_date": "2020-01-01", "end_date": "2025-12-31", "initial_capital": 100000.0}


def _no_subprocess(monkeypatch, m):
    def boom(*a, **k):  # noqa: ANN001
        raise AssertionError(f"a child was started: {a!r}")
    monkeypatch.setattr(m.subprocess, "run", boom)


def test_dry_run_prints_both_child_commands_and_starts_nothing(monkeypatch, capsys):
    m = _tool()
    monkeypatch.setattr(m, "resolve_source", lambda *a, **k: _fake_source())
    monkeypatch.setattr(m, "existing_parity_names", lambda names: [])
    _no_subprocess(monkeypatch, m)
    rc = m.main(["--opt", "487", "--rank", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("--_child") == 2
    assert "BA2_SHARED_ARRAYS=0" in out and "BA2_SHARED_ARRAYS=1" in out
    assert "PARITY-private-TOP1-sen-S6-goal2020-notional" in out
    assert "PARITY-shared-TOP1-sen-S6-goal2020-notional" in out


def test_a_label_makes_a_second_comparison_a_new_pair_of_names(monkeypatch, capsys):
    m = _tool()
    monkeypatch.setattr(m, "resolve_source", lambda *a, **k: _fake_source())
    monkeypatch.setattr(m, "existing_parity_names", lambda names: [])
    _no_subprocess(monkeypatch, m)
    m.main(["--opt", "487", "--rank", "1", "--label", "run2", "--dry-run"])
    out = capsys.readouterr().out
    assert "PARITY-private-TOP1-sen-S6-goal2020-notional-run2" in out


def test_existing_parity_rows_are_refused_never_overwritten(monkeypatch, capsys):
    """The source row and any earlier comparison are evidence. A re-run gets --label; it never
    lands on top of a row somebody may already have read."""
    m = _tool()
    monkeypatch.setattr(m, "resolve_source", lambda *a, **k: _fake_source())
    monkeypatch.setattr(m, "existing_parity_names",
                        lambda names: ["PARITY-private-TOP1-sen-S6-goal2020-notional"])
    _no_subprocess(monkeypatch, m)
    rc = m.main(["--opt", "487", "--rank", "1"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already exist" in out.lower()
    assert "--label" in out


# --------------------------------------------------------------------------------------------
# Child protocol
# --------------------------------------------------------------------------------------------
def test_child_id_is_read_from_the_last_marker_line_despite_noise():
    m = _tool()
    stdout = ("some preload chatter\n"
              "WARNING: whatever\n"
              f"{m.BT_ID_PREFIX}1701\n"
              "\n")
    assert m.parse_child_bt_id(stdout) == 1701


def test_a_child_that_printed_no_marker_is_a_failure_not_a_guess():
    m = _tool()
    assert m.parse_child_bt_id("traceback...\nBOOM\n") is None
