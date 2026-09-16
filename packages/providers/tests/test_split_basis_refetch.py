"""Split-basis drift (plan Task 6) and the refusal that protects the cache from its repair.

The FMP cache top-up APPENDS bars, so a symbol that splits after its file was first fetched ends
up on a mixed basis. Two halves, deliberately separated (final review I1):

* the LIVE refresh path only REPORTS it -- one WARNING per symbol per process. It runs inside the
  analysis pass for every symbol of every expert, and an automatic 15-year re-fetch there is
  unbounded and unrate-limited;
* :meth:`force_full_refetch` REPAIRS it, and is called by a tool an operator runs on purpose
  (``warm_market_conditions.py --fetch-missing``).

And the repair itself is guarded (final review C1): a fetch that returns LESS history than the
cache already holds is refused rather than written, because the marker it would write afterwards
makes every later check say ``refetched`` -- so the loss would hide itself.

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_split_basis_refetch.py -q -p no:cacheprovider
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.split_basis import (
    CalendarSplit,
    check_split_basis,
    full_fetch_marker_path,
    needs_full_refetch,
    read_full_fetch_marker,
)
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

SPLIT_DAY = date(2025, 2, 3)


def _sessions(a, b):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


def _truth(last: date) -> pd.DataFrame:
    sessions = _sessions(date(2023, 1, 3), last)
    rng = np.random.default_rng(5)
    n = len(sessions)
    c = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.011, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"Date": pd.to_datetime(sessions), "Open": o, "High": np.maximum(o, c) * 1.004,
                         "Low": np.minimum(o, c) * 0.996, "Close": c,
                         "Volume": rng.integers(1e6, 5e6, n).astype(float)})


def _unadjusted_before(df, day, factor):
    out = df.copy()
    pre = out["Date"] < pd.Timestamp(day)
    for col in ("Open", "High", "Low", "Close"):
        out.loc[pre, col] = out.loc[pre, col] * factor
    return out


class _Provider(FMPOHLCVProvider):
    """The real provider with the network replaced by a truth frame."""

    def __init__(self, truth, splits):
        super().__init__(api_key="test-key")
        self.truth = truth
        self.splits = splits
        self.impl_calls = []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.impl_calls.append((symbol, start_date.date(), end_date.date(), interval))
        df = self.truth
        return df[(df["Date"] >= pd.Timestamp(start_date.date())) & (df["Date"] <= pd.Timestamp(end_date.date()))] \
            .reset_index(drop=True).copy()

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


def _write_cache(symbol, df):
    out = df.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries("FMPOHLCVProvider", symbol, "1d", out)
    return native_cache.find_timeseries_path("FMPOHLCVProvider", symbol, "1d")


@pytest.fixture(autouse=True)
def _forget_reported_symbols():
    """The "reported once" memo is process-wide; each test gets a clean one."""
    from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface

    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()
    yield
    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()


def _full_fetches(provider):
    return [c for c in provider.impl_calls if (datetime.now().date() - c[1]).days > 365 * 14]


def test_a_drifted_file_is_REPORTED_by_the_live_refresh_and_not_refetched(tmp_path, caplog):
    """THE LIVE PATH. It notices, says so once, and changes nothing.

    An automatic repair here would fire inside the analysis pass, for every symbol of every
    expert, gated or not -- and on the first refresh after this feature ships no file carries a
    marker, so every symbol whose historical split factor is below MIN_DETECTABLE_FACTOR
    classifies ``undetectable`` and would trigger a 15-year re-fetch of its own.
    """
    symbol = "XSPLIT"
    yesterday = date.today() - timedelta(days=1)
    truth = _truth(yesterday)
    cached = _unadjusted_before(truth[truth["Date"] <= pd.Timestamp("2025-02-28")], SPLIT_DAY, 2.0)
    path = _write_cache(symbol, cached)
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])

    # The mixed basis is exactly what the checker sees.
    pre = pd.read_parquet(path)
    checks = check_split_basis(pre["Date"].to_numpy(), pre["Open"], pre["High"], pre["Low"], pre["Close"],
                               provider.splits, symbol=symbol, marker=read_full_fetch_marker(path))
    assert [c.verdict for c in checks] == ["drift"] and needs_full_refetch(checks)

    with caplog.at_level("WARNING"):
        df = provider._refresh_parquet_if_stale(pre.copy(), symbol, "1d", "FMPOHLCVProvider")

    assert _full_fetches(provider) == []                 # NO 15-year fetch
    assert read_full_fetch_marker(path) is None          # and therefore no marker
    # The ordinary tail TOP-UP still happens (that is what this path is for), but the cached
    # HISTORY is untouched: same first bar, and the drifted pre-split prices are still drifted --
    # nothing was replaced from the vendor.
    after = pd.read_parquet(path)
    assert after["Date"].min() == pre["Date"].min()
    same = after.merge(pre, on="Date", suffixes=("_a", "_b"))
    assert len(same) == len(pre) and np.allclose(same["Close_a"], same["Close_b"])
    assert not np.allclose(
        after[after["Date"] < pd.Timestamp(SPLIT_DAY)]["Close"].to_numpy(),
        truth[truth["Date"] < pd.Timestamp(SPLIT_DAY)]["Close"].to_numpy())
    assert len(df) >= len(pre)
    said = [r.getMessage() for r in caplog.records if symbol in r.getMessage()]
    assert len(said) == 1
    assert "warm_market_conditions" in said[0] and "NOT repaired here" in said[0]

    # ONE warning per symbol per process, not one per refresh: this runs on every daily top-up.
    caplog.clear()
    with caplog.at_level("WARNING"):
        provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", "FMPOHLCVProvider")
    assert [r.getMessage() for r in caplog.records if symbol in r.getMessage()] == []


def test_the_explicit_repair_replaces_the_file_and_writes_the_marker(tmp_path):
    """THE REPAIR PATH, which an operator reaches through the warm tool: a full-history fetch, a
    REPLACED file, a marker -- and the marker then turns the verdict into ``refetched`` so the
    repair happens once."""
    symbol = "XREPAIR"
    truth = _truth(date.today() - timedelta(days=1))
    cached = _unadjusted_before(truth[truth["Date"] <= pd.Timestamp("2025-02-28")], SPLIT_DAY, 2.0)
    path = _write_cache(symbol, cached)
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])

    out = provider.force_full_refetch(symbol, "1d", provider_name="FMPOHLCVProvider")

    full = _full_fetches(provider)
    assert len(full) == 1 and (datetime.now().date() - full[0][1]).days > 365 * 14
    after = pd.read_parquet(path)
    assert len(after) == len(truth) and len(out) == len(truth)
    merged = after.merge(truth, on="Date", suffixes=("_c", "_t"))
    assert len(merged) == len(truth) and np.allclose(merged["Close_c"], merged["Close_t"])

    marker = read_full_fetch_marker(path)
    assert marker and marker["fetched_on_utc"] == marker["fetched_at_utc"][:10]
    assert marker["fetched_at_utc"].endswith("+00:00")
    written_at = datetime.fromisoformat(marker["fetched_at_utc"])
    assert 0 <= (datetime.now(timezone.utc) - written_at).total_seconds() < 300
    assert marker["rows"] == len(truth)
    assert marker["first_bar"] == truth["Date"].iloc[0].date().isoformat()
    assert marker["last_bar"] == truth["Date"].iloc[-1].date().isoformat()

    checks = check_split_basis(after["Date"].to_numpy(), after["Open"], after["High"], after["Low"],
                               after["Close"], provider.splits, symbol=symbol,
                               marker=read_full_fetch_marker(path))
    assert [c.verdict for c in checks] == ["refetched"] and not needs_full_refetch(checks)

    # And the live refresh then has nothing to report.
    provider._refresh_parquet_if_stale(after.copy(), symbol, "1d", "FMPOHLCVProvider")
    assert len(_full_fetches(provider)) == 1


# --------------------------------------------------------- the replacement must not LOSE history
class _ShortProvider(_Provider):
    """A vendor that answers a full-history request with a truncated frame: a capped plan or
    endpoint, a partial payload, a shortened history window."""

    def __init__(self, truth, splits, keep_last=None, start_from=None):
        super().__init__(truth, splits)
        self.keep_last = keep_last
        self.start_from = start_from

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        df = super()._get_ohlcv_data_impl(symbol, start_date, end_date, interval)
        if self.start_from is not None:
            df = df[df["Date"] >= pd.Timestamp(self.start_from)]
        if self.keep_last is not None:
            df = df.tail(self.keep_last)
        return df.reset_index(drop=True).copy()


@pytest.mark.parametrize("kwargs,why", [
    ({"keep_last": 30}, "a capped plan or endpoint: far fewer rows"),
    ({"start_from": date(2024, 6, 3)}, "a shortened vendor window: same tail, later first bar"),
])
def test_a_refetch_that_returns_LESS_history_is_refused_and_changes_nothing(tmp_path, kwargs, why):
    """C1. Without this, ``empty`` was the only guard: a short-but-non-empty answer replaced
    fifteen years of daily bars in the cache the live platform, every backtest and every warmed
    snapshot read -- and the marker written afterwards made every later check say ``refetched``,
    so the loss hid itself.
    """
    symbol = "XSHORT"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, truth)
    before = open(path, "rb").read()
    provider = _ShortProvider(truth, [CalendarSplit(SPLIT_DAY, 2.0)], **kwargs)

    with pytest.raises(RuntimeError) as e:
        provider.force_full_refetch(symbol, "1d", provider_name="FMPOHLCVProvider")

    msg = str(e.value)
    assert symbol in msg and "1d" in msg and "LESS history" in msg
    # BOTH sides, so the message says what was offered as well as what would be lost.
    short = provider._get_ohlcv_data_impl(symbol, datetime.now() - timedelta(days=365 * 15),
                                          datetime.now(), "1d")
    assert f"{len(short)} rows from {short['Date'].iloc[0].date().isoformat()}" in msg
    assert (f"cached {len(truth)} rows from "
            f"{truth['Date'].iloc[0].date().isoformat()}") in msg
    assert open(path, "rb").read() == before, why              # byte-identical
    assert read_full_fetch_marker(path) is None                # and NO marker


def test_a_refetch_that_keeps_every_bar_is_allowed(tmp_path):
    """The control: equal rows and an equal first bar is not a loss, so it replaces normally --
    the guard is about losing history, not about growing."""
    symbol = "XSAME"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, truth)
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])
    out = provider.force_full_refetch(symbol, "1d", provider_name="FMPOHLCVProvider")
    assert len(out) == len(truth)
    assert read_full_fetch_marker(path)["rows"] == len(truth)


def test_an_unreadable_existing_cache_refuses_rather_than_overwriting(tmp_path, monkeypatch):
    """A file that cannot be READ must not be treated as "no history": that would turn the one
    failure this guards into a silent pass."""
    symbol = "XUNREADABLE"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, truth)
    before = open(path, "rb").read()
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])

    real_read = pd.read_parquet

    def _boom(p, *a, **k):
        if str(p) == str(path) and k.get("columns") == ["Date"]:
            raise OSError("parquet footer is corrupt")
        return real_read(p, *a, **k)

    monkeypatch.setattr(pd, "read_parquet", _boom)
    with pytest.raises(RuntimeError, match="could not be read"):
        provider.force_full_refetch(symbol, "1d", provider_name="FMPOHLCVProvider")
    monkeypatch.undo()
    assert open(path, "rb").read() == before
    assert read_full_fetch_marker(path) is None


def test_a_cold_full_fetch_has_nothing_to_compare_against(tmp_path):
    """No cached file at all: the guard must not stand in the way of a first fill."""
    symbol = "XCOLD"
    truth = _truth(date.today() - timedelta(days=1))
    provider = _Provider(truth, [])
    out = provider.force_full_refetch(symbol, "1d", provider_name="FMPOHLCVProvider")
    assert len(out) == len(truth)
    path = native_cache.find_timeseries_path("FMPOHLCVProvider", symbol, "1d")
    assert read_full_fetch_marker(path)["rows"] == len(truth)


def test_consistent_file_is_not_refetched(tmp_path):
    symbol = "XCLEAN"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, truth)
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])
    df = pd.read_parquet(path)
    before = pd.read_parquet(path)
    provider._refresh_parquet_if_stale(df.copy(), symbol, "1d", "FMPOHLCVProvider")
    assert all(c[3] == "1d" and (datetime.now().date() - c[1]).days < 30 for c in provider.impl_calls), \
        provider.impl_calls
    assert read_full_fetch_marker(path) is None
    after = pd.read_parquet(path)
    assert len(after) == len(before) and np.allclose(after["Close"], before["Close"])


def test_split_calendar_failure_does_not_break_the_refresh(tmp_path):
    symbol = "XERR"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, _unadjusted_before(truth, SPLIT_DAY, 2.0))

    class Broken(_Provider):
        def _split_calendar(self, symbol, interval):
            raise RuntimeError("FMP down")

    provider = Broken(truth, [])
    df = pd.read_parquet(path)
    out = provider._refresh_parquet_if_stale(df.copy(), symbol, "1d", "FMPOHLCVProvider")
    assert len(out) == len(df)
    assert not [c for c in provider.impl_calls if (datetime.now().date() - c[1]).days > 365 * 14]


def test_marker_path_is_out_of_the_parquet_glob(tmp_path):
    p = str(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet")
    marker = full_fetch_marker_path(p)
    assert marker.endswith("_split_basis" + "\\" + "AAPL_1d.json") or marker.endswith("_split_basis/AAPL_1d.json")


def test_fmp_split_calendar_parses_the_real_payload(monkeypatch):
    """``FMPOHLCVProvider._split_calendar`` over FMP's ``historical-price-full/stock_split``
    payload: dates and numerator/denominator ratios, an unknown ratio dropped, and no call at all
    for an interval this provider does not serve daily bars for."""
    from ba2_providers import symbol_info
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

    payload = {"symbol": "XYZ", "historical": [
        {"date": "2024-06-10", "label": "June 10, 24", "numerator": 10.0, "denominator": 1.0},
        {"date": "2020-08-31", "numerator": 4, "denominator": 1},
        {"date": "2022-07-18", "numerator": 1, "denominator": 20},      # reverse split
        {"date": "2021-01-04", "numerator": None, "denominator": 1},    # unknown: dropped
    ]}
    seen = []

    def fake_fetch_splits(api_key, symbol):
        seen.append((api_key, symbol))
        return payload

    monkeypatch.setattr(symbol_info, "fetch_splits", fake_fetch_splits)
    provider = FMPOHLCVProvider(api_key="test-key")

    splits = provider._split_calendar("XYZ", "1d")
    assert seen == [("test-key", "XYZ")]
    assert [(c.date, c.ratio) for c in splits] == [
        (date(2020, 8, 31), 4.0), (date(2022, 7, 18), 0.05), (date(2024, 6, 10), 10.0)]

    assert provider._split_calendar("XYZ", "5min") is None
    assert provider._split_calendar("XYZ", "1wk") is None    # not in TIMEFRAME_MAP at all
    assert len(seen) == 1                                    # neither asked FMP
