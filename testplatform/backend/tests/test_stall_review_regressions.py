"""Regressions for the 2026-09-21 stall-recovery review (G1 / G2 / escalation).

The first stall patch let the GA survive a wedged trial. The review found two ways that same
recovery could still take the grid down, plus a teardown that could not kill an uncooperative
worker:

G1  Top-N export selected the STALLED record as a candidate and re-ran it -- with no timeout of its
    own -- i.e. it could hang on exactly the trial the guard had just abandoned, AFTER a successful
    recovery.
G2  A stall-only search was finalised as ``completed`` (because ``all_results`` was non-empty) and
    cleared its checkpoint, exporting a "winner" that never ran.

These tests exercise the decision functions the fixes are built on -- so a regression fails here
instead of in a multi-hour grid run -- plus the SIGKILL escalation.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _root not in sys.path:
    sys.path.insert(0, _root)

handler = importlib.import_module("app.services.strategy_optimization_handler")
fitness = importlib.import_module("app.services.strategy_fitness")

_LAUNCHER_PATH = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _LAUNCHER_PATH)
_launcher_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("ba2test_launcher", _launcher_mod)
_spec.loader.exec_module(_launcher_mod)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# --- G2: completion is decided by MEASURED results, never by a non-empty all_results -----------

def test_final_status_completed_requires_at_least_one_measurement():
    assert handler._final_status([{"fitness": 1.0}]) == "completed"


def test_records_without_a_status_count_as_measured():
    """Both append sites produce plain records (no 'status' key). The first cut of this fix kept a
    hand-maintained counter, missed the local-trial append site, and reported "11 record(s),
    0 measured" -- plus an UnboundLocalError on the path that never incremented it. The count is
    derived from the records instead, and this pins that."""
    recs = [{"fitness": 1.0, "key": "k1"},
            {"fitness": 0.5, "key": "k2"},
            {"fitness": fitness.STALLED_SENTINEL, "status": "stalled", "key": "k3"}]
    assert handler._count_measured(recs) == 2
    assert handler._final_status(recs) == "completed"


def test_final_status_all_stalled_is_not_completed():
    """The exact G2 case: records exist, none of them was measured."""
    recs = [{"fitness": fitness.STALLED_SENTINEL, "status": "stalled"},
            {"fitness": fitness.STALLED_SENTINEL, "status": "stalled"}]
    assert handler._count_measured(recs) == 0
    assert handler._final_status(recs) == "no_measurements"


def test_final_status_with_no_records_is_a_plain_failure():
    assert handler._final_status([]) == "failed"


def test_no_measurement_marker_is_identical_in_handler_and_matrix_driver():
    """The driver keys off a string the handler writes. Drift between the two literals would
    silently reintroduce the campaign-wide stop the marker exists to prevent."""
    src = (_REPO_ROOT / "tools" / "run_options_matrix.py").read_text(encoding="utf-8")
    m = re.search(r'^NO_MEASUREMENT_MARKER = "([^"]+)"', src, re.M)
    assert m, "tools/run_options_matrix.py lost its NO_MEASUREMENT_MARKER"
    assert m.group(1) == handler.NO_MEASUREMENT_MARKER


def test_the_matrix_driver_skips_a_no_measurement_job_instead_of_stopping():
    src = (_REPO_ROOT / "tools" / "run_options_matrix.py").read_text(encoding="utf-8")
    assert "reason.startswith(NO_MEASUREMENT_MARKER)" in src
    assert "_failure_reason(name)" in src


# --- G1: Top-N export must never re-run a non-measurement --------------------------------------

def test_topn_selection_skips_stalled_records_and_returns_fewer_candidates():
    recs = [{"params": {"a": 1}, "key": "k1", "fitness": 2.0},
            {"params": {"a": 2}, "key": "k2", "fitness": 1.0},
            {"params": {"a": 3}, "key": "k3", "fitness": 0.0},
            {"params": {"a": 4}, "key": "k4", "fitness": -1.0},
            {"params": {"a": 5}, "key": "k5", "fitness": fitness.STALLED_SENTINEL,
             "status": "stalled"}]
    ranked, skipped = _launcher_mod._rank_measured_candidates(
        recs, 5, {"a": 5}, fitness.STALLED_SENTINEL)
    assert skipped == 1
    assert [r[2] for r in ranked] == [2.0, 1.0, 0.0, -1.0]  # FOUR, not five
    assert all(f != fitness.STALLED_SENTINEL for _, _, f in ranked)


def test_topn_selection_of_an_all_stalled_search_returns_nothing_not_the_sentinel():
    """The best_params fallback must not resurrect the non-measurement it was selected from."""
    recs = [{"params": {"a": 1}, "key": "k1", "fitness": fitness.STALLED_SENTINEL,
             "status": "stalled"}]
    ranked, skipped = _launcher_mod._rank_measured_candidates(
        recs, 5, {"a": 1}, fitness.STALLED_SENTINEL)
    assert ranked == []
    assert skipped == 1


def test_topn_selection_still_dedups_on_fitness():
    recs = [{"params": {"a": 1}, "key": "k1", "fitness": 2.0},
            {"params": {"a": 2}, "key": "k2", "fitness": 2.0},  # inert-gene twin: same score
            {"params": {"a": 3}, "key": "k3", "fitness": 1.0}]
    ranked, skipped = _launcher_mod._rank_measured_candidates(recs, 5, None, None)
    assert [r[2] for r in ranked] == [2.0, 1.0]
    assert skipped == 0


def test_topn_selection_falls_back_to_best_params_when_the_search_is_thin():
    ranked, skipped = _launcher_mod._rank_measured_candidates([], 5, {"a": 9}, 3.5)
    assert ranked == [({"a": 9}, None, 3.5)]
    assert skipped == 0


def test_export_rerun_is_bounded_and_never_waits_on_a_hung_worker():
    """The export used to be `with ProcessPoolExecutor(...)` + `as_completed(futs)` -- no timeout,
    and a blocking shutdown that would hang on the very worker being abandoned."""
    src = (_REPO_ROOT / "testplatform" / "ba2test_launcher.py").read_text(encoding="utf-8")
    export = src.split("def _persist_top_backtests")[1]
    assert "as_completed(futs, timeout=export_timeout)" in export
    assert "except _FutureTimeout:" in export
    assert "_kill_executor(local_ex)" in export
    assert "with ProcessPoolExecutor(" not in export
    assert "as_completed(futs):" not in export


# --- escalation: a worker that ignores SIGTERM must still die ---------------------------------

def _sigterm_ignoring_sleeper(seconds: int) -> str:
    import signal as _signal
    import time as _time
    _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)  # refuses to die politely
    _time.sleep(seconds)
    return "never"


def _quick(value: str) -> str:
    return value


def test_abandon_slot_sigkills_a_worker_that_ignores_sigterm():
    pools = handler._SlotPools(lambda: ProcessPoolExecutor(max_workers=1), 1, 0)
    try:
        fut = pools.submit(0, _sigterm_ignoring_sleeper, 300)
        procs = list((getattr(pools.pools[0], "_processes", None) or {}).values())
        assert procs, "the pool had no worker process to kill"

        t0 = time.monotonic()
        assert pools.abandon_slot(fut) == 0
        elapsed = time.monotonic() - t0

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and any(p.is_alive() for p in procs):
            time.sleep(0.1)
        assert not any(p.is_alive() for p in procs), (
            "a SIGTERM-ignoring worker survived abandon_slot -- the slot would stay dead")
        assert elapsed < 30.0, f"abandon_slot took {elapsed:.1f}s; the kill grace is not bounded"
        # the slot must be usable immediately afterwards
        assert pools.submit(0, _quick, "ok").result(timeout=60) == "ok"
    finally:
        pools.shutdown(wait=False, cancel_futures=True)


def test_kill_executor_terminates_without_waiting():
    ex = ProcessPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_sigterm_ignoring_sleeper, 300)
        procs = list((getattr(ex, "_processes", None) or {}).values())
        assert procs
        t0 = time.monotonic()
        _launcher_mod._kill_executor(ex)
        elapsed = time.monotonic() - t0
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and any(p.is_alive() for p in procs):
            time.sleep(0.1)
        assert not any(p.is_alive() for p in procs)
        assert elapsed < 30.0
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
