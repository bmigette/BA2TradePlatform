"""READ-side staleness guard: a reader asked for a day well past the store's newest scan raises
``MetricStoreStaleError`` instead of silently reusing that scan. One rule (``check_scan_freshness``)
is shared by every reader; inside the store's coverage nothing changes (golden test)."""
import numpy as np
import pandas as pd
import pytest

from ba2_providers.screener import metric_store as ms

SCANS = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-01-03", "2026-06-27", freq="7D")]
NEWEST = SCANS[-1]                                           # 2026-06-27


def _store(categorical: bool) -> "pd.DataFrame":
    rng = np.random.default_rng(1)
    rows = []
    for d in SCANS:
        for i, s in enumerate(("AAA", "BBB", "CCC", "DDD")):
            rows.append({"date": d, "symbol": s, "market_cap": 2e9 + i * 1e9, "price": 20.0 + i,
                         "close": 20.0 + i, "volume": 1e6, "relative_volume": float(rng.uniform(0.5, 3)),
                         "price_drop_pct": float(rng.uniform(0, 12)), "weinstein_stage": 2.0,
                         "momentum_12_1": float(rng.normal()), "sector": "T"})
    df = pd.DataFrame(rows)
    if categorical:
        df["date"] = pd.Categorical(df["date"], categories=sorted(set(df["date"])), ordered=True)
    return df


@pytest.fixture(params=[False, True], ids=["str-dates", "categorical-dates"])
def df(request):
    return _store(request.param)


def _plus(days: int) -> str:
    return (pd.Timestamp(NEWEST) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def _old_screen(df, day, settings):                       # the pre-change reader, verbatim
    d = ms._latest_scan_date_le(df, day)
    return [] if d is None else ms.screen_universe_for_day(df, d, settings)


def test_inside_coverage_and_grace_window_ok(df):
    assert ms.screen_universe_as_of(df, "2026-03-10", {}) != []
    assert ms.screen_universe_as_of(df, NEWEST, {}) != []
    assert ms.screen_universe_as_of(df, _plus(3), {}) != []          # 3 days after the last scan
    assert ms.screen_universe_as_of(df, _plus(14), {}) != []         # exactly the limit


def test_past_the_limit_raises_for_every_reader(df):
    day = _plus(15)
    with pytest.raises(ms.MetricStoreStaleError):
        ms.screen_universe_as_of(df, day, {})
    with pytest.raises(ms.MetricStoreStaleError):
        ms.metrics_as_of(df, day, ["close"])
    with pytest.raises(ms.MetricStoreStaleError):
        ms.screened_symbol_union(df, "2026-05-01", day, {})
    with pytest.raises(ms.MetricStoreStaleError):
        ms.resolve_scan_date(df, day)


def test_before_first_scan_unchanged(df):
    assert ms.screen_universe_as_of(df, "2025-12-01", {}) == []
    assert ms.metrics_as_of(df, "2025-12-01", ["close"]) == {}
    assert ms.resolve_scan_date(df, "2025-12-01") is None


def test_error_message_content(df):
    with pytest.raises(ms.MetricStoreStaleError) as ei:
        ms.screen_universe_as_of(df, "2026-09-26", {})
    m = str(ei.value)
    assert "2026-09-26" in m and NEWEST in m and "build-screener-metrics --end" in m
    assert issubclass(ms.MetricStoreStaleError, ms.MetricStoreQualityError)


def test_golden_identical_inside_coverage(df):
    """Every day from before the first scan to the end of the grace window gives EXACTLY the
    pre-change answer (the 2020-2025 backtests must be byte-identical)."""
    settings = {"market_cap_min": 3e9, "relative_volume_min": 1.0, "max_stocks": 3}
    for day in pd.date_range("2025-12-20", _plus(14)).strftime("%Y-%m-%d"):
        assert ms.screen_universe_as_of(df, day, settings) == _old_screen(df, day, settings), day
        d = ms._latest_scan_date_le(df, day)
        old_m = {} if d is None else df[df["date"] == d].set_index("symbol")[["close"]].to_dict("index")
        assert ms.metrics_as_of(df, day, ["close"]) == old_m, day
    assert ms.screened_symbol_union(df, "2026-02-01", NEWEST, settings) == \
        ms.screened_symbol_union(df, "2026-02-01", _plus(5), settings)


def test_coarser_store_cadence_widens_the_limit():
    dates = ["2026-01-01", "2026-02-01", "2026-03-01"]                # monthly store
    ms.check_scan_freshness("2026-04-20", "2026-03-01", dates)         # 50 days <= 2 x ~30
    with pytest.raises(ms.MetricStoreStaleError):
        ms.check_scan_freshness("2026-06-01", "2026-03-01", dates)


@pytest.mark.parametrize("intraday", [False, True])
def test_daily_engine_reader_shares_the_rule(tmp_path, intraday):
    from datetime import datetime
    from app.services.backtest.daily_engine import _screened_symbols_for_bar
    store = str(tmp_path / "s")
    ms.write_partitions(store, _store(False))
    rt = {"store": store, "settings": {}}
    assert _screened_symbols_for_bar(rt, datetime(2026, 6, 30, 10, 0), None, intraday=intraday) != []
    # Monday 09:35 right after the Saturday scan (2026-06-27): visible, never stale
    assert _screened_symbols_for_bar(rt, datetime(2026, 6, 29, 9, 35), None, intraday=intraday) != []
    with pytest.raises(ms.MetricStoreStaleError):
        _screened_symbols_for_bar(rt, datetime(2026, 9, 26, 10, 0), None, intraday=intraday)
    assert _screened_symbols_for_bar(rt, datetime(2025, 12, 1), None, intraday=intraday) == []
    assert _screened_symbols_for_bar(None, datetime(2026, 9, 26), None, intraday=intraday) is None


# ---- the freshness rule composed with the dev visibility selector (Saturday scans, 5-minute clock) ----
def test_resolve_scan_date_is_the_selector_plus_freshness(df):
    from datetime import date
    dates = ms._present_scan_dates(df)
    for off in (-30, 0, 1, 5, 14):                        # inside coverage / grace: == the selector
        day = _plus(off)
        assert ms.resolve_scan_date(df, day) == ms.visible_scan_date(
            dates, date.fromisoformat(day), intraday=False) == ms._latest_scan_date_le(df, day)


def test_a_pre_open_monday_after_a_saturday_scan_never_trips():
    # Saturday scan; the cutoff of Monday 09:35 is the Sunday (Friday's session is the last finished).
    from ba2_common.core.knowability import scan_cutoff_date
    from datetime import datetime
    df = _store(True)
    sat = NEWEST                                           # 2026-06-27 is a Saturday
    assert pd.Timestamp(sat).dayofweek == 5
    for wk in range(0, 2):                                 # the Monday after, and the one a week later
        mon = datetime.fromisoformat(_plus(2 + 7 * wk) + "T09:35:00")
        cutoff = scan_cutoff_date(mon).strftime("%Y-%m-%d")
        assert ms.resolve_scan_date(df, cutoff) == sat      # age <= 9 d: well inside the 14 d limit
