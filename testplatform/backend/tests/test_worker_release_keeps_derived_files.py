"""The governor's worker-side release hook vs. the host-shared derived array cache (Task 6 of
docs/plans/2026-09-14-shared-arrays-across-workers.md).

TWO CLAIMS THAT PULL IN OPPOSITE DIRECTIONS.

  * Release must still FREE THIS PROCESS. That is the whole reason the hook exists: idling a
    worker frees nothing, so the governor calls in and drops every process-local cache.
  * Release must NOT free THE HOST. With ``BA2_SHARED_ARRAYS`` on (the default) the cached
    columns are VIEWS over memory-mapped ``.npy`` files that belong to the machine, not to this
    process, and other workers are mapping the same bytes. Dropping the views closes this
    process's mapping and returns its private memory; the files stay, so the next miss re-OPENS
    them in milliseconds instead of re-parsing megabytes of parquet. A release that deleted or
    rebuilt them would turn a throttle into a stampede on every worker on the box.

Plus the flags themselves: ``BA2_SHARED_ARRAYS`` and ``BA2_SHARED_ARRAYS_LOCK_STALE_S`` have to
reach the spawned pool workers, or the master runs shared while its children each build their
own private copies -- the exact memory shape this plan exists to remove, with none of the
symptoms visible in the master.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import strategy_optimization_handler as H
from app.services.backtest import parquet_options_provider as pq
from app.services.backtest import price_source as ps


# --------------------------------------------------------------------------------------------
# Fixtures. COPIED, deliberately, from tests/backtest/test_bar_cache_shared.py and
# tests/backtest/test_parquet_options_provider.py rather than imported: a test module is not an
# importable fixture library, and this file must keep working when either of those is rewritten.
# --------------------------------------------------------------------------------------------
class FMPOHLCVProvider:  # noqa: D401 - the NAME is load-bearing (native cache dir == class name)
    """A network-backed provider stand-in; ``cached_only=True`` must never call through."""

    def get_provider_name(self) -> str:
        return "fmp"

    def get_ohlcv_data(self, *a, **k):  # pragma: no cover - hermetic mode must never call this
        raise AssertionError("hermetic backtest must not call the live provider")


class _CountingMemo(ps.MemoizedOHLCVProvider):
    """Counts ``read_window`` calls -- the parquet PARSE a warm derived set is supposed to skip."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads: list = []

    def read_window(self, symbol, start, end, interval):
        self.reads.append(symbol)
        return super().read_window(symbol, start, end, interval)


@pytest.fixture(autouse=True)
def _clean_worker_caches():
    """Every cache touched here is a PROCESS global -- the thing the release hook drops."""
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    pq.clear_worker_parquet_options_cache()
    ps._TRIAL_SEQ = 0
    yield
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    pq.clear_worker_parquet_options_cache()
    ps._TRIAL_SEQ = 0


def _native_tree(tmp_path, monkeypatch, syms=("AAA", "BBB"), n=300, interval="1d"):
    """A ``CACHE_FOLDER/FMPOHLCVProvider/<SYM>_<interval>.parquet`` tree, like
    ``ba2-test fetch-cache`` writes."""
    import ba2_common.config as cfg
    from ba2_common.core import native_cache

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    dates = (pd.bdate_range("2024-01-01", periods=n) if interval == "1d"
             else pd.date_range("2024-01-02 14:30", periods=n, freq="5min"))
    n = len(dates)
    d = tmp_path / "FMPOHLCVProvider"
    d.mkdir(exist_ok=True)
    for i, s in enumerate(syms):
        pd.DataFrame({
            "Date": dates,
            "Open": np.arange(n, dtype=float) + i,
            "High": np.arange(n, dtype=float) + i + 1.0,
            "Low": np.arange(n, dtype=float) + i - 1.0,
            "Close": np.arange(n, dtype=float) + i + 0.5,
            "Volume": np.arange(n, dtype=float) * 10.0,
        }).to_parquet(d / f"{s}_{interval}.parquet", index=False)
    return tmp_path


def _source(_tree, interval="1d", provider=None):
    """``_tree`` is the ``_native_tree`` return value. Taken and ignored ON PURPOSE: it makes the
    parquet tree an ARGUMENT, so a caller cannot build a price source before the tree (and the
    CACHE_FOLDER monkeypatch that comes with it) exists."""
    prov = provider or _CountingMemo(FMPOHLCVProvider(), datetime(2023, 1, 1),
                                     datetime(2026, 12, 31), interval=interval, cached_only=True)
    return ps.AsOfPriceSource(ohlcv_provider=prov, interval=interval), prov


def _preload(src, syms, interval="1d"):
    if interval == "1d":
        src.preload(list(syms), datetime(2024, 3, 1), datetime(2024, 12, 31), warmup_days=30)
    else:
        src.preload(list(syms), datetime(2024, 1, 2), datetime(2024, 4, 1), warmup_days=1)


def _derived_files(root: str) -> set:
    """Every published ``.npy`` under the derived root for a source tree, by absolute path."""
    from ba2_common.core import shared_arrays as SA

    return {p.resolve() for p in Path(SA.derived_root_for(str(root))).rglob("*.npy")}


def _bars_dump(src, syms):
    """Everything the bar store holds for ``syms``, NaN-safely comparable (repr, not ==)."""
    return {s: repr((list(src._keys[s]), src._o[s].tolist(), src._h[s].tolist(),
                     src._l[s].tolist(), src._c[s].tolist(), src._v[s].tolist()))
            for s in syms}


_UNDER = "ZZ"
_OPT_BARS = {
    date(2023, 1, 20): [
        ("ZZ230120C00100000", date(2023, 1, 3), 5.0, 5.4, 4.9, 5.2, 110, 900, 0.31),
        ("ZZ230120C00100000", date(2023, 1, 5), 6.0, 6.4, 5.9, 6.2, 120, 950, 0.33),
        ("ZZ230120P00100000", date(2023, 1, 3), 4.0, 4.4, 3.9, 4.2, 40, 500, 0.29),
    ],
    date(2023, 2, 17): [
        ("ZZ230217C00110000", date(2023, 1, 10), 3.0, 3.4, 2.9, 3.2, 55, 700, 0.28),
    ],
}


@pytest.fixture
def store_root(tmp_path):
    """A parquet option tree written through the REAL store, so the layout under test is the
    one ``tools/warm_options_history.py`` produces."""
    from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    root = str(tmp_path / "TastyTradeOptionsProvider")
    store = OptionHistoryParquetStore(root=root)
    for expiry, rows in _OPT_BARS.items():
        store.write_partition(
            _UNDER, expiry,
            [OptionEodBar(occ_symbol=occ, bar_date=d, open=o, high=h, low=lo, close=c,
                          volume=v, open_interest=oi, iv=iv)
             for (occ, d, o, h, lo, c, v, oi, iv) in rows],
            start=date(2023, 1, 1), end=date(2023, 3, 31))
    pq.clear_worker_parquet_options_cache()
    yield root
    pq.clear_worker_parquet_options_cache()


def _count_option_reads(monkeypatch):
    """Count ``OptionHistoryParquetStore.read_underlying`` -- the parquet read a warm derived
    set skips. NOT ``_load_raw_underlying``, which runs on every cold OPEN by design."""
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    real = OptionHistoryParquetStore.read_underlying
    calls = {"n": 0}

    def counting(self, underlying, parts=None):
        calls["n"] += 1
        return real(self, underlying, parts)

    monkeypatch.setattr(OptionHistoryParquetStore, "read_underlying", counting)
    return calls


# --------------------------------------------------------------------------------------------
# 1. The flags must reach the children
# --------------------------------------------------------------------------------------------
def test_shared_arrays_env_flags_reach_spawned_workers():
    """A master that maps and children that each parse privately is the worst of both worlds:
    the memory shape this plan removes, with the master's telemetry saying it is fixed."""
    assert "BA2_SHARED_ARRAYS" in H._WORKER_ENV_KEYS
    assert "BA2_SHARED_ARRAYS_LOCK_STALE_S" in H._WORKER_ENV_KEYS


def test_the_release_hook_names_functions_that_exist():
    """A rename on either side is caught HERE, at import time, not by a grid that quietly stops
    reclaiming: the hook reaches these two by string, and for months it named two functions that
    had never existed (`clear_worker_option_caches`, `clear_worker_5m_cache`)."""
    import importlib

    for mod, fn in (("app.services.backtest.options_provider", "clear_worker_options_cache"),
                    ("app.services.backtest.results", "clear_worker_5m_bars_cache"),
                    ("app.services.backtest.parquet_options_provider", "memory_stats")):
        assert hasattr(importlib.import_module(mod), fn), f"{mod}.{fn} is gone"


# --------------------------------------------------------------------------------------------
# 2. Release frees the process, not the host -- OHLCV bars
# --------------------------------------------------------------------------------------------
def test_release_drops_views_but_keeps_the_derived_files(tmp_path, monkeypatch):
    """The governor's release hook must free the PROCESS's memory, not the HOST's cache."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    root = _native_tree(tmp_path, monkeypatch)
    src, prov = _source(root)
    _preload(src, ["AAA", "BBB"])
    before = _bars_dump(src, ("AAA", "BBB"))
    files = _derived_files(tmp_path / "FMPOHLCVProvider")
    assert files, "nothing was published to the derived cache"
    assert sorted(prov.reads) == ["AAA", "BBB"]

    out = H._worker_release_memory()

    assert isinstance(out, dict)
    assert not ps._WORKER_BAR_CACHE, "the release must drop this process's views"
    assert not ps._BAR_CACHE_LAST_USED
    assert _derived_files(tmp_path / "FMPOHLCVProvider") == files, \
        "the derived .npy set belongs to the HOST -- a release must never touch it"

    # A later miss RE-OPENS the mapped set (ms) instead of re-parsing the parquet (tens of ms,
    # and the whole frame's transient) -- and serves byte-identical arrays.
    prov.reads.clear()
    src2, _ = _source(root, provider=prov)
    _preload(src2, ["AAA", "BBB"])
    assert prov.reads == [], "a released worker re-parsed parquet it could have mapped"
    assert _bars_dump(src2, ("AAA", "BBB")) == before


# --------------------------------------------------------------------------------------------
# 3. Release frees the process, not the host -- the option reader
# --------------------------------------------------------------------------------------------
def test_release_drops_option_views_but_keeps_the_derived_files(store_root, monkeypatch):
    """Same contract on the reader that motivated the plan (15.6 GB of private arrays PER
    WORKER at the 2020 ThetaData window)."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    raw = pq._raw_underlying(store_root, _UNDER)
    assert raw.n_rows and pq._WORKER_RAW_CACHE, "nothing was cached to release"
    files = _derived_files(store_root)
    assert files, "nothing was published to the derived cache"

    H._worker_release_memory()

    assert not pq._WORKER_RAW_CACHE, "the release must drop this process's option views"
    assert not pq._WORKER_UNDERLYING_CACHE
    assert _derived_files(store_root) == files, \
        "the derived .npy set belongs to the HOST -- a release must never touch it"

    calls = _count_option_reads(monkeypatch)
    raw2 = pq._raw_underlying(store_root, _UNDER)
    assert calls["n"] == 0, "a released worker re-parsed option parquet it could have mapped"
    for name in pq._RawUnderlying._DIRECT_ARRAYS:   # the encoded three are decoded, not bound
        np.testing.assert_array_equal(getattr(raw2, name), getattr(raw, name))
    assert raw2.c_occ == raw.c_occ and raw2.n_rows == raw.n_rows


# --------------------------------------------------------------------------------------------
# 4. Telemetry: what was mapped vs. what was private
# --------------------------------------------------------------------------------------------
def test_release_reports_shared_mb(tmp_path, monkeypatch):
    """``freed_cache_mb`` alone cannot distinguish a worker holding 5 GB of its own from one
    holding views over 5 GB the host holds once -- and the governor's actuator (throttle,
    then break the pool) is chosen on exactly that difference."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    # 20k intraday bars, so the MB figures clear the telemetry's 0.1 MB rounding.
    src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",), n=20_000,
                                  interval="5min"), interval="5min")
    _preload(src, ["AAA"], interval="5min")

    out = H._worker_release_memory()

    assert out["shared_mb"] > 0, "the mapped MB must be reported, not folded into the private MB"
    assert out["freed_cache_mb"] >= 0
    assert out["shared_mb"] > out["freed_cache_mb"], \
        "with the columns mapped, the private part is the int64 keys (8 of 48 B/bar)"


def test_release_reports_zero_shared_mb_on_the_private_path(tmp_path, monkeypatch):
    """``BA2_SHARED_ARRAYS=0``: every column is a private allocation, so nothing is shared and
    the whole cache is this process's own -- the reading the governor used to get."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",), n=20_000,
                                  interval="5min"), interval="5min")
    _preload(src, ["AAA"], interval="5min")

    out = H._worker_release_memory()

    assert out["shared_mb"] == 0.0
    assert out["freed_cache_mb"] > 0
    assert not (tmp_path / "_derived").exists(), "the escape hatch must write nothing"


# --------------------------------------------------------------------------------------------
# 5. A release that cannot clear something SAYS SO
# --------------------------------------------------------------------------------------------
@pytest.fixture
def worker_log(monkeypatch):
    """Capture ``price_source._worker_log`` -- the only channel that survives inside a pool
    child, whose stdlib logging ``_worker_init`` disables globally."""
    lines: list = []
    monkeypatch.setattr(ps, "_worker_log", lines.append)
    return lines


def test_a_missing_clear_function_is_announced_not_swallowed(worker_log, monkeypatch):
    """The whole bug this file was written after: a name that no longer resolves used to leave
    the cache resident with the release reporting success."""
    import importlib

    results = importlib.import_module("app.services.backtest.results")
    monkeypatch.delattr(results, "clear_worker_5m_bars_cache")

    H._worker_release_memory()

    assert any("clear_worker_5m_bars_cache" in m and "GONE" in m for m in worker_log), worker_log


def test_a_failing_clear_is_announced_and_does_not_stop_the_others(worker_log, monkeypatch):
    import importlib

    options = importlib.import_module("app.services.backtest.options_provider")
    results = importlib.import_module("app.services.backtest.results")
    ran: list = []
    monkeypatch.setattr(options, "clear_worker_options_cache",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(results, "clear_worker_5m_bars_cache", lambda: ran.append("5m"))

    H._worker_release_memory()

    assert any("clear_worker_options_cache" in m and "boom" in m for m in worker_log), worker_log
    assert ran == ["5m"], "one failing clear must not skip the rest of the release"


def test_failed_stats_are_announced_so_zero_is_not_read_as_empty(worker_log, monkeypatch):
    monkeypatch.setattr(ps, "memory_stats",
                        lambda: (_ for _ in ()).throw(RuntimeError("psutil gone")))

    out = H._worker_release_memory()

    assert out["freed_cache_mb"] == 0.0
    assert any("memory stats failed" in m and "psutil gone" in m for m in worker_log), worker_log


# --------------------------------------------------------------------------------------------
# 6. The governor's log shows private and mapped apart
# --------------------------------------------------------------------------------------------
class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return dict(self._value)


class _FakePool:
    """Stands in for a ProcessPoolExecutor: every submit resolves to one worker's release dict."""

    def __init__(self, value):
        self.value = value
        self.submits = 0

    def submit(self, fn, *a):
        self.submits += 1
        return _FakeFuture(self.value)


def test_the_pool_release_log_separates_private_from_mapped():
    """Summed into one number, a release that freed almost nothing PRIVATE reads as a big win --
    and the governor's next escalation then looks unnecessary to whoever reads the log."""
    msgs: list = []
    pool = _FakePool({"freed_cache_mb": 10.0, "shared_mb": 100.0})

    H._release_pool_memory(pool, 2, log=msgs.append)

    assert pool.submits == 6                      # oversubscribed 3x, see the docstring
    assert "~60 MB private" in msgs[0] and "+600 MB mapped views" in msgs[0], msgs


def test_slot_pools_release_all_returns_the_totals_and_they_reach_the_log():
    """``release_all`` used to discard every result dict, so the per-slot path -- the LOCAL
    path -- logged no numbers at all."""
    pools = H._SlotPools.__new__(H._SlotPools)     # no real subprocesses in a unit test
    pools.pools = [_FakePool({"freed_cache_mb": 7.0, "shared_mb": 70.0}) for _ in range(3)]

    got = pools.release_all()
    assert got == {"freed_cache_mb": 21.0, "shared_mb": 210.0}

    msgs: list = []
    H._release_pool_memory(pools, 3, log=msgs.append)
    assert "~21 MB private" in msgs[0] and "+210 MB mapped views" in msgs[0], msgs
    assert "per-slot" in msgs[0]


# --------------------------------------------------------------------------------------------
# 7. The option reader can say what it holds, and which half is the host's
# --------------------------------------------------------------------------------------------
_BIG_UNDER = "ZY"
_BIG_EXPIRY = date(2024, 3, 15)


@pytest.fixture
def big_store_root(tmp_path):
    """A store big enough that the MB figures clear the telemetry's 0.1 MB rounding:
    200 contracts x 300 bar dates = 60,000 rows (a rounding-sized fixture would make the
    private/shared split unreadable rather than wrong)."""
    from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    root = str(tmp_path / "ThetaDataOptionsProvider")
    dates = [d.date() for d in pd.bdate_range("2023-01-03", periods=300)]
    bars = []
    for i in range(200):
        strike = 50.0 + i
        occ = f"{_BIG_UNDER}{_BIG_EXPIRY:%y%m%d}C{int(round(strike * 1000)):08d}"
        for j, d in enumerate(dates):
            bars.append(OptionEodBar(occ_symbol=occ, bar_date=d, open=1.0 + j * 0.01,
                                     high=1.2 + j * 0.01, low=0.9 + j * 0.01,
                                     close=1.1 + j * 0.01, volume=10 + j, open_interest=100 + j,
                                     iv=0.3))
    OptionHistoryParquetStore(root=root).write_partition(
        _BIG_UNDER, _BIG_EXPIRY, bars, start=dates[0], end=_BIG_EXPIRY)
    pq.clear_worker_parquet_options_cache()
    yield root
    pq.clear_worker_parquet_options_cache()


def test_option_memory_stats_splits_mapped_columns_from_private_projections(big_store_root,
                                                                           monkeypatch):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    pq._raw_underlying(big_store_root, _BIG_UNDER)

    st = pq.memory_stats()

    assert st["entries"] == 1
    assert st["shared_mb"] > 0, "the mapped columns must be visible as the HOST's, not this one's"
    assert st["private_mb"] > 0, "bar_ord_l + the contract lists are this process's own"
    assert st["private_mb"] < st["shared_mb"], "4 B/row private against ~64 B/row mapped"


def test_option_memory_stats_reports_nothing_shared_on_the_private_path(big_store_root,
                                                                       monkeypatch):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    pq._raw_underlying(big_store_root, _BIG_UNDER)

    st = pq.memory_stats()

    assert st["shared_mb"] == 0.0, "the escape hatch maps nothing"
    assert st["private_mb"] > 0


def test_release_counts_the_option_caches_it_drops(big_store_root, monkeypatch):
    """``freed_cache_mb``/``shared_mb`` used to cover the BARS only, so a release of gigabytes
    of option arrays reported whatever the (possibly empty) bar cache happened to hold."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    pq._raw_underlying(big_store_root, _BIG_UNDER)
    st = pq.memory_stats()

    out = H._worker_release_memory()

    assert out["shared_mb"] >= st["shared_mb"]
    assert out["freed_cache_mb"] >= st["private_mb"]
    assert not pq._WORKER_RAW_CACHE
