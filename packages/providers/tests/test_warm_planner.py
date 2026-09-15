"""The warm planner: what is already on disk, what is stale, what is missing (spec step 4).

Lifecycle step 1 is "plan without network": inspect the production and shared roots
READ-ONLY and emit the exact missing/stale windows with estimated bytes. Two
properties make or break it.

*Zero network, by construction.* Every test runs with ``socket.socket.connect``
patched to raise -- a planner that reached out for a single "quick check" would be
a plan that costs bandwidth before anyone has approved spending any.

*The four on-disk states are told apart.* ``present`` / ``[]`` sentinel
(``checked_empty``) / ``stale`` / ``missing`` are four different answers, and
collapsing any pair of them either re-downloads a complete history every run or
reports a gap as covered.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import dependencies as dep
from ba2_providers.warm import planner

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
WINDOW = dep.Window(start=NOW - timedelta(days=365), end=NOW)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any socket connect during planning is a test failure, loudly."""
    import socket

    def _refuse(self, address, *args, **kwargs):
        raise AssertionError(f"the planner opened a socket to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _refuse, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse, raising=True)


def _history_file(root, namespace, symbol, payload="[{\"x\": 1}]", age_days=0.0):
    d = root / "fmp_history"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{namespace}__{symbol.upper()}.json"
    path.write_text(payload, encoding="utf-8")
    if age_days:
        import os
        old = (NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (old, old))
    else:
        import os
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    return path


def _history_req(namespace, symbol, optional=False):
    return dep.Requirement(provider="fmp", namespace=namespace, symbol=symbol, window=WINDOW,
                           interval=None, kind=dep.KIND_HISTORY, optional=optional, reason="t")


def _series_req(series_id):
    return dep.Requirement(provider="fred", namespace=series_id, symbol=None, window=WINDOW,
                           interval=None, kind=dep.KIND_SERIES, optional=False, reason="t")


def _timeseries_req(symbol, provider="fmp", interval="1d", window=None):
    return dep.Requirement(provider=provider, namespace="ohlcv", symbol=symbol,
                           window=window or WINDOW, interval=interval,
                           kind=dep.KIND_TIMESERIES, optional=False, reason="t")


def _write_parquet(root, provider_dir, symbol, interval, dates):
    import pandas as pd

    d = root / provider_dir
    d.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({
        "Date": pd.to_datetime(dates, utc=True),
        "Close": [1.0] * len(dates),
        "effective_date": pd.to_datetime(dates, utc=True),
    })
    frame.to_parquet(d / f"{symbol.upper()}_{interval}.parquet")


# --------------------------------------------------------------------------- #
# fmp_history: the four states
# --------------------------------------------------------------------------- #
def test_a_fresh_file_is_present_and_needs_no_action(tmp_path):
    _history_file(tmp_path, "price_target", "AAPL")

    result = planner.plan([_history_req("price_target", "AAPL")], [str(tmp_path)], as_of_now=NOW)

    entry = result.entries[0]
    assert entry.status == planner.STATUS_PRESENT
    assert entry.action == planner.ACTION_NONE
    assert entry.source_root == str(tmp_path)
    assert entry.size_bytes > 0


def test_an_empty_list_sentinel_is_checked_empty_not_missing(tmp_path):
    """"FMP has nothing for this symbol" is an ANSWER; re-fetching it every run is not."""
    _history_file(tmp_path, "insider_v2", "BNH", payload="[]")

    result = planner.plan([_history_req("insider_v2", "BNH")], [str(tmp_path)], as_of_now=NOW)

    assert result.entries[0].status == planner.STATUS_CHECKED_EMPTY
    assert result.entries[0].action == planner.ACTION_NONE


def test_a_file_older_than_the_history_max_age_is_stale_and_refreshed(tmp_path):
    from ba2_providers.fmp_common import _FMP_HISTORY_DISK_MAX_AGE_DAYS

    _history_file(tmp_path, "price_target", "AAPL",
                  age_days=_FMP_HISTORY_DISK_MAX_AGE_DAYS + 1)

    result = planner.plan([_history_req("price_target", "AAPL")], [str(tmp_path)], as_of_now=NOW)

    assert result.entries[0].status == planner.STATUS_STALE
    assert result.entries[0].action == planner.ACTION_REFRESH
    assert "age" in result.entries[0].detail


def test_staleness_is_measured_against_the_passed_clock_not_the_wall_clock(tmp_path):
    from ba2_providers.fmp_common import _FMP_HISTORY_DISK_MAX_AGE_DAYS

    _history_file(tmp_path, "price_target", "AAPL",
                  age_days=_FMP_HISTORY_DISK_MAX_AGE_DAYS + 1)
    earlier = NOW - timedelta(days=_FMP_HISTORY_DISK_MAX_AGE_DAYS + 1)

    result = planner.plan([_history_req("price_target", "AAPL")], [str(tmp_path)],
                          as_of_now=earlier)

    assert result.entries[0].status == planner.STATUS_PRESENT


def test_an_absent_file_is_missing_and_fetched(tmp_path):
    result = planner.plan([_history_req("grades_historical", "MSFT")], [str(tmp_path)],
                          as_of_now=NOW)

    entry = result.entries[0]
    assert entry.status == planner.STATUS_MISSING
    assert entry.action == planner.ACTION_FETCH
    assert entry.source_root is None


def test_a_shared_root_satisfies_a_requirement_the_production_root_lacks(tmp_path):
    production = tmp_path / "prod"
    shared = tmp_path / "shared"
    production.mkdir()
    _history_file(shared, "price_target", "AAPL")

    result = planner.plan([_history_req("price_target", "AAPL")],
                          [str(production), str(shared)], as_of_now=NOW)

    assert result.entries[0].status == planner.STATUS_PRESENT
    assert result.entries[0].source_root == str(shared)


# --------------------------------------------------------------------------- #
# The report as a whole
# --------------------------------------------------------------------------- #
def test_the_plan_reports_exactly_the_gaps(tmp_path):
    _history_file(tmp_path, "price_target", "AAPL")
    _history_file(tmp_path, "insider_v2", "BNH", payload="[]")

    result = planner.plan([
        _history_req("price_target", "AAPL"),
        _history_req("insider_v2", "BNH"),
        _history_req("grades_historical", "MSFT"),
    ], [str(tmp_path)], as_of_now=NOW)

    assert [e.requirement.key for e in result.pending()] == [
        _history_req("grades_historical", "MSFT").key]
    assert result.totals()["missing"] == 1
    assert result.totals()["present"] == 1
    assert result.totals()["checked_empty"] == 1


def test_state_and_unsupported_requirements_are_reported_not_fetched(tmp_path):
    state = dep.Requirement(provider="platform", namespace="closed_transactions", symbol=None,
                            window=WINDOW, interval=None, kind=dep.KIND_STATE, optional=False,
                            reason="cooldown")
    unsupported = dep.unsupported_requirement("FactorRanker", "no adapter")

    result = planner.plan([state, unsupported], [str(tmp_path)], as_of_now=NOW)

    assert [e.status for e in result.entries] == [planner.STATUS_LOCAL_STATE,
                                                  planner.STATUS_UNSUPPORTED]
    assert {e.action for e in result.entries} == {planner.ACTION_REPORT}
    assert result.pending() == [], "nothing here is warmable; the plan must not queue it"


def test_estimated_bytes_come_from_measured_comparable_files(tmp_path):
    _history_file(tmp_path, "price_target", "AAPL", payload="x" * 500)
    _history_file(tmp_path, "price_target", "MSFT", payload="x" * 700)

    result = planner.plan([_history_req("price_target", "NVDA")], [str(tmp_path)], as_of_now=NOW)

    entry = result.entries[0]
    assert entry.estimated_bytes == 600, "the median of the namespace's measured files"
    assert result.estimated_bytes_total() == 600


def test_a_namespace_with_nothing_to_measure_reports_an_unknown_estimate(tmp_path):
    result = planner.plan([_history_req("price_target", "NVDA")], [str(tmp_path)], as_of_now=NOW)

    assert result.entries[0].estimated_bytes is None, (
        "no comparable file measured -- inventing a number would make the budget fiction")
    assert result.totals()["unknown_estimate"] == 1


# --------------------------------------------------------------------------- #
# FRED series
# --------------------------------------------------------------------------- #
def test_a_fred_series_file_is_present_until_its_own_max_age(tmp_path):
    import os

    d = tmp_path / "fred"
    d.mkdir()
    path = d / "VIXCLS.json"
    path.write_text('{"observations": []}', encoding="utf-8")
    old = (NOW - timedelta(hours=30)).timestamp()
    os.utime(path, (old, old))

    fresh = planner.plan([_series_req("VIXCLS")], [str(tmp_path)], as_of_now=NOW,
                         fred_max_age_hours=48.0)
    stale = planner.plan([_series_req("VIXCLS")], [str(tmp_path)], as_of_now=NOW,
                         fred_max_age_hours=24.0)

    assert fresh.entries[0].status == planner.STATUS_PRESENT
    assert stale.entries[0].status == planner.STATUS_STALE


def test_a_missing_fred_series_is_missing(tmp_path):
    result = planner.plan([_series_req("UNRATE")], [str(tmp_path)], as_of_now=NOW)

    assert result.entries[0].status == planner.STATUS_MISSING


# --------------------------------------------------------------------------- #
# Parquet coverage: prefix, holes and tail
# --------------------------------------------------------------------------- #
#: A short window, so a "complete" series is a handful of bars rather than a year of
#: them. Ends on a Friday afternoon; ``SHORT_WINDOW.end`` is inside the session.
SHORT_WINDOW = dep.Window(start=datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc), end=NOW)


def _sessions(window):
    """The exchange sessions the window contains, as UTC dates."""
    from ba2_common.core import market_calendar

    return [close.astimezone(timezone.utc).date()
            for _open, close in market_calendar.nyse_regular_sessions(
                window.start.date(), window.end.date())]


def _complete_dates(window):
    return [d.isoformat() for d in _sessions(window)]


def test_a_series_covering_every_session_in_the_window_is_present(tmp_path):
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d", _complete_dates(SHORT_WINDOW))

    result = planner.plan([_timeseries_req("AAPL", window=SHORT_WINDOW)], [str(tmp_path)],
                          as_of_now=NOW)

    entry = result.entries[0]
    assert entry.status == planner.STATUS_PRESENT, entry.detail
    assert entry.rows == len(_complete_dates(SHORT_WINDOW))


def test_a_series_that_starts_after_the_window_does_is_stale_on_its_missing_prefix(tmp_path):
    """A 600-day lookback served by a 3-day file is not coverage."""
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d",
                   _complete_dates(SHORT_WINDOW)[-3:])

    result = planner.plan([_timeseries_req("AAPL", window=SHORT_WINDOW)], [str(tmp_path)],
                          as_of_now=NOW)

    entry = result.entries[0]
    assert entry.status == planner.STATUS_STALE
    assert entry.action == planner.ACTION_REFRESH
    assert "prefix missing" in entry.detail


def test_a_series_with_holes_inside_the_window_is_stale(tmp_path):
    """Rows are counted against the exchange calendar, not against max(date)."""
    dates = _complete_dates(SHORT_WINDOW)
    holed = [dates[0]] + dates[len(dates) // 2:]        # the first half is missing

    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d", holed)

    entry = planner.plan([_timeseries_req("AAPL", window=SHORT_WINDOW)], [str(tmp_path)],
                         as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_STALE
    assert "holes" in entry.detail


def test_one_missing_session_is_tolerated_rather_than_treated_as_a_hole(tmp_path):
    """Halts and sparse trading are real; the spec forbids manufacturing candles."""
    dates = _complete_dates(SHORT_WINDOW)
    with_gap = dates[:1] + dates[2:]

    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d", with_gap)

    entry = planner.plan([_timeseries_req("AAPL", window=SHORT_WINDOW)], [str(tmp_path)],
                         as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_PRESENT, entry.detail


def test_a_daily_tail_is_measured_against_the_last_COMPLETED_session(tmp_path):
    """Mid-session, today's daily bar does not exist yet -- and that is not staleness.

    Comparing against the wall-clock DATE marked every daily requirement stale on
    every batch, which re-downloaded the full history each time.
    """
    dates = _complete_dates(SHORT_WINDOW)
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d", dates)
    midsession = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)   # 11:00 New York
    window = dep.Window(start=SHORT_WINDOW.start, end=midsession)

    entry = planner.plan([_timeseries_req("AAPL", window=window)], [str(tmp_path)],
                         as_of_now=midsession).entries[0]

    assert entry.status == planner.STATUS_PRESENT, entry.detail


def test_a_series_whose_tail_stops_days_short_is_stale(tmp_path):
    dates = _complete_dates(SHORT_WINDOW)
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d", dates[:-3])

    entry = planner.plan([_timeseries_req("AAPL", window=SHORT_WINDOW)], [str(tmp_path)],
                         as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_STALE
    assert "tail stops" in entry.detail


def test_a_legacy_interval_spelling_on_disk_still_resolves(tmp_path):
    """``5m`` must find the ``_5min`` file the FMP writer produced, or every intraday
    cache in the platform reads as missing."""
    window = dep.Window(start=NOW, end=NOW)
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "5min", [NOW.isoformat()])

    entry = planner.plan([_timeseries_req("AAPL", interval="5m", window=window)],
                         [str(tmp_path)], as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_PRESENT, entry.detail
    assert entry.path.endswith("AAPL_5min.parquet")


def test_an_intraday_series_is_compared_at_timestamp_granularity_not_by_day(tmp_path):
    """A daily bar covers the day it is stamped with; a 5-minute bar covers only itself."""
    window = dep.Window(start=NOW - timedelta(hours=8), end=NOW)
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "5min",
                   ["2026-09-11T13:30:00+00:00"])

    entry = planner.plan([_timeseries_req("AAPL", interval="5m", window=window)],
                         [str(tmp_path)], as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_STALE


def test_an_empty_parquet_is_missing_not_present(tmp_path):
    _write_parquet(tmp_path, "FMPOHLCVProvider", "AAPL", "1d", [])

    result = planner.plan([_timeseries_req("AAPL")], [str(tmp_path)], as_of_now=NOW)

    assert result.entries[0].status == planner.STATUS_MISSING


def test_an_unknown_ohlcv_provider_name_is_refused_rather_than_guessed(tmp_path):
    with pytest.raises(planner.WarmPlanError):
        planner.plan([_timeseries_req("AAPL", provider="not-a-provider")], [str(tmp_path)],
                     as_of_now=NOW)


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def _indicator_req(window=None):
    return dep.Requirement(provider="indicators", namespace="atr_14", symbol="AAPL",
                           window=window or SHORT_WINDOW, interval="1d",
                           kind=dep.KIND_INDICATOR, optional=False, reason="t")


def test_an_indicator_resolves_against_whatever_provider_directory_holds_the_series(tmp_path):
    """Which OHLCV source backs the indicator provider is HOST wiring, so search them all."""
    _write_parquet(tmp_path, "YFinanceDataProvider", "AAPL", "1d",
                   _complete_dates(SHORT_WINDOW))

    entry = planner.plan([_indicator_req()], [str(tmp_path)], as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_PRESENT, entry.detail
    assert "YFinanceDataProvider" in entry.path


def test_an_indicator_with_no_series_anywhere_is_missing_and_names_where_it_looked(tmp_path):
    entry = planner.plan([_indicator_req()], [str(tmp_path)], as_of_now=NOW).entries[0]

    assert entry.status == planner.STATUS_MISSING
    assert "AAPL_1d.parquet" in entry.detail


# --------------------------------------------------------------------------- #
# Round trip and read-only-ness
# --------------------------------------------------------------------------- #
def test_the_plan_round_trips_through_json(tmp_path):
    _history_file(tmp_path, "price_target", "AAPL")
    result = planner.plan([_history_req("price_target", "AAPL"),
                           _history_req("grades_historical", "MSFT")],
                          [str(tmp_path)], as_of_now=NOW)

    restored = planner.WarmPlan.from_json(result.to_json())

    assert restored.to_json() == result.to_json()
    assert [e.requirement.key for e in restored.pending()] == [
        e.requirement.key for e in result.pending()]


def test_planning_writes_nothing_into_the_roots_it_inspects(tmp_path):
    _history_file(tmp_path, "price_target", "AAPL")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    planner.plan([_history_req("price_target", "AAPL"), _history_req("x", "Y"),
                  _series_req("VIXCLS"), _timeseries_req("AAPL")],
                 [str(tmp_path)], as_of_now=NOW)

    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert after == before, "the planner is READ-ONLY; it may not even create a directory"


def test_a_root_that_does_not_exist_is_not_created(tmp_path):
    absent = tmp_path / "nope"

    result = planner.plan([_history_req("price_target", "AAPL")], [str(absent)], as_of_now=NOW)

    assert not absent.exists()
    assert result.entries[0].status == planner.STATUS_MISSING


# --------------------------------------------------------------------------- #
# Sizing a root with no requirements in hand
# --------------------------------------------------------------------------- #
def test_root_sizes_are_measurable_without_any_requirements(tmp_path):
    """``measured_sizes()`` reads a plan's ENTRIES, so a plan over no requirements
    measures nothing whatever the root holds -- which is how the live host came to
    report an empty root at every startup. This reads the root itself."""
    _history_file(tmp_path, "price_target", "AAPL", payload="x" * 400)
    _history_file(tmp_path, "grades_historical", "AAPL", payload="y" * 900)

    empty_plan = planner.plan([], [str(tmp_path)], as_of_now=NOW)

    assert empty_plan.measured_sizes() == []
    assert sorted(planner.measured_root_sizes([str(tmp_path)])) == [400, 900]


def test_an_empty_root_measures_nothing_rather_than_inventing_a_size(tmp_path):
    assert planner.measured_root_sizes([str(tmp_path)]) == []


def test_measuring_a_root_writes_nothing_into_it(tmp_path):
    _history_file(tmp_path, "price_target", "AAPL")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    planner.measured_root_sizes([str(tmp_path)])

    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
