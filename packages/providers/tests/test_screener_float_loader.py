"""The bulk float table loader: single-flight, bounded, failure-remembering, typed errors."""
import threading
import time

import pytest

import ba2_providers.fmp_common as fmp_common
import ba2_providers.StockScreener as S
from ba2_providers.screener import float_filter as ff

ROWS = [{"symbol": "AAA", "floatShares": 5e7}, {"symbol": "BBB", "floatShares": 0}]


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ff.reset_float_table_cache()
    monkeypatch.setattr(ff, "get_app_setting", lambda k: "k")
    yield
    ff.reset_float_table_cache()


def _patch_http(monkeypatch, fn):
    calls = {"n": 0, "kwargs": None}

    def fake(url, params=None, **kw):
        calls["n"] += 1
        calls["kwargs"] = kw
        return fn(calls["n"])

    monkeypatch.setattr(fmp_common, "fmp_http_get", fake)
    return calls


def test_cold_load_is_single_flight_across_threads(monkeypatch):
    def slow(n):
        time.sleep(0.3)
        return _Resp(ROWS)

    calls = _patch_http(monkeypatch, slow)
    results, errors = [], []

    def worker():
        try:
            results.append(ff.load_float_table())
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert calls["n"] == 1                           # ONE download for ten concurrent screens
    assert len(results) == 10 and all(r == {"AAA": 5e7} for r in results)


def test_fetch_is_bounded(monkeypatch):
    calls = _patch_http(monkeypatch, lambda n: _Resp(ROWS))
    ff.load_float_table()
    assert calls["kwargs"]["timeout"] == ff.FLOAT_FETCH_TIMEOUT_S == 45
    assert calls["kwargs"]["delays"] == ff.FLOAT_FETCH_DELAYS == (5,)        # 2 attempts max


def test_failure_is_typed_remembered_then_retried(monkeypatch):
    def fail(n):
        raise S.FMPError("HTTP 429")

    calls = _patch_http(monkeypatch, fail)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ff, "_now", lambda: clock["t"])
    with pytest.raises(S.ScreenerDataError, match="429"):
        ff.load_float_table()
    assert calls["n"] == 1
    # later screens inside the memo window re-raise at once, with no new download
    clock["t"] += ff.FLOAT_FAILURE_MEMO_S - 1
    for _ in range(5):
        with pytest.raises(S.ScreenerDataError, match="remembered"):
            ff.load_float_table()
    assert calls["n"] == 1
    # after the window the vendor is asked again (and a success clears the memory)
    clock["t"] += 2
    monkeypatch.setattr(fmp_common, "fmp_http_get", lambda *a, **k: _Resp(ROWS))
    assert ff.load_float_table() == {"AAA": 5e7}


def test_concurrent_failures_download_once(monkeypatch):
    def fail(n):
        time.sleep(0.2)
        raise S.FMPError("boom")

    calls = _patch_http(monkeypatch, fail)
    errs = []

    def worker():
        try:
            ff.load_float_table()
        except S.ScreenerDataError as e:
            errs.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(errs) == 6 and calls["n"] == 1


def test_error_is_an_fmp_error_and_empty_payload_is_a_failure(monkeypatch):
    _patch_http(monkeypatch, lambda n: _Resp([]))
    with pytest.raises(S.ScreenerDataError):
        ff.load_float_table()
    assert issubclass(S.ScreenerDataError, fmp_common.FMPError)


def test_live_history_fetch_is_single_flight(monkeypatch):
    sc = S.StockScreener({})
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def impl(symbols, lookback_days, chunk_size=5, max_workers=8):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
        return {}

    monkeypatch.setattr(sc, "_fetch_history_bulk_impl", impl)
    threads = [threading.Thread(target=sc._fetch_history_bulk, args=(["A"], 30)) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] == 1


def test_as_of_history_fetch_is_not_locked(monkeypatch):
    from datetime import datetime, timezone
    sc = S.StockScreener({}, as_of=datetime(2020, 6, 30, tzinfo=timezone.utc))
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def impl(symbols, lookback_days, chunk_size=5, max_workers=8):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.15)
        with lock:
            state["active"] -= 1
        return {}

    monkeypatch.setattr(sc, "_fetch_history_bulk_impl", impl)
    threads = [threading.Thread(target=sc._fetch_history_bulk, args=(["A"], 30)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] > 1
