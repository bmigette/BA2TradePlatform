"""LIVE: the regime TradeManager publishes on one thread is what decision code reads on another.

Live publishes the overlay stress flag and the SPY benchmark regime from the account-refresh /
entry-pass thread and reads them from the recommendation worker threads (sizing, account
validation, rule conditions, TP/SL actions). The backtest's per-run scope
(``ba2_common.core.market_state_scope``) must not change that: live opens no scope, so the
publisher and the readers share the process-global -- including while a backtest runs in a scope on
another thread of the same process, which must neither see nor disturb the live value.
"""
from __future__ import annotations

import threading

import pandas as pd
import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core import regime_overlay as ro
from ba2_common.core.market_state_scope import run_market_state_scope

from ba2_trade_platform.core import TradeManager as tm_mod
from ba2_trade_platform.core.TradeManager import TradeManager
from tests.test_trademanager_benchmark_regime import THU, Fetcher, at_ny


class _Expert:
    _S = {"regime_overlay_enabled": True, "regime_risk_scale": 1.5}

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._S.get(name)


class _Provider:
    def get_ohlcv_data(self, *a, **k):
        # 680 quiet closes, then 25 wild ones: the newest realised vol ranks at the top -> STRESSED.
        quiet = [100.0 + (i % 2) * 0.05 for i in range(680)]
        wild = [100.0 if i % 2 else 112.0 for i in range(25)]
        return pd.DataFrame({"Close": quiet + wild})


def _on_thread(fn):
    out, errors = {}, []

    def _t():
        try:
            out["v"] = fn()
        except BaseException as e:  # noqa: BLE001 -- re-raised on the test thread
            errors.append(e)

    t = threading.Thread(target=_t)
    t.start()
    t.join(60)
    assert not t.is_alive()
    if errors:
        raise errors[0]
    return out.get("v")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import ba2_providers

    ro.reset_stressed()
    br.reset_benchmark_regime()
    tm_mod._reset_benchmark_regime_cache()
    monkeypatch.setattr(tm_mod, "_LIVE_REGIME_CACHE",
                        {"day": None, "stressed": None, "retry_after": None, "failures": 0})
    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: _Provider())
    monkeypatch.setattr(TradeManager, "_benchmark_regime_needed", lambda self: True)
    monkeypatch.setattr(TradeManager, "_warn_benchmark_source_mismatch", lambda self, today: None)
    yield
    ro.reset_stressed()
    br.reset_benchmark_regime()
    tm_mod._reset_benchmark_regime_cache()


def _publish_live(monkeypatch):
    tm = TradeManager()
    monkeypatch.setattr(tm, "_fetch_benchmark_daily", Fetcher(THU))
    tm._publish_market_regime()
    tm._publish_benchmark_regime(now=at_ny(THU, 17))


def test_a_worker_thread_reads_what_the_refresh_thread_published(monkeypatch):
    _on_thread(lambda: _publish_live(monkeypatch))          # the refresh / entry-pass thread
    published = tm_mod._BENCHMARK_REGIME_CACHE["regime"]
    assert tm_mod._LIVE_REGIME_CACHE["stressed"] is True and published is not None

    def worker_reads():                                     # a recommendation worker thread
        return (ro.get_stressed(), ro.regime_scale(_Expert(), "regime_risk_scale", ro.get_stressed()),
                br.get_benchmark_regime(), br.get_benchmark_regime_failure())

    assert _on_thread(worker_reads) == (True, 1.5, published, None)


def test_a_backtest_scope_on_another_thread_neither_sees_nor_disturbs_live(monkeypatch):
    _on_thread(lambda: _publish_live(monkeypatch))
    published = tm_mod._BENCHMARK_REGIME_CACHE["regime"]
    in_run, live_seen = threading.Event(), threading.Event()
    seen_by_run = {}

    def backtest():
        with run_market_state_scope():
            seen_by_run["before"] = (ro.get_stressed(), br.get_benchmark_regime())
            ro.set_stressed(False)                          # the run's own bar
            br.set_benchmark_regime(None)
            br.set_benchmark_regime_failure("run: no calendar")
            in_run.set()
            assert live_seen.wait(30)
            seen_by_run["after"] = (ro.get_stressed(), br.get_benchmark_regime_failure())

    t = threading.Thread(target=backtest)
    t.start()
    assert in_run.wait(30)
    live = _on_thread(lambda: (ro.get_stressed(), br.get_benchmark_regime(),
                               br.get_benchmark_regime_failure()))
    live_seen.set()
    t.join(30)
    assert live == (True, published, None)                  # live is untouched by the running backtest
    assert seen_by_run == {"before": (None, None), "after": (False, "run: no calendar")}
    assert (ro.get_stressed(), br.get_benchmark_regime()) == (True, published)
