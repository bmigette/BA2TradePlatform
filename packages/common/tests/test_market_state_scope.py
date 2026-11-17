"""Where the per-bar market state lives: a backtest's run scope, or the process-global (live).

* LIVE (no scope): the publisher thread and every reader thread share the process-global, exactly
  as before -- TradeManager publishes on the refresh / entry-pass thread, the recommendation worker
  threads read it.
* BACKTEST (scope): publish/read/reset reach the run's own state; a fresh scope holds nothing
  published; the process-global is neither read nor written; two scopes on two threads never see
  each other; work submitted with the run's context copied (``capture_aware_submit``) sees the run.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core import market_regime as mr
from ba2_common.core import regime_overlay as ro
from ba2_common.core.market_state_scope import current_run_state, run_market_state_scope
from ba2_common.core.replay.context import capture_aware_submit

DOWN = br.BenchmarkRegime(mr.TREND_DOWN, mr.STRESSED, "", "", date(2024, 1, 2))
UP = br.BenchmarkRegime(mr.TREND_UP, mr.CALM, "", "", date(2024, 1, 2))


class _Expert:
    """Overlay + down-trend trigger on, risk scale 1.5 / down-trend risk scale 0.5."""

    _S = {"regime_overlay_enabled": True, "regime_risk_scale": 1.5,
          "downtrend_overlay_enabled": True, "downtrend_risk_scale": 0.5}

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._S.get(name)


def _read():
    """Everything decision code reads, through the shared read functions."""
    return (ro.get_stressed(), br.get_benchmark_regime(), br.get_benchmark_regime_failure(),
            ro.is_downtrend_now(), ro.regime_scale(_Expert(), "regime_risk_scale", ro.get_stressed()))


def _in_thread(fn):
    out = {}

    def _t():
        out["v"] = fn()

    t = threading.Thread(target=_t)
    t.start()
    t.join(30)
    assert not t.is_alive()
    return out["v"]


@pytest.fixture(autouse=True)
def _clean():
    ro.reset_stressed()
    br.reset_benchmark_regime()
    yield
    ro.reset_stressed()
    br.reset_benchmark_regime()


def test_live_publisher_thread_and_reader_thread_share_the_global():
    def publish():
        ro.set_stressed(True)
        br.set_benchmark_regime(DOWN)
        br.set_benchmark_regime_failure(None)

    _in_thread(publish)                                   # the refresh / entry-pass thread
    assert _in_thread(_read) == (True, DOWN, None, True, 1.5 * 0.5)   # a worker thread
    assert _read() == (True, DOWN, None, True, 0.75)                  # and any other thread


def test_live_failure_reason_crosses_threads_too():
    _in_thread(lambda: (br.set_benchmark_regime(None), br.set_benchmark_regime_failure("FMP 503")))
    assert _in_thread(br.get_benchmark_regime_failure) == "FMP 503"


def test_a_fresh_run_scope_holds_nothing_published_and_never_falls_through_to_the_global():
    ro.set_stressed(True)                                 # live (or a leftover) on the global
    br.set_benchmark_regime(DOWN)
    br.set_benchmark_regime_failure("live failure")
    with run_market_state_scope():
        assert _read() == (None, None, None, False, 1.0)  # today's "nothing published" semantics


def test_a_run_scope_neither_writes_nor_resets_the_global():
    ro.set_stressed(True)
    br.set_benchmark_regime(DOWN)
    with run_market_state_scope() as state:
        ro.set_stressed(False)
        br.set_benchmark_regime(UP)
        br.set_benchmark_regime_failure("run failure")
        assert (state.stressed, state.benchmark_regime, state.benchmark_failure) == (False, UP, "run failure")
        assert _read() == (False, UP, "run failure", False, 1.0)
        ro.reset_stressed()
        br.reset_benchmark_regime()
        assert _read() == (None, None, None, False, 1.0)
    assert _read() == (True, DOWN, None, True, 0.75)
    assert current_run_state() is None


def test_two_runs_on_two_threads_never_see_each_other():
    """Lockstep: both publish, then both read -- with a shared seam one of them would read the other's."""
    barrier = threading.Barrier(2, timeout=30)
    out, errors = {}, []

    def run(name, stressed, regime):
        try:
            with run_market_state_scope():
                for _ in range(3):
                    ro.set_stressed(stressed)
                    br.set_benchmark_regime(regime)
                    barrier.wait()
                    out.setdefault(name, []).append(_read())
                    barrier.wait()
        except BaseException as e:  # noqa: BLE001 -- reported below
            errors.append(e)
            barrier.abort()

    threads = [threading.Thread(target=run, args=("A", True, DOWN)),
               threading.Thread(target=run, args=("B", False, UP))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert out["A"] == [(True, DOWN, None, True, 0.75)] * 3
    assert out["B"] == [(False, UP, None, False, 1.0)] * 3
    assert _read() == (None, None, None, False, 1.0)      # the global was never touched


def test_helper_work_submitted_with_the_run_context_reads_and_writes_the_run_state():
    with run_market_state_scope() as state:
        ro.set_stressed(True)
        br.set_benchmark_regime(DOWN)
        with ThreadPoolExecutor(max_workers=2) as ex:
            assert capture_aware_submit(ex, _read).result(30) == (True, DOWN, None, True, 0.75)
            capture_aware_submit(ex, br.set_benchmark_regime_failure, "from a helper").result(30)
        assert state.benchmark_failure == "from a helper"


def test_helper_work_without_the_run_context_reads_the_untouched_global_never_the_run():
    """A plain executor task starts with an empty context: it reads the process-global, which a
    backtest never writes -- "nothing published" here, never this (or another) run's value."""
    with run_market_state_scope():
        ro.set_stressed(True)
        br.set_benchmark_regime(DOWN)
        with ThreadPoolExecutor(max_workers=1) as ex:
            assert ex.submit(_read).result(30) == (None, None, None, False, 1.0)


def test_a_nested_scope_is_refused_and_the_outer_scope_survives():
    with run_market_state_scope() as outer:
        ro.set_stressed(True)
        with pytest.raises(RuntimeError, match="already open"):
            with run_market_state_scope():
                pass
        assert current_run_state() is outer and ro.get_stressed() is True


def test_the_scope_closes_on_an_exception():
    with pytest.raises(ValueError):
        with run_market_state_scope():
            ro.set_stressed(True)
            raise ValueError("run died")
    assert current_run_state() is None and ro.get_stressed() is None
