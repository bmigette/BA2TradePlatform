"""Regressions for the 2026-09-21 stall-recovery RECHECK (H1-H5).

The first stall patch (G1/G2) was correct for newly written records. The recheck found five
defects that survived it, all on the failure paths:

H1  A timed-out REMOTE export could still hold the launcher open: the remote attempts ran in a
    ThreadPoolExecutor (whose threads Python JOINS at exit and `shutdown(wait=False)` cannot stop),
    and after two remote failures the local fallback ran INLINE in that thread -- starting fresh
    work after the export deadline had already dropped the rank.
H2  Old-format stalled records (the sentinel, NO status -- what the previous patch wrote) counted
    as measured, so an all-stalled checkpoint was finalised `completed` and cleared.
H3  The handler's `no_measurements` status was not `failed`, so the main task queue marked the job
    COMPLETED (progress 100, no error) while its optimization row said failed.
H4  The matrix driver read the newest row with the same job NAME, so a stale no-measurement marker
    hid a fresh launch failure that wrote no row at all.
H5  The extracted ranking helper called an out-of-scope `_json`, so a record with a missing fitness
    raised NameError instead of being excluded.

Each test exercises the decision function the fix is built on, plus the process-level property H1
is actually about.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
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
_MATRIX_PATH = str(_REPO_ROOT / "tools" / "run_options_matrix.py")
_mspec = importlib.util.spec_from_file_location("run_options_matrix", _MATRIX_PATH)
_matrix_mod = importlib.util.module_from_spec(_mspec)
sys.modules.setdefault("run_options_matrix", _matrix_mod)
_mspec.loader.exec_module(_matrix_mod)

_SENTINEL = fitness.STALLED_SENTINEL


# --- H2/H5: ONE measurement predicate, shared by counting and ranking --------------------------

class TestOneMeasurementPredicate:
    @pytest.mark.parametrize('score', [float('nan'), float('inf'), float('-inf'), True, False,
                                      pytest.param(10 ** 400, id='overflowing_integer')])
    def test_nonfinite_scores_and_booleans_are_not_measurements(self, score):
        assert fitness.is_measured_result({'params': {}, 'fitness': score}) is False

    @pytest.mark.parametrize('score', [fitness.ZERO_TRADE_SENTINEL, fitness.LOW_TRADE_SENTINEL,
                                      fitness.WIPED_OUT_SENTINEL, 0.0, -2.5, 10.0])
    def test_finite_observed_outcomes_remain_measurements(self, score):
        assert fitness.is_measured_result({'params': {}, 'fitness': score}) is True

    def test_an_old_format_stalled_record_is_not_a_measurement(self):
        # What the patch BEFORE the recheck wrote: the sentinel, and no status field at all.
        assert fitness.is_measured_result({"params": {"a": 1}, "fitness": _SENTINEL}) is False

    def test_a_status_marked_stalled_record_is_not_a_measurement(self):
        assert fitness.is_measured_result({"params": {}, "fitness": 1.0, "status": "stalled"}) is False

    def test_an_ordinary_historical_record_stays_measured(self):
        # Older checkpoints: a numeric fitness, no status. They must keep counting.
        assert fitness.is_measured_result({"params": {}, "fitness": 1.25}) is True

    def test_a_missing_or_non_numeric_fitness_is_not_a_measurement(self):
        assert fitness.is_measured_result({"params": {}, "fitness": None}) is False
        assert fitness.is_measured_result({"params": {}}) is False
        assert fitness.is_measured_result({"params": {}, "fitness": "high"}) is False

    def test_a_non_dict_is_not_a_measurement(self):
        assert fitness.is_measured_result(None) is False
        assert fitness.is_measured_result("stalled") is False

    def test_counting_and_ranking_now_agree_on_an_old_format_record(self):
        """The recheck's exact repro: counting said `completed`, ranking said "no candidate"."""
        records = [{"params": {"a": 1}, "fitness": _SENTINEL}]
        assert handler._count_measured(records) == 0
        assert handler._final_status(records) == "no_measurements"
        ranked, skipped = _launcher_mod._rank_measured_candidates(records, 5, None, None)
        assert ranked == []
        assert skipped == 1


# --- H5: ranking tolerates a missing fitness instead of raising --------------------------------

class TestRankingToleratesMissingFitness:
    def test_records_without_a_usable_genome_are_not_exported(self):
        records = [{'fitness': 100.0}, {'params': None, 'fitness': 200.0},
                   {'params': 'bad', 'fitness': 300.0}, {'params': {}, 'fitness': 1.0}]
        ranked, skipped = _launcher_mod._rank_measured_candidates(records, 5, None, None)
        assert ranked == [({}, None, 1.0)]
        assert skipped == 3

    def test_invalid_best_genome_is_not_exported(self):
        assert _launcher_mod._rank_measured_candidates([], 5, 'bad', 1.0)[0] == []

    def test_malformed_records_are_filtered_before_sorting_and_counting(self):
        invalid = [None, 'diagnostic', {'fitness': 'high'}, {'fitness': True},
                   {'fitness': float('nan')}, {'fitness': float('inf')}]
        records = invalid + [{'params': {'x': 1}, 'fitness': 3.0},
                             {'params': {'x': 2}, 'fitness': 9.0}]
        ranked, skipped = _launcher_mod._rank_measured_candidates(records, 1, None, None)
        assert ranked == [({'x': 2}, None, 9.0)]
        assert skipped == len(invalid)
        assert handler._count_measured(records) == 2
        assert handler._final_status(invalid) == 'no_measurements'

    @pytest.mark.parametrize('score', [None, 'high', True, float('nan'), float('inf'),
                                      float('-inf'), _SENTINEL])
    def test_best_params_cannot_bypass_measurement_validation(self, score):
        ranked, _ = _launcher_mod._rank_measured_candidates([], 5, {'x': 1}, score)
        assert ranked == []

    def test_a_null_fitness_no_longer_raises(self):
        # Before: NameError from `_json.dumps` in the dedup key.
        ranked, skipped = _launcher_mod._rank_measured_candidates(
            [{"params": {"a": 1}, "fitness": None}], 5, None, None)
        assert ranked == []
        assert skipped == 1

    def test_mixed_valid_and_invalid_rows_rank_only_the_valid(self):
        records = [
            {"params": {"a": 1}, "fitness": None},
            {"params": {"b": 2}, "fitness": 3.0},
            {"params": {"c": 3}, "fitness": _SENTINEL},
            {"params": {"d": 4}, "fitness": 7.5},
        ]
        ranked, skipped = _launcher_mod._rank_measured_candidates(records, 5, None, None)
        assert [row[2] for row in ranked] == [7.5, 3.0]
        assert skipped == 2

    def test_the_best_params_fallback_still_refuses_a_stalled_best(self):
        ranked, _ = _launcher_mod._rank_measured_candidates([], 5, {"x": 1}, _SENTINEL)
        assert ranked == []

    def test_the_best_params_fallback_still_uses_a_real_best(self):
        ranked, _ = _launcher_mod._rank_measured_candidates([], 5, {"x": 1}, 4.2)
        assert ranked == [({"x": 1}, None, 4.2)]


# --- H3: the failure contract every consumer already understands -------------------------------

class TestNoMeasurementOutcomeIsAFailure:
    def test_the_kind_exists_and_is_not_a_new_success_status(self):
        assert handler.NO_MEASUREMENT_KIND == "no_measurements"
        # The status must stay inside the contract the queue/UI already treat as failure.
        assert handler.NO_MEASUREMENT_KIND != "failed"

    def test_the_queue_marks_a_no_measurement_result_failed(self, tmp_path, monkeypatch):
        """The recheck ran the actual inline processor: it set the task COMPLETED, progress 100,
        with no error, while the optimization row said failed."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import app.services.task_queue as task_queue
        from app.models.database import Base
        from app.models.task_queue import TaskQueue, TaskStatus

        engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        service = task_queue.TaskQueueService(use_subprocess=False)
        service.register_handler("demo", lambda task_id, payload: {
            "status": "failed",
            "failure_kind": handler.NO_MEASUREMENT_KIND,
            "error": f"{handler.NO_MEASUREMENT_MARKER}: 3 record(s), 3 stalled and 0 measured.",
        })

        session = Session()
        session.add(TaskQueue(task_id="t1", task_type="demo", name="demo task", payload={},
                              status=TaskStatus.RUNNING.value, progress=10.0))
        session.commit()
        session.close()

        monkeypatch.setattr(task_queue, "SessionLocal", Session)
        task = Session().query(TaskQueue).filter(TaskQueue.task_id == "t1").first()
        service._process_task_inline(task, "test-worker")

        done = Session().query(TaskQueue).filter(TaskQueue.task_id == "t1").first()
        assert done.status == TaskStatus.FAILED.value, "a no-measurement job must not look successful"
        assert done.error_message and handler.NO_MEASUREMENT_MARKER in done.error_message

    def test_the_cli_recognises_both_the_kind_and_the_legacy_status(self):
        source = Path(_LAUNCHER_PATH).read_text(encoding="utf-8")
        assert 'res.get("failure_kind") == "no_measurements"' in source
        assert 'res.get("status") == "no_measurements"' in source


# --- H4: a marker only counts when THIS launch wrote it ---------------------------------------

def _seed_optimizations(path: Path, rows) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE strategy_optimizations "
                       "(id INTEGER PRIMARY KEY, name TEXT, error_message TEXT)")
    connection.executemany("INSERT INTO strategy_optimizations (id, name, error_message) "
                           "VALUES (?, ?, ?)", rows)
    connection.commit()
    connection.close()


class TestFailureMarkerIsBoundToTheAttempt:
    def test_an_older_marker_does_not_speak_for_a_new_launch(self, tmp_path, monkeypatch):
        db = tmp_path / "opt.db"
        _seed_optimizations(db, [(7, "optm-x-grid", f"{_matrix_mod.NO_MEASUREMENT_MARKER}: 3 stalled")])
        monkeypatch.setenv("DB_FILE", str(db))
        # The launch that just failed created no row: the only marker is the OLD one (id 7).
        assert _matrix_mod._newest_optimization_id() == 7
        assert _matrix_mod._failure_reason("optm-x-grid", created_after=7) == ""
        assert _matrix_mod._classify_failure("optm-x-grid", 7) == "stop"

    def test_a_marker_this_launch_wrote_still_skips(self, tmp_path, monkeypatch):
        db = tmp_path / "opt.db"
        _seed_optimizations(db, [
            (7, "optm-x-grid", f"{_matrix_mod.NO_MEASUREMENT_MARKER}: 3 stalled"),
            (8, "optm-x-grid", f"{_matrix_mod.NO_MEASUREMENT_MARKER}: 4 stalled"),
        ])
        monkeypatch.setenv("DB_FILE", str(db))
        assert _matrix_mod._classify_failure("optm-x-grid", 7) == "skip"

    def test_a_new_row_with_a_real_error_stops(self, tmp_path, monkeypatch):
        db = tmp_path / "opt.db"
        _seed_optimizations(db, [(8, "optm-x-grid", "every backtest failed: OHLCV cache miss")])
        monkeypatch.setenv("DB_FILE", str(db))
        assert _matrix_mod._classify_failure("optm-x-grid", 7) == "stop"

    def test_an_unreadable_table_stops_rather_than_skipping(self):
        # No prior id could be established -> the marker cannot be attributed -> stop.
        assert _matrix_mod._classify_failure("optm-x-grid", None) == "stop"

    def test_the_name_based_lookup_is_still_available_for_legacy_callers(self, tmp_path, monkeypatch):
        db = tmp_path / "opt.db"
        _seed_optimizations(db, [(7, "optm-x-grid", "boom")])
        monkeypatch.setenv("DB_FILE", str(db))
        assert _matrix_mod._failure_reason("optm-x-grid") == "boom"


# --- H1: a timed-out attempt must not keep the launcher alive ----------------------------------

class TestExportIsActuallyBounded:
    def test_the_real_export_pool_starts_in_the_backend_directory(self):
        # Persistence unit tests mock the executor; this protects the real factory and
        # initializer, including the import-scope regression that motivated 7f2f58a5.
        pool = _launcher_mod._new_local_pool(1)
        try:
            assert Path(pool.submit(os.getcwd).result(timeout=60)).resolve() == Path(_root).resolve()
        finally:
            _launcher_mod._kill_executor(pool)
            pool.shutdown(wait=False, cancel_futures=True)

    def test_the_real_fallback_pool_is_killed_at_its_deadline(self, monkeypatch):
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing

        pools = []

        def new_pool(n):
            pool = ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context('spawn'))
            pools.append(pool)
            return pool

        monkeypatch.setattr(_launcher_mod, '_new_local_pool', new_pool)
        monkeypatch.setattr(handler, '_persist_trial_worker', time.sleep)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match='remaining export budget'):
                _launcher_mod._run_local_fallback_bounded(20, deadline=started + 0.5)
            assert time.monotonic() - started < 10
            assert pools
        finally:
            for pool in pools:
                _launcher_mod._kill_executor(pool)
                pool.shutdown(wait=False, cancel_futures=True)

    def test_a_past_deadline_refuses_to_start_the_local_fallback(self, monkeypatch):
        started = []
        monkeypatch.setattr(_launcher_mod, "_run_local_fallback_bounded",
                            lambda *args, **kwargs: started.append(args))
        with pytest.raises(TimeoutError):
            _launcher_mod._remote_then_local({"name": "w1"}, {"x": 1}, "metric",
                                             deadline=time.monotonic() - 1)
        assert started == [], "a dropped rank must not start a fresh backtest"

    def test_the_bounded_fallback_refuses_past_the_deadline(self):
        with pytest.raises(TimeoutError):
            _launcher_mod._run_local_fallback_bounded({"x": 1}, deadline=time.monotonic() - 1)

    def test_the_retry_is_skipped_when_the_backoff_would_overrun_the_deadline(self, monkeypatch):
        """The 5s retry backoff used to run even with no budget left, then start the fallback."""
        attempts = []
        monkeypatch.setattr(_launcher_mod, "_REMOTE_RETRY_BACKOFF_S", 5.0)
        monkeypatch.setattr(_launcher_mod, "_run_local_fallback_bounded",
                            lambda *args, **kwargs: {"fallback": True})
        worker_client = importlib.import_module("app.services.worker_client")
        monkeypatch.setattr(worker_client, "run_trial_full",
                            lambda *args, **kwargs: attempts.append(1) or (_ for _ in ()).throw(
                                RuntimeError("remote down")))

        def _no_sleep(_seconds):
            raise AssertionError("must not sleep away the remaining budget")

        monkeypatch.setattr(time, "sleep", _no_sleep)
        out = _launcher_mod._remote_then_local({"name": "w1"}, {"x": 1}, "metric",
                                               deadline=time.monotonic() + 1.0)
        assert out == {"fallback": True}
        assert attempts == [1], "the retry must not be attempted when the backoff overruns"

    def test_remote_attempts_run_in_daemon_threads(self):
        """Not a ThreadPoolExecutor: Python 3.9+ JOINS those at exit, which is how a stuck remote
        call kept the launcher open. The Future API is kept so as_completed/done() still work."""
        import threading
        before = {t.name for t in threading.enumerate()}
        release = threading.Event()
        future = _launcher_mod._submit_daemon(lambda: (release.wait(5), "done")[1])
        assert future.done() is False
        new = [t for t in threading.enumerate() if t.name not in before]
        assert new and all(t.daemon for t in new), "the attempt must not block interpreter exit"
        release.set()
        assert future.result(timeout=5) == "done"

    def test_the_process_exits_with_a_stuck_remote_attempt_in_flight(self, tmp_path):
        """The recheck's P1, at process level: with the OLD code this script hung (the parent probe
        had to kill its own subprocess)."""
        script = tmp_path / "stuck_export.py"
        script.write_text(textwrap.dedent(f'''
            import importlib.util, sys, time
            spec = importlib.util.spec_from_file_location("ba2test_launcher", r"{_LAUNCHER_PATH}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["ba2test_launcher"] = mod
            spec.loader.exec_module(mod)

            def never_returns():
                time.sleep(600)

            fut = mod._submit_daemon(never_returns)
            time.sleep(0.5)
            assert not fut.done()
            print("exiting with a stuck attempt in flight", flush=True)
        '''), encoding="utf-8")
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                              timeout=120)
        elapsed = time.monotonic() - started
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert "exiting with a stuck attempt in flight" in proc.stdout
        assert elapsed < 90, f"the process waited for the stuck attempt ({elapsed:.1f}s)"
