"""Review fixes for the ATM-IV FMP-429 work: the REAL spot loader tops up, breaker, bounded fetch, warm
guards, gate summary outside the lock and for overlapping batches."""
import threading
import time
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from ba2_trade_platform.modules.dataproviders.options import atm_iv_task_hook as K
from tests.atm_iv_fakes import FakeSpots, FakeWorld, make_provider, reset_module_state

END = date(2026, 3, 6)
LOOK = 60
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    with K._PASS_LOCK:
        K._PASS.clear()
    yield
    reset_module_state()


class FlakySpots:
    def __init__(self, world, always=True):
        self.inner, self.always, self.calls = FakeSpots(world), always, 0

    def spots(self, symbol, start, end):
        self.calls += 1
        if self.always:
            raise H.SpotSourceTransient("TEST: spot fetch failed: FMPError: HTTP 429") from RuntimeError("429")
        return self.inner.spots(symbol, start, end)


class _Acct:
    def __init__(self, prov):
        self.p = prov

    def _atm_iv_history(self):
        return self.p


def _series(status, reason=None, values=(), end=END):
    return H.AtmIvSeries(symbol="X", end_session=end, status=status, values={d: 0.3 for d in values}, reason=reason)


# ---- the REAL loader ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p, self.headers = status, payload, {}
        self.content = b"x" * 10

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def fmp_world(tmp_path, monkeypatch):
    """Scratch CACHE_FOLDER with TEST_1d.parquet + a fake FMP HTTP layer (requests.get)."""
    import ba2_common.core.native_cache as nc
    import ba2_providers.fmp_common as fc
    import importlib
    fp = importlib.import_module('ba2_providers.ohlcv.FMPOHLCVProvider')
    fp = __import__('sys').modules['ba2_providers.ohlcv.FMPOHLCVProvider']
    from ba2_trade_platform import config
    monkeypatch.setattr(nc, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(fp, "get_app_setting", lambda k: "key")
    monkeypatch.setattr(fc, "_gate_wait", lambda sleep: None)
    monkeypatch.setattr(fc, "_gate_arm", lambda d: None)
    state = types.SimpleNamespace(urls=[], mode="ok", bars={})
    for d in pd.bdate_range("2026-01-05", "2026-02-12"):
        state.bars[d.date()] = 100.0 + d.toordinal() % 7

    def fake_get(url, params=None, timeout=None):
        state.urls.append(url)
        if state.mode == "429":
            return _Resp(429)
        if "stock_split" in url:
            return _Resp(200, {"symbol": "TEST", "historical": []})
        lo, hi = pd.Timestamp(params["from"]).date(), pd.Timestamp(params["to"]).date()
        rows = [{"date": d.isoformat(), "open": px, "high": px, "low": px, "close": px, "volume": 1}
                for d, px in sorted(state.bars.items(), reverse=True) if lo <= d <= hi]
        return _Resp(200, {"symbol": "TEST", "historical": rows})
    monkeypatch.setattr(requests, "get", fake_get)

    def seed(last):
        df = pd.DataFrame([{"Date": pd.Timestamp(d, tz="UTC"), "Open": px, "High": px, "Low": px, "Close": px,
                            "Volume": 1} for d, px in sorted(state.bars.items()) if d <= last])
        df["effective_date"] = df["Date"]
        assert nc.write_timeseries("FMPOHLCVProvider", "TEST", "1d", df)
    state.seed = seed
    return state


def test_real_default_loader_tops_up_a_cache_four_sessions_behind(fmp_world):
    """No injected loader. Cache ends 2026-02-06, wanted sessions 02-09..02-12: the OLD loader asked the
    provider for a PAST end date, which never tops up, so those sessions came back missing."""
    fmp_world.seed(date(2026, 2, 6))
    spots = H.FmpSpotSource().spots("TEST", date(2026, 2, 9), date(2026, 2, 12))
    assert sorted(spots) == [date(2026, 2, 9), date(2026, 2, 10), date(2026, 2, 11), date(2026, 2, 12)]
    assert any("historical-price-full/TEST" in u for u in fmp_world.urls)


def test_real_default_loader_makes_zero_fmp_requests_when_the_cache_is_current(fmp_world):
    fmp_world.seed(date(2026, 2, 12))
    spots = H.FmpSpotSource().spots("TEST", date(2026, 2, 9), date(2026, 2, 12))
    assert len(spots) == 4
    assert fmp_world.urls == []


def test_real_default_loader_429_is_transient_and_costs_one_http_call_worth_of_requests(fmp_world):
    fmp_world.seed(date(2026, 2, 6))
    fmp_world.mode = "429"
    with pytest.raises(H.SpotSourceTransient) as ei:
        H.FmpSpotSource().spots("TEST", date(2026, 2, 9), date(2026, 2, 12))
    assert ei.value.retryable
    assert len(fmp_world.urls) == 4          # fmp_http_get's 4 attempts: no probe, no second endpoint


def test_real_loader_vendor_without_the_bar_is_a_genuine_missing_session(fmp_world):
    fmp_world.seed(date(2026, 2, 6))
    del fmp_world.bars[date(2026, 2, 11)]
    spots = H.FmpSpotSource().spots("TEST", date(2026, 2, 9), date(2026, 2, 12))
    assert date(2026, 2, 11) not in spots and date(2026, 2, 12) in spots


def test_worst_case_extra_fmp_requests_per_pass_with_real_loader(fmp_world, tmp_path):
    # one parquet (TEST) stands for every symbol: the breaker counts per ensure_filled call
    """97 symbols, FMP 429 for everything, in-task path: the breaker opens after BREAKER_SYMBOLS symbols."""
    fmp_world.seed(date(2026, 2, 6))
    fmp_world.mode = "429"
    prov, *_ = make_provider(tmp_path / "iv", spot_source=H.FmpSpotSource(), sleep=lambda s: None)
    statuses = [prov.ensure_filled("TEST", date(2026, 2, 12), LOOK, deadline_seconds=300).reason
                for _ in range(10)]
    per_symbol = 4 * (H.SPOT_RETRIES_IN_TASK + 1)
    # BREAKER_SYMBOLS symbols fail normally, every later call is short-circuited with zero FMP requests
    assert len(fmp_world.urls) == per_symbol * H.BREAKER_SYMBOLS
    assert sum("circuit open" in (r or "") for r in statuses) == 10 - H.BREAKER_SYMBOLS


# ---- breaker / bounds / guards -----------------------------------------------------------------
def test_breaker_counts_repeated_spot_failures_and_short_circuits_later_symbols(tmp_path):
    world = FakeWorld()
    spots = FlakySpots(world)
    prov, *_ = make_provider(tmp_path, world, spot_source=spots, sleep=lambda s: None)
    for sym in ("AAA", "BBB", "CCC"):
        assert prov.ensure_filled(sym, END, LOOK).status == H.STATUS_FILLING
    calls = spots.calls
    res = prov.ensure_filled("DDD", END, LOOK)
    assert res.status == H.STATUS_FILLING and "circuit open" in res.reason
    assert spots.calls == calls


def test_one_spot_fetch_is_bounded_and_worst_case_per_symbol_is_stated(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "SPOT_CALL_LIMIT_SECONDS", 0.3)
    release = threading.Event()

    class Hung:
        def spots(self, s, a, b):
            release.wait(5)
            return {}
    prov, *_ = make_provider(tmp_path, spot_source=Hung(), sleep=lambda s: None)
    t0 = time.monotonic()
    res = prov.ensure_filled("TEST", END, LOOK, deadline_seconds=60)
    took = time.monotonic() - t0
    release.set()
    assert res.status == H.STATUS_FILLING and "TimeoutError" in res.reason
    assert took < 2.0


def test_worst_case_per_symbol_constants_in_task_vs_warm():
    backoff = max(H.SPOT_BACKOFF_BASE_SECONDS * 1.5, H.SPOT_GATE_WAIT_CAP_SECONDS)
    in_task = (H.SPOT_RETRIES_IN_TASK + 1) * H.SPOT_CALL_LIMIT_SECONDS + H.SPOT_RETRIES_IN_TASK * backoff
    assert in_task <= 110.0 and H.SPOT_RETRIES_IN_TASK == 1 and H.SPOT_RETRIES_WARM > H.SPOT_RETRIES_IN_TASK


def test_guard_stops_the_fill_before_any_spot_call(tmp_path):
    spots = FlakySpots(FakeWorld(), always=False)
    prov, *_ = make_provider(tmp_path, spot_source=spots)
    res = prov.ensure_filled("TEST", END, LOOK, guard=lambda: "warm deadline 09:20 ET reached")
    assert res.status == H.STATUS_FILLING and "09:20" in res.reason and spots.calls == 0


def test_warm_catchup_window():
    mon = lambda h, m: datetime(2026, 10, 12, h, m, tzinfo=NY)
    assert K.warm_catchup_due(mon(8, 45)) and K.warm_catchup_due(mon(9, 14))
    assert not K.warm_catchup_due(mon(8, 30)) and not K.warm_catchup_due(mon(9, 15))
    assert not K.warm_catchup_due(datetime(2026, 10, 10, 8, 45, tzinfo=NY))       # Saturday


def test_warm_rechecks_the_deadline_between_symbols(tmp_path):
    prov, *_ = make_provider(tmp_path)
    t = [datetime(2026, 3, 9, 9, 19, 30, tzinfo=NY)]

    def sleep(s):
        t[0] = t[0] + timedelta(seconds=40)
    out = K.warm_iv_history(targets={1: ["A", "B", "C"]}, account_resolver=lambda i: _Acct(prov),
                            now=lambda: t[0], sleep=sleep, pace_seconds=40.0)
    assert out["skipped_deadline"] >= 1


# ---- gate summary: overlapping batches, outside the lock ---------------------------------------
def test_overlapping_batches_of_one_expert_are_all_flushed_and_nothing_leaks(monkeypatch):
    logged = []
    import ba2_common.core.db as dbm
    monkeypatch.setattr(dbm, "log_activity", lambda *a, **k: logged.append(a))
    K.record_gate_outcome(3, "old", "AAA", _series("complete"))
    K.record_gate_outcome(3, "new", "BBB", _series("complete"))
    popped = K.pop_gate_reports(3)
    assert len(popped) == 2 and K._PASS == {}
    assert len(K.emit_gate_reports(popped)) == 2 and len(logged) == 2


def test_worker_queue_emits_the_gate_summary_outside_the_risk_manager_lock(monkeypatch):
    from ba2_trade_platform.core.WorkerQueue import WorkerQueue
    from ba2_trade_platform.core.types import AnalysisUseCase
    wq = WorkerQueue.__new__(WorkerQueue)
    wq._risk_manager_lock = threading.Lock()
    wq._task_lock = threading.Lock()
    wq._tasks = {}
    wq._processing_experts = set()
    wq._expert_priority = types.SimpleNamespace(contains=lambda b: False)
    K.record_gate_outcome(9, "bb", "AAA", _series("complete"))
    seen = {}

    def emit(popped):
        seen["locked"] = wq._risk_manager_lock.locked()
        seen["n"] = len(popped)
        return []
    monkeypatch.setattr(K, "emit_gate_reports", emit)
    import ba2_trade_platform.core.utils as cu
    monkeypatch.setattr(cu, "get_expert_instance_from_id", lambda i: None)     # early return inside the lock
    wq._process_expert_recommendations(9, AnalysisUseCase.ENTER_MARKET, "bb")
    assert seen == {"locked": False, "n": 1}
    assert K._PASS == {}
