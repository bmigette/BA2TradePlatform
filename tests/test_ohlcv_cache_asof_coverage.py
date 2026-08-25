"""An as_of slice that comes back EMPTY is a cache HIT, not a cache MISS.

Audit 2026-08-24, from FMP's monthly usage dashboard:

    /v3/historical-price-full   146.26k calls   65.25 GB   20% over-limit
    /v3/historical-chart/5min   742.19k calls   10.43 GB   11% over-limit

65.25 GB / 146.26k = ~446 KB per call, which is the size of a FULL 15-year daily
``historical-price-full`` payload -- i.e. essentially every one of those calls was
``get_ohlcv_data``'s COLD-FETCH branch (``datetime.now() - 15y .. now``), not an
incremental top-up.

Three defects in ``MarketDataProviderInterface.get_ohlcv_data`` /
``native_cache`` produced them, and they compound:

D1  ``read_timeseries(..., as_of=X)`` returns the rows with ``effective_date <= X``.
    For a symbol whose FIRST cached bar postdates X that slice is legitimately EMPTY
    -- the instrument had not traded yet. ``get_ohlcv_data`` mapped that empty frame
    to ``df = None``, which is also what an ABSENT cache file yields, so it fell into
    the cold-fetch branch and re-downloaded the whole 15-year history. The fetch does
    not (cannot) add rows below the symbol's real first bar, so the very next read
    slices to empty again: an UNBOUNDED re-download, once per read, forever.
    Measured on the real dev cache: 5 640 of 10 919 ``*_1d.parquet`` files (51.7%)
    have a first bar after 2020-01-01 -- exactly the window
    ``test_files/stress_2020_forwardtest.py`` runs.

D2  ``_write_ohlcv_parquet`` REPLACED the parquet with just the freshly fetched frame.
    The intraday cold-fetch range is clamped to the caller's window, so one narrow
    miss destroyed a wide cache -- which then makes D1 fire for every read outside
    the surviving sliver.

D3  ``native_cache.write_timeseries`` built its path from the interval string
    VERBATIM while ``find_timeseries_path`` reads the CANONICAL spelling first. A
    write with ``"5m"`` therefore created ``<SYM>_5m.parquet`` next to an existing
    complete ``<SYM>_5min.parquet`` and permanently SHADOWED it. Found on the real
    cache: e.g. ``AMC_5m.parquet`` holds 596 bars (2026-06-09..2026-06-18) and hides
    ``AMC_5min.parquet``'s 69 621 bars (2022-06-06..2025-12-31). 8 such pairs exist.

Non-negotiables these tests also pin:
  * a pinned historical ``as_of`` must NEVER re-fetch (reproducibility), and must
    return byte-identical rows;
  * the LIVE path must still top up, and the top-up must fetch only the MISSING
    TAIL -- never the whole history;
  * a failed/over-limit fetch must never be written to the cache (under the D1 fix a
    cached empty frame would become a permanent poisoned "no data" hit).

Time is FROZEN to 2026-03-16, deliberately not "today": this is cache-expiry logic,
and a test frozen to the current date passes for the wrong reason.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import sys

import ba2_common.core.interfaces.MarketDataProviderInterface  # noqa: F401
from ba2_common.core import native_cache

# ``interfaces/__init__`` re-exports the CLASS under the module's own name, so
# ``import ... as mdp_mod`` would bind the class. Reach for the module explicitly.
mdp_mod = sys.modules["ba2_common.core.interfaces.MarketDataProviderInterface"]
MarketDataProviderInterface = mdp_mod.MarketDataProviderInterface

# Must equal the stub class name: get_ohlcv_data keys the cache on type(self).__name__.
PROVIDER = "_StubProvider"

# Frozen wall clock. NOT today -- see module docstring.
FROZEN_NOW = datetime(2026, 3, 16, 14, 30, 0)
FROZEN_NOW_UTC = FROZEN_NOW.replace(tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """``datetime`` with a pinned ``now()``/``utcnow()`` (no freezegun in this venv)."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW_UTC.astimezone(tz).replace(tzinfo=tz) if tz else FROZEN_NOW

    @classmethod
    def utcnow(cls):
        return FROZEN_NOW


class _StubProvider(MarketDataProviderInterface):
    """Counts every source fetch and records the exact range each one asked for."""

    def __init__(self, rows_for=None):
        super().__init__()
        self.impl_calls = []
        # rows_for(start, end, interval) -> DataFrame; default: no data at all
        # (the realistic shape for a symbol whose history starts after ``start``).
        self._rows_for = rows_for

    def get_provider_name(self):
        return PROVIDER

    def get_supported_features(self):
        return ["ohlcv"]

    def validate_config(self):
        return True

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval):
        self.impl_calls.append(
            {"symbol": symbol, "start": start_date, "end": end_date, "interval": interval}
        )
        if self._rows_for is None:
            return _frame([])
        return self._rows_for(start_date, end_date, interval)

    def fetched_days(self):
        """Calendar-day span of each recorded fetch (how much history it asked for)."""
        return [(c["end"] - c["start"]).days for c in self.impl_calls]


def _frame(dates):
    """Minimal OHLCV frame over ``dates`` (no effective_date -- that is cache-internal)."""
    dates = list(dates)
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(pd.Series(dates, dtype="datetime64[ns]")),
            "Open": [10.0] * len(dates),
            "High": [11.0] * len(dates),
            "Low": [9.0] * len(dates),
            "Close": [10.5] * len(dates),
            "Volume": [1000] * len(dates),
        }
    )


def _fmp_like(first_bar, last_bar, freq="D"):
    """``rows_for`` that behaves like FMP: it serves the symbol's REAL history and
    nothing before its first bar, however far back the caller asks.

    This is what makes the defect unbounded -- a cold fetch triggered by an as_of
    below the first bar cannot add any row below it, so the next read misses again.
    """

    def _rows(start, end, interval):
        lo = max(pd.Timestamp(start).tz_localize(None), pd.Timestamp(first_bar))
        hi = min(pd.Timestamp(end).tz_localize(None), pd.Timestamp(last_bar))
        if hi < lo:
            return _frame([])
        return _frame(pd.date_range(lo, hi, freq=freq))

    return _rows


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect the parquet cache root AND freeze the clock.

    The real ~/Documents/.../cache holds 9.8 GB the user depends on -- nothing here
    may read-through or write to it.
    """
    d = tmp_path / "cache"
    d.mkdir()
    import ba2_common.config as cfg

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(d))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(d))
    monkeypatch.setattr(native_cache, "_CACHE_ROOT", str(d / "datasets" / "cache"))
    monkeypatch.setattr(mdp_mod, "datetime", _FrozenDatetime)
    return str(d)


def _seed(symbol, interval, first, last, freq="D", spelling=None, age_hours=0.0):
    """Write a parquet spanning [first, last]; optionally under a legacy alias spelling."""
    dates = pd.date_range(start=first, end=last, freq=freq)
    df = _frame(dates)
    df["effective_date"] = df["Date"]
    native_cache.write_timeseries(PROVIDER, symbol, spelling or interval, df)
    path = native_cache.find_timeseries_path(PROVIDER, symbol, spelling or interval)
    assert path is not None
    old = (FROZEN_NOW - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (old, old))
    return path


# ---------------------------------------------------------------------------
# D1 -- the 65 GB bug
# ---------------------------------------------------------------------------

def test_empty_asof_slice_is_not_treated_as_a_cache_miss(cache_dir):
    """A symbol whose first cached bar postdates the as_of must NOT re-download.

    Pre-fix this issued one ``historical-price-full`` call spanning ~15 years.
    """
    _seed("LATE", "1d", "2022-06-06", "2025-12-31")
    p = _StubProvider(rows_for=_fmp_like("2022-06-06", "2025-12-31"))

    got = p.get_ohlcv_data(
        "LATE",
        start_date=datetime(2019, 6, 30),
        end_date=datetime(2020, 6, 30),
        interval="1d",
    )

    assert p.impl_calls == [], (
        "an EMPTY as_of slice from a POPULATED cache was mistaken for an absent "
        f"cache and re-fetched the full history: {p.impl_calls}"
    )
    assert got.empty, "no bars exist at or below this as_of, so the frame must be empty"


def test_repeated_empty_asof_reads_issue_zero_calls(cache_dir):
    """The loop that produced the 146k: 250 as_of reads below the cache floor.

    Pre-fix every single one was a full-history download (250 calls / ~110 MB of
    payload for ONE symbol). After: zero.
    """
    _seed("LATE", "1d", "2022-06-06", "2025-12-31")
    p = _StubProvider(rows_for=_fmp_like("2022-06-06", "2025-12-31"))

    as_ofs = pd.date_range("2020-01-06", periods=250, freq="B")
    for as_of in as_ofs:
        p.get_ohlcv_data(
            "LATE",
            start_date=as_of.to_pydatetime() - timedelta(days=400),
            end_date=as_of.to_pydatetime(),
            interval="1d",
        )

    assert len(p.impl_calls) == 0, (
        f"{len(p.impl_calls)} source fetches for 250 cached reads -- the cache is "
        f"being bypassed on every read"
    )


def test_empty_asof_slice_intraday_is_not_a_miss(cache_dir):
    """Same defect on the 5-minute path, which fans out into ~8-day chunked calls."""
    _seed("LATE", "5m", "2022-06-06 09:30", "2022-06-10 15:55", freq="5min")
    p = _StubProvider(
        rows_for=_fmp_like("2022-06-06 09:30", "2022-06-10 15:55", freq="5min"))

    try:
        got = p.get_ohlcv_data(
            "LATE",
            start_date=datetime(2021, 1, 4),
            end_date=datetime(2021, 3, 1),
            interval="5m",
        )
    except Exception as exc:
        # Pre-fix this took the cold-fetch branch, found nothing in 2021 (the symbol
        # was not listed yet) and turned a legitimate "no bars at this as_of" into a
        # hard error -- after fanning out the ~8-day chunked HTTP calls first.
        pytest.fail(
            f"intraday empty as_of slice took the cold-fetch path: "
            f"calls={p.impl_calls} exc={exc!r}"
        )

    assert p.impl_calls == [], (
        f"intraday empty as_of slice re-fetched the window: {p.impl_calls}"
    )
    assert got.empty


def test_absent_cache_still_cold_fetches_once(cache_dir):
    """Guard against over-fixing: with NO cache file at all we must still fetch."""
    p = _StubProvider(
        rows_for=lambda s, e, i: _frame(pd.date_range("2024-01-01", "2024-03-01", freq="D"))
    )

    got = p.get_ohlcv_data(
        "COLD",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 3, 1),
        interval="1d",
    )

    assert len(p.impl_calls) == 1, "an absent cache must still cold-fetch exactly once"
    assert not got.empty
    assert native_cache.find_timeseries_path(PROVIDER, "COLD", "1d") is not None


def test_zero_row_parquet_on_disk_is_still_a_miss(cache_dir):
    """A file that exists but holds NO bars is a broken cache, not a legitimate hit.

    Without this distinction the D1 fix would turn an empty/corrupt cache file into a
    permanent "this symbol has no data" answer that never self-heals.
    """
    empty = _frame([])
    empty["effective_date"] = empty["Date"]
    native_cache.write_timeseries(PROVIDER, "HOLLOW", "1d", empty)
    assert native_cache.find_timeseries_path(PROVIDER, "HOLLOW", "1d") is not None

    p = _StubProvider(
        rows_for=lambda s, e, i: _frame(pd.date_range("2024-01-01", "2024-03-01", freq="D"))
    )
    p.get_ohlcv_data(
        "HOLLOW",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 3, 1),
        interval="1d",
    )

    assert len(p.impl_calls) == 1, "a 0-row cache file must be refilled, not served"


# ---------------------------------------------------------------------------
# Pinned backtest as_of: never re-fetch, byte-identical rows
# ---------------------------------------------------------------------------

def test_pinned_asof_inside_coverage_never_refetches(cache_dir):
    """Immutable history. A pinned as_of inside the cached range is a pure read."""
    _seed("PIN", "1d", "2022-06-06", "2025-12-31", age_hours=400 * 24)
    p = _StubProvider()

    p.get_ohlcv_data(
        "PIN",
        start_date=datetime(2023, 1, 3),
        end_date=datetime(2023, 6, 30),
        interval="1d",
    )

    assert p.impl_calls == [], "a pinned historical as_of must never re-fetch"


def test_pinned_asof_rows_are_byte_identical_to_the_raw_cache_slice(cache_dir):
    """Reproducibility: the returned rows equal a direct sliced parquet read, exactly."""
    _seed("PIN", "1d", "2022-06-06", "2025-12-31", age_hours=400 * 24)
    start, end = datetime(2023, 1, 3), datetime(2023, 6, 30)

    expected = native_cache.read_timeseries(PROVIDER, "PIN", "1d", as_of=end)
    expected = expected.drop(columns=["effective_date"])
    expected = expected[
        (pd.to_datetime(expected["Date"]) >= pd.Timestamp(start))
        & (pd.to_datetime(expected["Date"]) <= pd.Timestamp(end))
    ].reset_index(drop=True)
    # validate_date_range normalizes the filter dates to UTC, so get_ohlcv_data
    # localizes the tz-naive cache frame on the way out. Match that here; the ROW SET
    # is what this test pins.
    expected["Date"] = expected["Date"].dt.tz_localize(timezone.utc)

    got = _StubProvider().get_ohlcv_data(
        "PIN", start_date=start, end_date=end, interval="1d"
    ).reset_index(drop=True)

    pd.testing.assert_frame_equal(got, expected)
    assert len(got) > 100, "sanity: the window really does hold rows"


# ---------------------------------------------------------------------------
# Live path: still tops up, and only the MISSING TAIL
# ---------------------------------------------------------------------------

def test_live_daily_still_tops_up_a_stale_cache(cache_dir):
    """A stale live daily read must still refresh -- a stale price is a money bug."""
    _seed("LIVE", "1d", "2025-06-02", "2026-02-27", age_hours=30 * 24)
    p = _StubProvider(
        rows_for=lambda s, e, i: _frame(pd.date_range(start=s, end=min(e, FROZEN_NOW), freq="D"))
    )

    p.get_ohlcv_data("LIVE", end_date=FROZEN_NOW_UTC, interval="1d", lookback_days=30)

    assert len(p.impl_calls) == 1, "a 30-day-stale live daily cache must be topped up"


def test_live_topup_fetches_only_the_missing_tail(cache_dir):
    """The top-up range must start at the last cached bar, not at now-15y."""
    _seed("LIVE", "1d", "2025-06-02", "2026-02-27", age_hours=30 * 24)
    p = _StubProvider(
        rows_for=lambda s, e, i: _frame(pd.date_range(start=s, end=min(e, FROZEN_NOW), freq="D"))
    )

    p.get_ohlcv_data("LIVE", end_date=FROZEN_NOW_UTC, interval="1d", lookback_days=30)

    assert len(p.impl_calls) == 1
    call = p.impl_calls[0]
    assert call["start"].date() == (datetime(2026, 2, 27) + timedelta(days=1)).date(), (
        f"top-up started at {call['start']} -- it must resume from the last cached bar"
    )
    span_days = (call["end"] - call["start"]).days
    assert span_days <= 45, (
        f"top-up asked for {span_days} days of history; an incremental refresh must "
        f"fetch only the missing tail, never the whole series"
    )


def test_fresh_live_cache_is_not_topped_up(cache_dir):
    """The freshness gate must not be inverted: a fresh file makes no call."""
    _seed("LIVE", "1d", "2025-06-02", "2026-03-13", age_hours=1)
    p = _StubProvider()

    p.get_ohlcv_data("LIVE", end_date=FROZEN_NOW_UTC, interval="1d", lookback_days=30)

    assert p.impl_calls == [], "a 1-hour-old daily cache must not be re-fetched"


# ---------------------------------------------------------------------------
# D2 -- a cache write must never lose history
# ---------------------------------------------------------------------------

def test_cache_write_merges_instead_of_replacing(cache_dir):
    """A narrow fetch must not destroy a wide cache.

    The intraday cold-fetch range is clamped to the caller's window, so a
    replace-on-write turned one narrow miss into a permanently truncated cache --
    which then makes every read outside the sliver a fresh full download.
    """
    _seed("MERGE", "5m", "2022-06-06 09:30", "2022-06-10 15:55", freq="5min")
    before = pd.read_parquet(native_cache.find_timeseries_path(PROVIDER, "MERGE", "5m"))

    narrow = _frame(pd.date_range("2026-03-09 09:30", "2026-03-09 15:55", freq="5min"))
    _StubProvider()._write_ohlcv_parquet(narrow, PROVIDER, "MERGE", "5m")

    after = pd.read_parquet(native_cache.find_timeseries_path(PROVIDER, "MERGE", "5m"))
    assert len(after) == len(before) + len(narrow), (
        f"cache went from {len(before)} to {len(after)} rows after writing "
        f"{len(narrow)} new ones -- the write REPLACED instead of merging"
    )
    assert pd.Timestamp(after["Date"].min()) == pd.Timestamp(before["Date"].min())
    assert pd.Timestamp(after["Date"].max()) == pd.Timestamp("2026-03-09 15:55")


def test_cache_write_merge_is_idempotent(cache_dir):
    """Re-writing rows already present must not duplicate them."""
    _seed("MERGE", "1d", "2024-01-01", "2024-03-01")
    path = native_cache.find_timeseries_path(PROVIDER, "MERGE", "1d")
    before = len(pd.read_parquet(path))

    same = _frame(pd.date_range("2024-01-01", "2024-03-01", freq="D"))
    _StubProvider()._write_ohlcv_parquet(same, PROVIDER, "MERGE", "1d")

    assert len(pd.read_parquet(path)) == before


# ---------------------------------------------------------------------------
# D3 -- an alias-spelled write must not shadow the real cache
# ---------------------------------------------------------------------------

def test_alias_spelled_write_targets_the_existing_file(cache_dir):
    """A "5m" write must land in an existing "<SYM>_5min.parquet", not beside it.

    Real damage found on the dev cache: ``AMC_5m.parquet`` (596 bars,
    2026-06-09..2026-06-18) shadows ``AMC_5min.parquet`` (69 621 bars,
    2022-06-06..2025-12-31), because ``find_timeseries_path`` prefers the canonical
    spelling on read while ``write_timeseries`` used the caller's spelling.
    """
    _seed("SHADOW", "5min", "2022-06-06 09:30", "2022-06-10 15:55", freq="5min")
    legacy = os.path.join(cache_dir, PROVIDER, "SHADOW_5min.parquet")
    canonical = os.path.join(cache_dir, PROVIDER, "SHADOW_5m.parquet")
    assert os.path.exists(legacy)
    big = len(pd.read_parquet(legacy))

    new = _frame(pd.date_range("2026-03-09 09:30", "2026-03-09 15:55", freq="5min"))
    _StubProvider()._write_ohlcv_parquet(new, PROVIDER, "SHADOW", "5m")

    assert not os.path.exists(canonical), (
        "a '5m' write created a second parquet that SHADOWS the complete '5min' cache"
    )
    assert len(pd.read_parquet(legacy)) == big + len(new)


def test_alias_spelled_read_after_write_sees_the_full_history(cache_dir):
    """End-to-end: after a '5m' cold write, a '5m' read still sees the '5min' history."""
    _seed("SHADOW", "5min", "2022-06-06 09:30", "2022-06-10 15:55", freq="5min")
    new = _frame(pd.date_range("2026-03-09 09:30", "2026-03-09 15:55", freq="5min"))
    _StubProvider()._write_ohlcv_parquet(new, PROVIDER, "SHADOW", "5m")

    p = _StubProvider(
        rows_for=_fmp_like("2022-06-06 09:30", "2022-06-10 15:55", freq="5min"))
    got = p.get_ohlcv_data(
        "SHADOW",
        start_date=datetime(2022, 6, 6),
        end_date=datetime(2022, 6, 10, 23, 59),
        interval="5m",
    )

    assert p.impl_calls == [], "the 2022 window is cached; nothing should be fetched"
    assert len(got) > 100, f"only {len(got)} bars -- the 5min history is still shadowed"


# ---------------------------------------------------------------------------
# A failed / over-limit fetch must never be cached
# ---------------------------------------------------------------------------

def test_failed_fetch_writes_nothing_to_the_cache(cache_dir):
    """An exhausted-retry (429/over-limit) fetch propagates and caches nothing.

    Under the D1 fix a cached empty frame would become a permanent "this symbol has
    no data" hit, so writing a failure is strictly worse than not writing at all.
    """

    class _Boom(_StubProvider):
        def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval):
            super()._get_ohlcv_data_impl(symbol, start_date, end_date, interval)
            raise RuntimeError("FMP historical-price-full failed after 4 attempts (HTTP 429)")

    p = _Boom()
    with pytest.raises(RuntimeError, match="429"):
        p.get_ohlcv_data(
            "RATELIMITED",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            interval="1d",
        )

    assert len(p.impl_calls) == 1
    assert native_cache.find_timeseries_path(type(p).__name__, "RATELIMITED", "1d") is None, (
        "a rate-limited failure was written to the cache"
    )


def test_empty_fetch_result_writes_nothing_to_the_cache(cache_dir):
    """FMP's 200-with-no-'historical' form surfaces as an EMPTY frame -- never cache it."""
    p = _StubProvider()  # rows_for=None -> always empty

    with pytest.raises(Exception, match="Failed to fetch data"):
        p.get_ohlcv_data(
            "NODATA",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 3, 1),
            interval="1d",
        )

    assert native_cache.find_timeseries_path(PROVIDER, "NODATA", "1d") is None, (
        "an empty fetch result was cached -- it would read as a permanent 'no data' hit"
    )


# ---------------------------------------------------------------------------
# Gaps a mutation run found (survivors M14/M15/M16/M17)
# ---------------------------------------------------------------------------

def test_merge_lets_a_revised_bar_overwrite_the_cached_one(cache_dir):
    """A re-fetched bar must WIN over the cached copy of the same timestamp.

    FMP revises late-reported volume/closes. A merge that kept the FIRST duplicate
    would pin the stale value forever, and no row count would ever reveal it.
    """
    _seed("REVISE", "1d", "2024-01-01", "2024-01-10")
    path = native_cache.find_timeseries_path(PROVIDER, "REVISE", "1d")

    revised = _frame(pd.date_range("2024-01-08", "2024-01-10", freq="D"))
    revised["Close"] = 99.0
    _StubProvider()._write_ohlcv_parquet(revised, PROVIDER, "REVISE", "1d")

    on_disk = pd.read_parquet(path).set_index("Date")
    assert on_disk.loc[pd.Timestamp("2024-01-09"), "Close"] == 99.0, (
        "the merge kept the STALE cached bar instead of the freshly fetched revision"
    )
    assert on_disk.loc[pd.Timestamp("2024-01-02"), "Close"] == 10.5
    assert len(on_disk) == 10


def test_merged_cache_stays_ascending(cache_dir):
    """Bars must stay in ascending Date order after a merge.

    Every reader (searchsorted slicing in the backtest price source, ``iloc[-1]`` in
    the top-up) assumes ascending order; a descending cache silently mis-slices.
    """
    _seed("ORDER", "1d", "2024-02-01", "2024-02-10")
    earlier = _frame(pd.date_range("2024-01-01", "2024-01-10", freq="D"))
    _StubProvider()._write_ohlcv_parquet(earlier, PROVIDER, "ORDER", "1d")

    dates = pd.read_parquet(native_cache.find_timeseries_path(PROVIDER, "ORDER", "1d"))["Date"]
    assert list(dates) == sorted(dates), "merged cache is not in ascending Date order"


def test_asof_read_never_returns_bars_after_the_asof(cache_dir):
    """No lookahead: the effective_date ceiling must actually cut the series."""
    _seed("NOLOOK", "1d", "2024-01-01", "2024-12-31")
    end = datetime(2024, 3, 15)

    got = _StubProvider().get_ohlcv_data(
        "NOLOOK", start_date=datetime(2024, 1, 1), end_date=end, interval="1d"
    )

    assert not got.empty
    latest = pd.to_datetime(got["Date"]).max()
    assert latest.tz_localize(None) <= pd.Timestamp(end), (
        f"as_of read leaked a bar from {latest}, after the {end} as_of"
    )


def test_intraday_cold_fetch_honours_the_requested_start(cache_dir):
    """An intraday cache-fill must ask for the CALLER's window, not a deep default.

    The 5-minute endpoint is chunked into ~8-calendar-day HTTP calls, so widening the
    fill range from a 2-month window to 15 years turns ~8 calls into ~685.
    """
    p = _StubProvider(rows_for=_fmp_like("2020-01-02 09:30", "2026-01-01", freq="5min"))

    p.get_ohlcv_data(
        "COLD5",
        start_date=datetime(2024, 1, 2),
        end_date=datetime(2024, 3, 1),
        interval="5m",
    )

    assert len(p.impl_calls) == 1
    call = p.impl_calls[0]
    assert call["start"].date() == datetime(2024, 1, 2).date(), (
        f"intraday cache-fill started at {call['start']} instead of the requested "
        f"2024-01-02 -- it is pulling years of bars nobody asked for"
    )
    assert (call["end"] - call["start"]).days <= 70


def test_daily_cold_fetch_is_bounded(cache_dir):
    """The daily pre-fill window is deep BY DESIGN (15y) -- pin it so it cannot grow."""
    p = _StubProvider(rows_for=_fmp_like("2015-01-01", "2026-01-01"))

    p.get_ohlcv_data(
        "COLD1D", start_date=datetime(2024, 1, 2), end_date=datetime(2024, 3, 1), interval="1d"
    )

    assert len(p.impl_calls) == 1
    span = (p.impl_calls[0]["end"] - p.impl_calls[0]["start"]).days
    assert 365 * 14 <= span <= 365 * 16, f"daily pre-fill span changed to {span} days"


def test_read_timeseries_enforces_the_effective_date_ceiling(cache_dir):
    """The parquet as_of ceiling itself, tested at the substrate.

    ``get_ohlcv_data`` re-applies its own Date<=end_date mask afterwards, so a broken
    ceiling in ``read_timeseries`` is INVISIBLE through the public method -- yet every
    other consumer of ``read_timeseries`` (and the no-lookahead guarantee the whole
    as_of store rests on) depends on it. A mutation run found this gap: nothing in any
    suite pinned it.
    """
    _seed("CEIL", "1d", "2024-01-01", "2024-12-31")

    sliced = native_cache.read_timeseries(
        PROVIDER, "CEIL", "1d", as_of=datetime(2024, 3, 15))

    assert sliced is not None and not sliced.empty
    assert pd.to_datetime(sliced["Date"]).max() <= pd.Timestamp("2024-03-15"), (
        "read_timeseries returned bars ABOVE the as_of ceiling -- lookahead"
    )
    unsliced = native_cache.read_timeseries(PROVIDER, "CEIL", "1d", as_of=None)
    assert len(unsliced) > len(sliced), "as_of=None must return the whole series"
