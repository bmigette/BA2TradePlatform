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


# ----------------------------------------------------------------------------------------------
# History fetch: NO process-wide lock (prod screens use disjoint bands; they must overlap), and a
# real chunk failure through ``_fetch_history_bulk`` (only ``fmp_http_get`` is faked).
# ----------------------------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone


def _recent_bars(volume=1_000_000, n=25):
    today = datetime.now(timezone.utc)
    return [{"date": (today - timedelta(days=k)).strftime("%Y-%m-%d"), "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 10.0, "volume": volume} for k in range(1, n + 1)]      # newest first


def _history_http(monkeypatch, fail=None, delay=0.0, log=None):
    """Fake the history endpoint. ``fail(chunk_symbols, attempt)`` True => raise FMPError."""
    attempts = {}
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def fake(url, params=None, **kw):
        chunk = tuple(url.rsplit("/", 1)[1].split(","))
        with lock:
            attempts[chunk] = attempts.get(chunk, 0) + 1
            n = attempts[chunk]
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        try:
            if delay:
                time.sleep(delay)
            if fail and fail(chunk, n):
                raise fmp_common.FMPError("HTTP 429")
            return _Resp({"historicalStockList": [{"symbol": s, "historical": _recent_bars()} for s in chunk]})
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(fmp_common, "fmp_http_get", fake)
    monkeypatch.setattr(S, "get_app_setting", lambda k: "k")
    fmp_common._LIVE_BULK_CACHES.clear()
    return attempts, state


def test_two_screens_on_disjoint_symbols_overlap_in_time(monkeypatch):
    attempts, state = _history_http(monkeypatch, delay=0.3)
    sc1, sc2 = S.StockScreener({}), S.StockScreener({})
    t0 = time.monotonic()
    threads = [threading.Thread(target=sc1._fetch_history_bulk, args=([f"AA{i}" for i in range(5)], 30)),
               threading.Thread(target=sc2._fetch_history_bulk, args=([f"BB{i}" for i in range(5)], 30))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] == 2                         # no serialisation between screens
    assert time.monotonic() - t0 < 0.55              # ~0.3 s, not ~0.6 s


def _enrich(sc, symbols, min_rvol=0.0):
    sc._fetch_quotes_chunked = lambda syms, *a, **k: {}
    return sc._enrich_with_rvol([{"symbol": s, "price": 10.0, "volume": 1} for s in symbols], min_rvol)


def test_failed_chunk_is_refetched_once_and_recovers(monkeypatch):
    syms = [f"RC{i:02d}" for i in range(20)]
    bad = tuple(syms[5:10])
    attempts, _ = _history_http(monkeypatch, fail=lambda chunk, n: chunk == bad and n == 1)
    sc = S.StockScreener({"screener_volume_min": 500_000})
    kept, stats = _enrich(sc, syms)
    assert len(kept) == 20 and stats["dropped_no_history"] == 0
    assert attempts[bad] == 2                        # a real second attempt of the failed chunk only
    assert all(n == 1 for c, n in attempts.items() if c != bad)


def test_persistent_chunk_failure_raises_after_one_refetch(monkeypatch):
    syms = [f"PC{i:02d}" for i in range(20)]
    bad = tuple(syms[0:5])                           # 25% without history
    attempts, _ = _history_http(monkeypatch, fail=lambda chunk, n: chunk == bad)
    sc = S.StockScreener({"screener_volume_min": 500_000})
    with pytest.raises(S.ScreenerDataError, match=r"5/20"):
        _enrich(sc, syms)
    assert attempts[bad] == 2


def test_one_failed_chunk_of_many_stays_under_the_limit_and_is_labelled(monkeypatch):
    syms = [f"UL{i:02d}" for i in range(60)]         # 5/60 = 8.3% < 10%: no refetch, no raise
    bad = tuple(syms[0:5])
    attempts, _ = _history_http(monkeypatch, fail=lambda chunk, n: chunk == bad)
    sc = S.StockScreener({"screener_volume_min": 500_000})
    kept, stats = _enrich(sc, syms)
    assert len(kept) == 55 and stats["dropped_no_history"] == 5 and stats["dropped_volume_min"] == 0
    assert attempts[bad] == 1


def test_all_chunks_failing_raises_even_with_no_volume_bound(monkeypatch):
    syms = [f"AF{i:02d}" for i in range(10)]
    _history_http(monkeypatch, fail=lambda chunk, n: True)
    sc = S.StockScreener({"screener_relative_volume_min": 0})          # nothing "needs" bars
    with pytest.raises(S.ScreenerDataError, match="no price history for any"):
        _enrich(sc, syms)


def test_price_drop_and_weinstein_stages_refuse_a_total_history_failure(monkeypatch):
    syms = [f"PD{i:02d}" for i in range(10)]
    _history_http(monkeypatch, fail=lambda chunk, n: True)
    sc = S.StockScreener({"screener_price_drop_pct": 10, "screener_price_drop_days": 5})
    cands = [{"symbol": s, "price": 10.0} for s in syms]
    with pytest.raises(S.ScreenerDataError, match="price-drop"):
        sc._filter_by_price_drop(cands, 10, 5)
    with pytest.raises(S.ScreenerDataError, match="Weinstein"):
        sc._filter_by_weinstein_stage2(cands)
