"""The namespace fetch table and the warm fetcher built on it (spec step 4).

The load-bearing property is a ROUND TRIP: the file a fetch writes must be the file
the planner then reports ``present``. When it is not, every plan re-lists the same
requirement as missing and the warm re-downloads it forever -- silently, because each
individual run looks like it worked. That is exactly what a fetcher keyed on one
instance-wide provider name did: the planner resolved a ``fmp`` price requirement
against ``FMPOHLCVProvider``'s directory while the fetch wrote the host indicator
stack's ``YFinanceDataProvider`` one.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import dependencies as dep
from ba2_experts import warm_fetchers
from ba2_providers import fmp_common
from ba2_providers.warm import planner

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
WINDOW = dep.Window(start=NOW - timedelta(days=30), end=NOW)


def _history_req(namespace="price_target", symbol="AAPL", window=None):
    return dep.Requirement(provider="fmp", namespace=namespace, symbol=symbol,
                           window=window or WINDOW, interval=None, kind=dep.KIND_HISTORY,
                           optional=False, reason="t")


def _timeseries_req(symbol="AAPL", provider="fmp", interval="1d", window=None):
    return dep.Requirement(provider=provider, namespace="ohlcv", symbol=symbol,
                           window=window or WINDOW, interval=interval,
                           kind=dep.KIND_TIMESERIES, optional=False, reason="t")


def _indicator_req(symbol="AAPL", window=None):
    return dep.Requirement(provider="indicators", namespace="atr_14", symbol=symbol,
                           window=window or WINDOW, interval="1d", kind=dep.KIND_INDICATOR,
                           optional=False, reason="t")


def _fetcher(indicator_provider="yfinance", **kwargs):
    return warm_fetchers.DefaultWarmFetcher(
        indicator_ohlcv_provider=indicator_provider,
        end_date_provider=kwargs.pop("end_date_provider", lambda: NOW),
        fmp_key=kwargs.pop("fmp_key", "a-key"), fred_key=kwargs.pop("fred_key", None),
        **kwargs)


@pytest.fixture(autouse=True)
def api_keys():
    """Put a throwaway FMP key in the (isolated) app-settings DB.

    The providers read it with ``ba2_common.config.get_app_setting`` at CONSTRUCTION
    and refuse to exist without one. Patching that function is not enough -- each
    provider module imported the NAME, so there are several bindings -- and the row is
    what the real code path reads anyway. Nothing here reaches a network: every fetch
    is stubbed.
    """
    from ba2_common.core.db import add_instance, get_setting
    from ba2_common.core.models import AppSetting

    if get_setting("FMP_API_KEY") is None:
        add_instance(AppSetting(key="FMP_API_KEY", value_str="test-key"))
    yield


@pytest.fixture(autouse=True)
def clean_history_memo():
    """Drop the in-process fmp_history memo between tests.

    It is keyed ``(namespace, symbol)`` and outlives a test: a second test warming the
    same key would be served the FIRST one's payload and never touch the disk, so the
    round trip it is checking would silently not happen.
    """
    from ba2_providers.fmp_common import _HISTORY_MEM_CACHE

    for key in ("price_target__AAPL", "grades_historical__AAPL", "insider_v2__AAPL"):
        _HISTORY_MEM_CACHE.invalidate(key)
    yield
    for key in ("price_target__AAPL", "grades_historical__AAPL", "insider_v2__AAPL"):
        _HISTORY_MEM_CACHE.invalidate(key)


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point every cache reader AND writer at one temp root."""
    import ba2_common.config as common_config
    import ba2_common.core.native_cache as native_cache

    monkeypatch.setattr(common_config, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #
def test_every_namespace_the_adapters_declare_has_a_fetcher():
    """A declaration nothing can satisfy is a plan that never completes."""
    import ba2_experts.replay_dependencies as adapters

    table = warm_fetchers.NamespaceFetchers().table
    declared = set(adapters.STATEMENT_NAMESPACES) | {
        "price_target", "grades_historical", "analyst_grades",
        adapters.ESTIMATOR_EARNINGS_NAMESPACE, adapters.ESTIMATOR_ESTIMATES_NAMESPACE,
        "insider_v2"}

    assert declared <= set(table)


def test_the_provider_instances_are_built_once_and_shared():
    table = warm_fetchers.NamespaceFetchers()

    assert table.details() is table.details()
    assert table.insider() is table.insider()


def test_an_unknown_namespace_is_refused_by_name():
    with pytest.raises(warm_fetchers.WarmFetchError) as excinfo:
        _fetcher()(_history_req("not_a_namespace"))

    assert "not_a_namespace" in str(excinfo.value)


def test_a_namespace_that_needs_the_api_key_refuses_without_one():
    with pytest.raises(warm_fetchers.WarmFetchError):
        _fetcher(fmp_key=None)(_history_req("price_target"))


def test_platform_state_is_refused_because_nothing_can_download_it():
    state = dep.Requirement(provider="platform", namespace="closed_transactions", symbol=None,
                            window=WINDOW, interval=None, kind=dep.KIND_STATE, optional=False,
                            reason="cooldown")

    with pytest.raises(warm_fetchers.WarmFetchError):
        _fetcher()(state)


def test_the_insider_lookback_comes_from_the_requirements_own_window():
    """A replay warm answers for ONE configuration, not for a GA search space."""
    seen = {}

    class _Table(warm_fetchers.NamespaceFetchers):
        def fetch(self, request):
            seen.update(namespace=request.namespace, lookback=request.lookback_days)

    fetcher = _fetcher(namespace_fetchers=_Table())
    fetcher(_history_req("insider_v2", window=dep.Window(start=NOW - timedelta(days=120),
                                                         end=NOW)))

    assert seen == {"namespace": "insider_v2", "lookback": 120}


def test_the_reference_date_is_read_at_each_fetch_not_at_construction():
    """A live warm fetcher outlives the day it was built.

    The host builds ONE of these at startup and the queue keeps it for the life of the
    process -- weeks. A ``datetime.now()`` frozen into it at construction was then the
    ``end_date`` of every statement, earnings, estimates and insider warm from then on,
    so from day two the warm asked for a window that ended in the past and the tail it
    was supposed to extend never arrived.
    """
    clock = [NOW]
    seen = []

    class _Table(warm_fetchers.NamespaceFetchers):
        def fetch(self, request):
            seen.append(request.end_date)

    fetcher = _fetcher(namespace_fetchers=_Table(), end_date_provider=lambda: clock[0])
    fetcher(_history_req("price_target"))
    clock[0] = NOW + timedelta(days=1)
    fetcher(_history_req("price_target"))

    assert seen == [NOW, NOW + timedelta(days=1)], (
        f"the reference date was frozen at construction: {seen}")


# --------------------------------------------------------------------------- #
# fmp_history round trip through the real cache writer
# --------------------------------------------------------------------------- #
def test_warming_an_empty_history_writes_the_sentinel_the_planner_reads_as_checked_empty(
        cache_root):
    """A symbol FMP genuinely has no data for must stop looking like a prewarm gap."""
    class _Table(warm_fetchers.NamespaceFetchers):
        def fetch(self, request):
            # Shaped exactly like FMPRating.fetch_price_target_history_cached.
            return fmp_common.fmp_history_disk_cached(
                request.namespace, request.symbol, lambda: [])

    from ba2_providers.warm import seams

    with seams.fmp_fetch_context():
        _fetcher(namespace_fetchers=_Table())(_history_req())

    entry = planner.plan([_history_req()], [str(cache_root)], as_of_now=NOW).entries[0]
    assert entry.status == planner.STATUS_CHECKED_EMPTY
    assert entry.action == planner.ACTION_NONE


def test_a_warmed_history_is_reported_present_and_a_second_plan_has_nothing_pending(
        cache_root):
    """The whole point: warm once, re-plan, zero pending."""
    class _Table(warm_fetchers.NamespaceFetchers):
        def fetch(self, request):
            return fmp_common.fmp_history_disk_cached(
                request.namespace, request.symbol, lambda: [{"t": 1}])

    from ba2_providers.warm import seams

    requirement = _history_req()
    before = planner.plan([requirement], [str(cache_root)], as_of_now=NOW)
    assert len(before.pending()) == 1

    with seams.fmp_fetch_context():
        _fetcher(namespace_fetchers=_Table())(requirement)

    after = planner.plan([requirement], [str(cache_root)], as_of_now=NOW)
    assert after.pending() == []
    assert after.entries[0].status == planner.STATUS_PRESENT
    assert json.loads(open(after.entries[0].path, encoding="utf-8").read()) == [{"t": 1}]


# --------------------------------------------------------------------------- #
# C1: the price series lands where the planner looks for it
# --------------------------------------------------------------------------- #
def _fake_bars(monkeypatch, symbol, dates):
    """Make every OHLCV provider's raw fetch return these bars, without a network."""
    import pandas as pd

    frame = pd.DataFrame({
        "Date": pd.to_datetime(dates, utc=True),
        "Open": [1.0] * len(dates), "High": [1.0] * len(dates),
        "Low": [1.0] * len(dates), "Close": [1.0] * len(dates),
        "Volume": [100] * len(dates),
    })

    def _impl(self, symbol, start_date, end_date, interval="1d"):
        return frame.copy()

    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    from ba2_providers.ohlcv.YFinanceDataProvider import YFinanceDataProvider

    monkeypatch.setattr(FMPOHLCVProvider, "_get_ohlcv_data_impl", _impl, raising=True)
    monkeypatch.setattr(YFinanceDataProvider, "_get_ohlcv_data_impl", _impl, raising=True)


def _window_sessions(window):
    from ba2_common.core import market_calendar

    return [close.astimezone(timezone.utc).date().isoformat()
            for _open, close in market_calendar.nyse_regular_sessions(
                window.start.date(), window.end.date())]


def test_a_price_requirement_is_fetched_through_the_provider_it_names(cache_root,
                                                                     monkeypatch):
    """The file the fetcher writes must be the file the planner then reports present.

    The host's indicator stack reads yfinance; a price requirement says ``fmp``. Writing
    the former's directory for the latter's requirement leaves it ``missing`` on every
    re-plan -- an unbounded re-download nobody can see in a single run.
    """
    window = dep.Window(start=NOW - timedelta(days=10), end=NOW)
    _fake_bars(monkeypatch, "AAPL", _window_sessions(window))
    requirement = _timeseries_req(window=window)

    assert planner.plan([requirement], [str(cache_root)],
                        as_of_now=NOW).entries[0].status == planner.STATUS_MISSING

    _fetcher(indicator_provider="yfinance")(requirement)

    entry = planner.plan([requirement], [str(cache_root)], as_of_now=NOW).entries[0]
    assert entry.status == planner.STATUS_PRESENT, entry.detail
    assert "FMPOHLCVProvider" in entry.path


def test_an_indicator_requirement_is_fetched_through_the_hosts_indicator_stack(cache_root,
                                                                               monkeypatch):
    """An ATR has no artifact of its own: warming its underlying series is the work."""
    window = dep.Window(start=NOW - timedelta(days=10), end=NOW)
    _fake_bars(monkeypatch, "AAPL", _window_sessions(window))
    requirement = _indicator_req(window=window)

    _fetcher(indicator_provider="yfinance")(requirement)

    entry = planner.plan([requirement], [str(cache_root)], as_of_now=NOW).entries[0]
    assert entry.status == planner.STATUS_PRESENT, entry.detail
    assert "YFinanceDataProvider" in entry.path


def test_the_cli_default_wiring_round_trips_too(cache_root, monkeypatch):
    """``replay warm``'s defaults must satisfy the plan they were built from."""
    window = dep.Window(start=NOW - timedelta(days=10), end=NOW)
    _fake_bars(monkeypatch, "AAPL", _window_sessions(window))
    requirements = [_timeseries_req(window=window), _indicator_req(window=window)]

    fetcher = _fetcher(indicator_provider="yfinance")
    for requirement in requirements:
        fetcher(requirement)

    after = planner.plan(requirements, [str(cache_root)], as_of_now=NOW)
    assert after.pending() == [], [e.detail for e in after.entries]


def test_a_price_requirement_without_a_bounded_window_is_refused():
    unbounded = _timeseries_req(window=dep.Window(start=None, end=NOW))

    with pytest.raises(warm_fetchers.WarmFetchError):
        _fetcher()(unbounded)


# --------------------------------------------------------------------------- #
# FRED
# --------------------------------------------------------------------------- #
def test_a_macro_series_is_refused_without_the_fred_key():
    series = dep.Requirement(provider="fred", namespace="VIXCLS", symbol=None, window=WINDOW,
                             interval=None, kind=dep.KIND_SERIES, optional=False, reason="t")

    with pytest.raises(warm_fetchers.WarmFetchError):
        _fetcher(fred_key=None)(series)
