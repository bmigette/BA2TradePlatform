"""``tests_scripts/bench_market_conditions.py --quick`` -- the benchmark's own regression.

A benchmark nobody runs between measurements rots into a script that raises on the day it is
needed, which is always the day a performance question is urgent. ``--quick`` fabricates its
own store and shrinks every phase, so the whole harness -- coverage selection, both observe
paths, the real condition class, and the spawn-pool worker measurement -- executes here in a
second with no caches, no database and no provider.

It also pins the two claims the benchmark exists to make, on the fabricated store where they
are checkable: with a manifest pinned a reader COMPUTES NOTHING, and the trial-path reader is
the one without window retention (the retaining one re-reads and re-hashes the raw shard, and
reporting its milliseconds as a trial cost would be wrong by three orders of magnitude).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from datetime import date

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_BACKEND, "tests_scripts", "bench_market_conditions.py")


def _load():
    # The worker phase SPAWNS children, and a spawned child re-imports the target's module BY
    # NAME. Loading the script purely by file location leaves that name unresolvable in the
    # child, which then dies on the import and the parent waits out its whole start timeout.
    scripts = os.path.dirname(_SCRIPT)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("bench_market_conditions", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_market_conditions"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench():
    return _load()


@pytest.fixture(autouse=True)
def _restore_logging():
    """``main()`` calls ``logging.disable`` -- deliberately, for a standalone run. It must not
    outlive the test that invoked it."""
    yield
    logging.disable(logging.NOTSET)


def test_quick_runs_every_phase_and_writes_its_json(bench, tmp_path, monkeypatch):
    out = tmp_path / "bench.json"
    monkeypatch.chdir(tmp_path)
    assert bench.main(["--quick", "--out", str(out), "--workers", "3"]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["quick"] is True
    assert result["coverage"]["chosen"] and not result["coverage"]["uncovered"]
    assert set(result["observe"]) == {"trial_path", "capture_path"}
    assert result["gate"]["evaluate"]["n"] > 0
    assert result["workers"]["workers"] == 3
    # A child we could not measure records None, so ``> 0`` alone would raise a TypeError
    # instead of reporting the real problem -- assert the measurement EXISTS first.
    assert result["workers"]["rss_mb_max"] is not None, result["workers"]
    assert result["workers"]["rss_mb_max"] > 0
    assert result["workers"]["unmeasurable"] == 0
    # "trial" needs a persisted genome and the option caches; --quick drops it rather than
    # pretending to have measured it.
    assert "trial" not in result


def test_a_pinned_manifest_means_the_reader_computes_nothing(bench, tmp_path):
    """The contract the whole feature store exists for: a trial does an indexed lookup, never
    a calculation. ``computed`` is the counter that can prove it."""
    from ba2_common.core.market_calendar import regular_sessions_ending_at
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    sessions = regular_sessions_ending_at(date(2024, 6, 28), 10)
    digest = bench.fabricate_store(str(tmp_path), ["AAA", "BBB"], sessions)
    reader = MappedMarketConditionReader(str(tmp_path), digest, "ohlcv-v1")
    result = bench.phase_observe(reader, ["AAA", "BBB"], sessions, repeats=2)
    for path in ("trial_path", "capture_path"):
        assert result[path]["computed"] == 0, path
        assert result[path]["mapped_rows"] == 20, path
        assert result[path]["rows_absent"] == 0, path
        # A hit must be MUCH cheaper than a miss, or the memo is doing nothing. A bare ``<``
        # would pass on a 1% difference, which is what a broken memo looks like under timer
        # noise; on this store the real ratio is ~20x, so half is a floor with margin, not a
        # threshold tuned to the number it happens to produce.
        assert result[path]["hit"]["p50_us"] <= result[path]["miss"]["p50_us"] / 2


def test_the_gate_phase_goes_through_the_real_condition_and_restores_the_seam(bench, tmp_path):
    import ba2_common.core.TradeConditions as TC
    from ba2_common.core.market_calendar import regular_sessions_ending_at
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    sessions = regular_sessions_ending_at(date(2024, 6, 28), 10)
    digest = bench.fabricate_store(str(tmp_path), ["AAA"], sessions)
    reader = MappedMarketConditionReader(str(tmp_path), digest, "ohlcv-v1")
    before = TC.get_market_condition_context_resolver()
    result = bench.phase_gate(reader, ["AAA"], sessions)
    assert TC.get_market_condition_context_resolver() is before
    assert result["evaluate"]["n"] == 20
    # One resolver call per evaluation -- the counter the no-impact gate reads.
    assert result["resolver_calls"] == 20
    assert result["computed"] == 0


def test_the_coverage_phase_reports_what_the_snapshot_does_not_serve(bench, tmp_path):
    from ba2_common.core.market_calendar import regular_sessions_ending_at
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    sessions = regular_sessions_ending_at(date(2024, 6, 28), 5)
    digest = bench.fabricate_store(str(tmp_path), ["AAA", "BBB"], sessions)
    reader = MappedMarketConditionReader(str(tmp_path), digest, "ohlcv-v1")
    coverage = bench.phase_coverage(reader, ["AAA", "ZZZ", "BBB"], wanted=10)
    assert coverage["chosen"] == ["AAA", "BBB"]
    assert coverage["uncovered"] == ["ZZZ"]
    # ...and the bench only ever benches what is covered, so a missing symbol cannot flatter
    # the latency numbers by returning None in nanoseconds.
    assert "ZZZ" not in coverage["chosen"]


def _launcher():
    import importlib.util

    path = os.path.join(os.path.dirname(_BACKEND), "ba2test_launcher.py")
    spec = importlib.util.spec_from_file_location("bench_test_launcher", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_test_launcher"] = module
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    return module


def test_the_trial_gates_are_the_launchers_own_leaves_decoded(bench):
    """A bench that timed a hand-written leaf would measure a condition the grid never runs.

    So the leaf is built by ``_market_condition_gates`` and decoded by ``_apply_mode`` -- the
    two functions the grid itself uses -- and this asserts the RESULT of that, not the contents
    of the constant that names it.
    """
    launcher = _launcher()
    leaf = bench._decoded_gate(launcher, "o_lc", "adx", "below", 25.0)
    assert leaf["id"] == "o_lc-market-adx"
    assert leaf["field"] == "underlying_adx_14"
    assert leaf["op"] == leaf["comparison"] == "<"        # below -> strict less-than
    assert leaf["value"] == 25.0
    assert leaf["mode"] == "below"
    # A DECODED leaf is a rule, not a template: the search metadata must be gone or the
    # deploy exporter would refuse it as an unresolved gene.
    assert "mode_optimize" not in leaf and "mode_choices" not in leaf
    # ...and the module is left as it was found.
    assert getattr(launcher, "_MARKET_CONDITION_PROFILES", ()) == ()


def test_the_gates_land_on_the_entry_rules_AND_tree(bench):
    launcher = _launcher()
    rules = [{"id": "entry", "conditions": {"operator": "AND", "conditions": [
        {"id": "has_no_position", "field": "has_no_position"}]}}]
    assert bench._append_gates(rules, launcher, "o_lc") == len(bench.TRIAL_GATES)
    leaves = rules[0]["conditions"]["conditions"]
    assert len(leaves) == 1 + len(bench.TRIAL_GATES)
    assert [leaf["id"] for leaf in leaves[1:]] == [f"o_lc-market-{short}"
                                                   for short, _, _ in bench.TRIAL_GATES]
    # Appended at the END, so the evaluator's first-match short-circuit reaches them last --
    # the same placement the launcher uses.
    assert leaves[0]["id"] == "has_no_position"


def test_a_rule_with_no_group_to_append_to_is_refused(bench):
    launcher = _launcher()
    with pytest.raises(SystemExit):
        bench._append_gates([{"id": "entry", "conditions": {"field": "x"}}], launcher, "o_lc")
    with pytest.raises(SystemExit):
        bench._append_gates([], launcher, "o_lc")


def test_the_manifest_is_required_without_quick(bench):
    with pytest.raises(SystemExit):
        bench.main(["--phases", "observe"])


def test_quick_leaves_no_fabricated_store_behind(bench, tmp_path, monkeypatch):
    """The failure mode is cumulative and invisible: every attempt used to leave a
    ``bench-mc-*`` tree, so a benchmark run repeatedly filled the disk it was measuring."""
    import glob
    import tempfile

    monkeypatch.chdir(tmp_path)
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "bench-mc-*")))
    bench.main(["--quick", "--out", str(tmp_path / "b.json"), "--workers", "2"])
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "bench-mc-*"))) == before


def test_a_child_that_cannot_open_the_mapping_leaves_no_live_process(bench, tmp_path):
    """THE LEAK THIS CLOSES. The children block waiting to be measured; when anything before
    the release raises -- here every child dies on a manifest that is not there -- the release
    used to be skipped and the spawned processes stayed resident for the life of the parent.

    Asserted on the PROCESS TABLE, not on the return value: a cleanup that only sets a flag
    would satisfy any weaker check."""
    import psutil

    me = psutil.Process()
    before = {p.pid for p in me.children(recursive=True)}
    with pytest.raises(Exception):
        bench.phase_workers(str(tmp_path), "sha256:" + "0" * 64, "ohlcv-v1", "AAA",
                            date(2024, 6, 28), workers=2, ready_timeout=3.0)
    survivors = [p for p in me.children(recursive=True)
                 if p.pid not in before and p.is_alive()]
    assert survivors == [], [(p.pid, p.name()) for p in survivors]


def test_the_arithmetic_bound_multiplies_the_measured_costs_by_the_operation_count(bench):
    """The number the report quotes, because the A/B difference sits inside the rig's noise."""
    observe = {"trial_path": {"miss": {"p50_us": 10.0}}}
    gate = {"evaluate": {"p50_us": 1.0}}
    trial = {"symbols": 20, "gates": 3, "off": {"median_s": 10.0}}
    bound = bench.arithmetic_bound(observe, gate, trial, sessions=500)
    assert bound["memo_misses"] == 10_000          # 20 symbols x 500 sessions
    assert bound["evaluations"] == 30_000          # ...x 3 leaves
    assert bound["seconds"] == pytest.approx((10_000 * 10.0 + 30_000 * 1.0) / 1e6)
    assert bound["pct"] == pytest.approx(bound["seconds"] / 10.0 * 100.0)
    # The inputs travel with the answer, so the number can be re-derived from the JSON alone.
    assert bound["observe_miss_p50_us"] == 10.0 and bound["evaluate_p50_us"] == 1.0
    assert bound["trial_median_s"] == 10.0
