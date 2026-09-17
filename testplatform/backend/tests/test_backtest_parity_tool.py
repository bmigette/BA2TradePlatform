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
import io
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
            "expert": "SenateTrading", "ga_fitness": 5.5741, "option_source": False,
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
              f"{m.EVIDENCE_PREFIX}{json.dumps(_evidence())}\n"
              f"{m.BT_ID_PREFIX}1701\n"
              "\n")
    assert m.parse_child_bt_id(stdout) == 1701


def test_a_child_that_printed_no_marker_is_a_failure_not_a_guess():
    m = _tool()
    assert m.parse_child_bt_id("traceback...\nBOOM\n") is None


# --------------------------------------------------------------------------------------------
# Evidence -- a PASS must prove the shared path was actually used
# --------------------------------------------------------------------------------------------
def _evidence(**over):
    ev = {"shared_enabled": True, "total_trades": 214, "bars_shared_mb": 812.5,
          "bars_private_mb": 96.0, "options_shared_mb": 0.0, "options_private_mb": 0.0,
          "options_entries": 0, "options_provider_built": False}
    ev.update(over)
    return ev


def _private_evidence(**over):
    base = {"shared_enabled": False, "bars_shared_mb": 0.0, "bars_private_mb": 908.5}
    base.update(over)
    return _evidence(**base)


def test_evidence_is_read_from_its_protocol_line():
    m = _tool()
    stdout = (f"preload chatter\n{m.EVIDENCE_PREFIX}{json.dumps(_evidence())}\n"
              f"{m.BT_ID_PREFIX}1701\n")
    assert m.parse_child_evidence(stdout) == _evidence()


def test_absent_or_unparsable_evidence_is_none_not_a_guess():
    m = _tool()
    assert m.parse_child_evidence("nothing here\n") is None
    assert m.parse_child_evidence(f"{m.EVIDENCE_PREFIX}{{not json\n") is None


def test_matching_modes_report_no_evidence_problem():
    m = _tool()
    assert m.evidence_problems(_private_evidence(), _evidence()) == []


def test_a_shared_child_that_mapped_no_bars_is_not_evidence():
    """The failure this gate exists for: both children take the private path (an env name typo, a
    consumer that fell back) and the rows match for a reason that says nothing about arrays."""
    m = _tool()
    problems = m.evidence_problems(_private_evidence(),
                                   _evidence(bars_shared_mb=0.0, bars_private_mb=908.5))
    assert problems and any("0 MB" in p for p in problems)


def test_shared_arrays_disabled_in_either_child_is_refused():
    m = _tool()
    assert any("SHARED child" in p
               for p in m.evidence_problems(_private_evidence(),
                                            _evidence(shared_enabled=False)))
    assert any("PRIVATE child" in p
               for p in m.evidence_problems(_private_evidence(shared_enabled=True), _evidence()))


def test_missing_option_mapping_is_only_a_problem_when_the_private_run_had_options():
    """An equity run holds no option arrays at all; 0 vs 0 is silence, not a failure."""
    m = _tool()
    assert m.evidence_problems(_private_evidence(options_private_mb=0.0),
                               _evidence(options_shared_mb=0.0)) == []
    assert any("option" in p for p in
               m.evidence_problems(_private_evidence(options_private_mb=1500.0),
                                   _evidence(options_shared_mb=0.0)))


def test_a_run_whose_shared_child_mapped_nothing_exits_2_without_comparing(monkeypatch, capsys):
    """End to end through main: both rows persisted, and the tool still refuses to call it a
    PASS -- and never even loads the rows, because there is nothing worth comparing."""
    m = _tool()
    monkeypatch.setattr(m, "resolve_source", lambda *a, **k: _fake_source())
    monkeypatch.setattr(m, "existing_parity_names", lambda names: [])
    evidence = {"private": _private_evidence(),
                "shared": _evidence(bars_shared_mb=0.0, bars_private_mb=908.5)}
    ran = []

    def fake_run_one(mode, opt_id, rank, name, timeout_min=0.0):
        ran.append(mode)
        return (1700 + len(ran)), evidence[mode], "persisted"

    monkeypatch.setattr(m, "_run_one", fake_run_one)
    monkeypatch.setattr(m, "_load_view", lambda bt_id: pytest.fail("compared despite no evidence"))
    rc = m.main(["--opt", "487", "--rank", "1"])
    out = capsys.readouterr().out
    assert rc == 2
    assert ran == ["private", "shared"]
    assert "INCONCLUSIVE" in out
    assert "PASS" not in out


def test_a_clean_pair_with_good_evidence_passes(monkeypatch, capsys):
    m = _tool()
    monkeypatch.setattr(m, "resolve_source", lambda *a, **k: _fake_source())
    monkeypatch.setattr(m, "existing_parity_names", lambda names: [])
    evidence = {"private": _private_evidence(), "shared": _evidence()}
    monkeypatch.setattr(m, "_run_one",
                        lambda mode, o, r, n, t=0.0: (1700, evidence[mode], "persisted"))
    monkeypatch.setattr(m, "_load_view", lambda bt_id: _view(m))
    rc = m.main(["--opt", "487", "--rank", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "bars 812.5 MB shared" in out      # the evidence is in the report, not just the gate


def test_an_unreadable_option_stat_is_refused_not_reported_as_zero():
    """A defect in the option telemetry (a renamed key, a changed signature) would fire in BOTH
    children, give 0 MB on both sides, compare equal -- and silently PASS the option reference
    runs, which are the runs this evidence exists for."""
    m = _tool()
    broken = _evidence(options_stats_error="ImportError('parquet_options_provider')")
    assert any("option cache stats" in p
               for p in m.evidence_problems(_private_evidence(), broken))
    assert any("option cache stats" in p
               for p in m.evidence_problems(_private_evidence(options_stats_error="boom"),
                                            _evidence()))


def test_zero_trades_on_both_sides_is_vacuous_not_a_pass():
    """Measured in the field: opt 429 (the O_LEAP perf probe) re-ran to 0 trades in BOTH children
    -- its stored bt block builds no options provider -- and the tool printed PASS. Two runs that
    traded nothing are identical whatever the arrays did."""
    m = _tool()
    problems = m.evidence_problems(_private_evidence(total_trades=0),
                                   _evidence(total_trades=0))
    assert any("VACUOUS" in p for p in problems)
    # One side trading is not vacuous -- that is a real (and alarming) difference, and the row
    # comparison is the thing that must report it.
    assert not any("VACUOUS" in p for p in
                   m.evidence_problems(_private_evidence(total_trades=0), _evidence()))


def test_an_option_source_that_loaded_no_option_data_is_refused():
    m = _tool()
    ev = dict(_evidence(options_entries=0, options_provider_built=False))
    problems = m.evidence_problems(_private_evidence(options_entries=0,
                                                     options_provider_built=False),
                                   ev, option_source=True)
    assert any("OPTION strategy" in p for p in problems)
    # The same evidence is fine for an equity source, and fine as soon as one child read a chain.
    assert m.evidence_problems(_private_evidence(), _evidence(), option_source=False) == []
    assert not any("OPTION strategy" in p for p in m.evidence_problems(
        _private_evidence(options_entries=3, options_provider_built=True),
        _evidence(options_entries=3, options_provider_built=True), option_source=True))


@pytest.mark.parametrize("block,expected", [
    # The real shapes, read off the live DB 2026-09-14: an option job is identified by its entry
    # ACTION and its O_* label, never by options_store (every equity opt carries one).
    ({"entry_action": {"action_type": "buy_call", "option_dte_min": 380},
      "labels": ["perfprobe", "O_LEAP"], "options_store": "parquet"}, True),
    ({"entry_action": None, "labels": ["goal2020-notional", "S7"],
      "options_store": "sqlite"}, False),
    ({"strategy": "O_PMCC"}, True),
    ({"strategy": "OS_SOMETHING"}, True),
    ({}, False),
])
def test_an_option_source_is_recognised_without_keying_on_options_store(block, expected):
    assert _tool().is_option_source(block) is expected


# --------------------------------------------------------------------------------------------
# Child streaming: the protocol lines must survive trailing chatter, the parent's stdout must
# survive anything the child logs, and the timeout timer must not fire on a finished child.
# --------------------------------------------------------------------------------------------
def test_a_child_line_the_parents_stdout_cannot_encode_does_not_kill_the_run(monkeypatch):
    """Measured in the field: a parent died at 40 minutes with UnicodeEncodeError forwarding a
    child log line containing '⚡'. Under nohup the parent's stdout is cp1252, and losing an
    hour of work to one glyph is not a trade anyone would make."""
    m = _tool()
    buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp1252"))
    m._emit("    [shared] ⚡ preloaded 98 symbols")
    sys.stdout.flush()
    assert b"preloaded 98 symbols" in buf.getvalue()
def _fake_child(monkeypatch, m, body):
    monkeypatch.setattr(m, "_child_command",
                        lambda mode, opt_id, rank, name: [sys.executable, "-c", body])


def test_protocol_lines_are_captured_while_streaming_not_from_the_tail(monkeypatch, capsys):
    m = _tool()
    body = (f"print({m.EVIDENCE_PREFIX!r} + {json.dumps(_evidence())!r})\n"
            f"print({m.BT_ID_PREFIX!r} + '1701')\n"
            f"[print('chatter %d' % i) for i in range({m._TAIL_LINES} + 50)]\n")
    _fake_child(monkeypatch, m, body)
    bt_id, ev, note = m._run_one("shared", 1, 1, "PARITY-shared-x", timeout_min=5.0)
    assert bt_id == 1701, note
    assert ev == _evidence()
    assert "chatter 0" in capsys.readouterr().out      # and the stream was forwarded live


def test_a_child_that_outlives_its_budget_is_killed(monkeypatch):
    m = _tool()
    _fake_child(monkeypatch, m, "import time; time.sleep(60)")
    bt_id, ev, note = m._run_one("private", 1, 1, "PARITY-private-x", timeout_min=0.02)
    assert bt_id is None and ev is None
    assert "KILLED" in note


# --------------------------------------------------------------------------------------------
# --bt: the rank lives in the archived row's NAME and nowhere else
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("TOP1-scr-small-FMPInsiderClusterBuy-S7-goal2020-notional", 1),
    ("TOP12-sen-S6-goal2020-notional", 12),
    ("BEST-sen-S5-goal2020-risk_atr", "best"),
    ("PARITY-private-TOP1-x", None),          # a parity row is not a source
    ("my manual run", None),
    ("", None),
])
def test_rank_is_read_off_the_row_name(name, expected):
    assert _tool().rank_from_backtest_name(name) == expected


# --------------------------------------------------------------------------------------------
# research metadata (design 8.8) -- excluded from the verdict walk, compared in its own section
# --------------------------------------------------------------------------------------------
_MC_BLOCK = {"profiles": ["ohlcv-v1"], "manifests": {"ohlcv-v1": "sha256:" + "a" * 64},
             "calc_versions": {"ohlcv-v1": "ohlcv-v1/calc-1"}, "timing_policy": "prior_session_v1",
             "stats": {"eligible_recommendations": 12, "market_gate_rejected": 3}}
_ENTRY_STATE = {"symbol": "AAPL", "session": "2020-01-03", "prior_session": "2020-01-02",
                "values": {"underlying_adx_14": {"value": 21.5, "status": "valid"}}}


def _loaded(value):
    return json.loads(value) if isinstance(value, str) else value


def _gated(m, **over):
    """A row as a PROFILE-ON run persists it: the block on ``results``, a state on one trade."""
    row = _fake_row(**over)
    row.results = {**row.results, "market_condition": _MC_BLOCK}
    row.trades = [{**row.trades[0], "entry_state": _ENTRY_STATE}, row.trades[1]]
    return m._row_view(row)


def test_the_gated_extras_are_not_part_of_the_byte_for_byte_verdict():
    """The no-impact gate compares a profile-ON re-run against a profile-OFF archive. Walking
    the block inside the identity comparison would report the FEATURE ITSELF as a failure."""
    m = _tool()
    assert m.compare_rows(_gated(m), _view(m)) == []
    assert m.compare_rows(_gated(m), _gated(m)) == []


def test_a_real_result_difference_is_still_caught_on_a_gated_row():
    """The exclusion is two keys, not a blanket amnesty for a gated run."""
    m = _tool()
    diffs = m.compare_rows(_gated(m), _gated(m, total_return=31.26))
    assert diffs and any("total_return" in d for d in diffs)


def test_the_research_metadata_is_compared_in_its_own_section():
    m = _tool()
    assert m.compare_research_metadata(_gated(m), _gated(m)) == []
    # one row gated, the other not: reported as present/absent, not as a result difference
    diffs = m.compare_research_metadata(_gated(m), _view(m))
    assert sorted(diffs) == ["results.market_condition: present != <absent>",
                             "trades[0].entry_state: present != <absent>"]
    assert m.compare_rows(_gated(m), _view(m)) == []


def test_a_changed_entry_state_behind_identical_trades_is_reported():
    """Identical trades explained by a different measurement is a reader defect, not a nuance."""
    m = _tool()
    other = _gated(m)
    trades = _loaded(other["trades"])
    trades[0] = {**trades[0],
                 "entry_state": {**_ENTRY_STATE,
                                 "values": {"underlying_adx_14": {"value": 99.0,
                                                                  "status": "valid"}}}}
    other["trades"] = json.dumps(trades)
    diffs = m.compare_research_metadata(_gated(m), other)
    assert len(diffs) == 1 and diffs[0].startswith("trades[0].entry_state:")
    assert "21.5" in diffs[0] and "99.0" in diffs[0]


def test_a_changed_market_condition_block_is_reported_but_not_as_a_result_difference():
    m = _tool()
    other = _gated(m)
    block = {**_MC_BLOCK, "stats": {**_MC_BLOCK["stats"], "market_gate_rejected": 4}}
    other["results"] = json.dumps({**_loaded(other["results"]), "market_condition": block})
    assert m.compare_rows(_gated(m), other) == []
    diffs = m.compare_research_metadata(_gated(m), other)
    assert len(diffs) == 1 and diffs[0].startswith("results.market_condition:")


def test_an_entry_state_OUTSIDE_the_trade_blob_is_still_compared():
    """M3 (final review). ``entry_state`` used to be dropped at ANY depth, so an unrelated
    ``entry_state`` a future writer puts somewhere the verdict is supposed to cover -- on
    ``results``, in the config -- would vanish from the comparison: silently narrowing the
    verdict instead of moving one trade field out of it. It is scoped to the trade blob now.
    """
    m = _tool()
    a, b = _gated(m), _gated(m)
    a["results"] = json.dumps({**_loaded(a["results"]), "entry_state": "left"})
    b["results"] = json.dumps({**_loaded(b["results"]), "entry_state": "right"})
    diffs = m.compare_rows(a, b)
    assert diffs and any("entry_state" in d for d in diffs), diffs

    # ... while the TRADE-level one stays out of the verdict (its own section reports it).
    assert m.compare_rows(_gated(m), _view(m)) == []
    assert m._RESEARCH_KEYS == ("market_condition",)
    assert m._SCOPED_RESEARCH_KEYS == {"trades": ("entry_state",)}


def test_the_market_condition_block_is_still_dropped_at_any_depth():
    """It is reached through both ``results`` and the run config, and nothing else ever carries
    that name -- so it stays depth-free while entry_state does not."""
    m = _tool()
    a, b = _gated(m), _gated(m)
    a["optimization_config"] = json.dumps({"backtest": {"market_condition": {"profiles": ["x"]}}})
    b["optimization_config"] = json.dumps({"backtest": {"market_condition": {"profiles": ["y"]}}})
    assert m.compare_rows(a, b) == []
