"""Tests for ``ba2_providers.symbol_info`` — the symbol-info panel data layer.

Every FMP access is monkeypatched (no key, no network), following the repo
convention of patching the callee attribute on the module under test
(see ``test_symbol_snapshot.py``).

Time is always frozen to an explicit ``as_of``, and never to "today": a YTD
calculation frozen to the real today passes for the wrong reason.
"""
from datetime import date

import pytest

import ba2_providers.symbol_info as SI


# The frozen clock for every test in this file. Deliberately NOT today.
AS_OF = date(2025, 8, 22)


# ---------------------------------------------------------------------------
# The total-return trap: adjClose ALREADY includes reinvested dividends.
# ---------------------------------------------------------------------------
def _mixing_trap_points():
    """A window where the four candidate answers are all distinct numbers.

    close   100.0 -> 110.0            (price-only return  = +10.0%)
    adjClose 100.0 -> 126.5           (TOTAL return       = +26.5%)
    paid dividends in window          = 5.00 per share

    So a caller that mixes the two series lands on a visibly different number:
      close + dividends      -> +15.0%   (dividends counted, price-only base)
      adjClose + dividends   -> +31.5%   (dividends DOUBLE counted)
      close alone            -> +10.0%   (dividends OMITTED)
    """
    return [
        SI.PricePoint(date(2020, 8, 21), close=100.0, adj_close=100.0),
        SI.PricePoint(date(2023, 1, 3), close=105.0, adj_close=115.0),
        SI.PricePoint(date(2025, 8, 22), close=110.0, adj_close=126.5),
    ]


def _mixing_trap_dividends():
    return [
        SI.DividendEvent(date(2021, 5, 7), dividend=2.5, adj_dividend=2.5),
        SI.DividendEvent(date(2024, 5, 7), dividend=2.5, adj_dividend=2.5),
    ]


def test_total_return_uses_adjclose_only_never_close_plus_dividends():
    r = SI.compute_window_return(
        "5y", _mixing_trap_points(), _mixing_trap_dividends(), splits=[], as_of=AS_OF)

    assert r.total_return_pct == pytest.approx(26.5)

    # The three wrong answers, spelled out so a future mixer trips over them.
    assert r.total_return_pct != pytest.approx(15.0), "close + dividends"
    assert r.total_return_pct != pytest.approx(31.5), "adjClose + dividends (double count)"
    assert r.total_return_pct != pytest.approx(10.0), "close alone (dividends omitted)"


def test_price_return_is_close_only_and_excludes_dividends():
    """The price-only return is a SEPARATE field, computed from ``close`` alone."""
    r = SI.compute_window_return(
        "5y", _mixing_trap_points(), _mixing_trap_dividends(), splits=[], as_of=AS_OF)
    assert r.price_return_pct == pytest.approx(10.0)
    # ...and it must not have absorbed the dividends either.
    assert r.price_return_pct != pytest.approx(15.0)


def test_dividends_in_window_are_PAID_not_reinvested():
    """The dividend figure is cash PAID per share, never the reinvested total return."""
    r = SI.compute_window_return(
        "5y", _mixing_trap_points(), _mixing_trap_dividends(), splits=[], as_of=AS_OF)
    assert r.dividends_paid_per_share == pytest.approx(5.0)
    # 5.0 of cash on a 100.0 base is 5%, which is NOT the 26.5% total return.
    assert r.dividends_paid_per_share != pytest.approx(r.total_return_pct)


# ---------------------------------------------------------------------------
# Splits: an unadjusted close is not comparable across one.
# ---------------------------------------------------------------------------
def test_price_return_is_unknown_when_a_split_falls_inside_the_window():
    points = _mixing_trap_points()
    splits = [SI.SplitEvent(date(2022, 6, 6), numerator=4.0, denominator=1.0, ratio=4.0)]

    r = SI.compute_window_return("5y", points, _mixing_trap_dividends(),
                                 splits=splits, as_of=AS_OF)

    assert r.price_return_pct is None
    assert "2022-06-06" in r.why("price_return_pct")
    # The TOTAL return still stands: adjClose is split-adjusted by construction.
    assert r.total_return_pct == pytest.approx(26.5)


def test_price_return_is_unknown_when_split_history_could_not_be_fetched():
    """``splits=None`` means UNKNOWN, and is not the same as ``splits=[]`` (no splits)."""
    points = _mixing_trap_points()
    unknown = SI.compute_window_return("5y", points, [], splits=None, as_of=AS_OF)
    known_none = SI.compute_window_return("5y", points, [], splits=[], as_of=AS_OF)

    assert unknown.price_return_pct is None
    assert unknown.why("price_return_pct") != ""
    assert known_none.price_return_pct == pytest.approx(10.0)


def test_split_outside_the_window_does_not_suppress_price_return():
    points = _mixing_trap_points()
    splits = [SI.SplitEvent(date(2019, 1, 2), numerator=2.0, denominator=1.0, ratio=2.0)]
    r = SI.compute_window_return("5y", points, [], splits=splits, as_of=AS_OF)
    assert r.price_return_pct == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# A 3y/5y return for an 18-month-old symbol is NOT COMPUTABLE.
# ---------------------------------------------------------------------------
def _young_symbol_points():
    """~18 months of history: first bar 2024-02-20, last 2025-08-22."""
    return [
        SI.PricePoint(date(2024, 2, 20), close=20.0, adj_close=20.0),
        SI.PricePoint(date(2024, 8, 22), close=25.0, adj_close=25.0),
        SI.PricePoint(date(2025, 8, 22), close=30.0, adj_close=30.0),
    ]


@pytest.mark.parametrize("window", ["3y", "5y"])
def test_long_window_on_an_18_month_old_symbol_is_not_computable(window):
    r = SI.compute_window_return(window, _young_symbol_points(), [], splits=[], as_of=AS_OF)

    assert r.total_return_pct is None, "not zero, and not silently since-inception"
    assert r.price_return_pct is None
    assert r.start_date is None
    reason = r.why("total_return_pct")
    assert window in reason
    assert "2024-02-20" in reason, "the reason must name the earliest price we do have"


def test_one_year_window_on_an_18_month_old_symbol_IS_computable():
    r = SI.compute_window_return("1y", _young_symbol_points(), [], splits=[], as_of=AS_OF)
    assert r.start_date == date(2024, 8, 22)
    assert r.total_return_pct == pytest.approx(20.0)


def test_a_not_computable_window_is_not_the_same_as_a_flat_one():
    """The inverse guard: a genuinely flat window reports 0.0, not unknown."""
    flat = [
        SI.PricePoint(date(2024, 8, 21), close=50.0, adj_close=50.0),
        SI.PricePoint(date(2025, 8, 22), close=50.0, adj_close=50.0),
    ]
    r = SI.compute_window_return("1y", flat, [], splits=[], as_of=AS_OF)
    assert r.total_return_pct == 0.0
    assert r.why("total_return_pct") == ""


# ---------------------------------------------------------------------------
# Parsing FMP payloads. Shapes mirror the live responses (see module docstring
# for the two field names this module could not verify offline).
# ---------------------------------------------------------------------------
_PRICE_PAYLOAD = {
    "symbol": "XLK",
    "historical": [
        # FMP returns NEWEST FIRST.
        {"date": "2025-08-22", "open": 259.0, "high": 261.0, "low": 258.0,
         "close": 260.5, "adjClose": 260.5, "volume": 5_000_000,
         "unadjustedVolume": 5_000_000, "change": 1.5, "changePercent": 0.58,
         "vwap": 259.8, "label": "August 22, 25", "changeOverTime": 0.0058},
        {"date": "2025-08-21", "open": 257.0, "high": 259.5, "low": 256.0,
         "close": 259.0, "adjClose": 258.2, "volume": 4_800_000,
         "unadjustedVolume": 4_800_000, "change": 2.0, "changePercent": 0.78,
         "vwap": 258.0, "label": "August 21, 25", "changeOverTime": 0.0078},
    ],
}


def test_parse_price_history_sorts_ascending_and_keeps_both_series():
    pts = SI.parse_price_history(_PRICE_PAYLOAD)
    assert [p.date for p in pts] == [date(2025, 8, 21), date(2025, 8, 22)]
    assert pts[0].close == 259.0 and pts[0].adj_close == 258.2
    assert pts[1].close == 260.5 and pts[1].adj_close == 260.5


def test_parse_price_history_missing_adjclose_is_none_never_the_close():
    """Falling back to ``close`` would silently strip the dividends from the
    total return, which is the exact mixing error this module exists to avoid."""
    payload = {"symbol": "X", "historical": [
        {"date": "2025-08-22", "close": 100.0},                 # no adjClose key
        {"date": "2025-08-21", "close": 99.0, "adjClose": None},  # explicit null
    ]}
    pts = SI.parse_price_history(payload)
    assert pts[0].adj_close is None
    assert pts[1].adj_close is None
    assert pts[1].close == 100.0, "the close itself must survive"


def test_parse_price_history_zero_adjclose_is_preserved_not_nulled():
    """The inverse guard: a genuine 0 must not be laundered into unknown."""
    payload = {"symbol": "X", "historical": [{"date": "2025-08-22",
                                              "close": 0.0, "adjClose": 0.0}]}
    pts = SI.parse_price_history(payload)
    assert pts[0].adj_close == 0.0 and pts[0].adj_close is not None


def test_parse_price_history_empty_and_missing_key_give_empty_list():
    assert SI.parse_price_history({"symbol": "X", "historical": []}) == []
    assert SI.parse_price_history({"symbol": "X"}) == []
    assert SI.parse_price_history(None) == []


_DIVIDEND_PAYLOAD = {
    "symbol": "AAPL",
    "historical": [
        {"date": "2025-08-11", "label": "August 11, 25", "adjDividend": 0.26,
         "dividend": 0.26, "recordDate": "2025-08-11",
         "paymentDate": "2025-08-14", "declarationDate": "2025-07-31"},
        {"date": "2020-05-08", "label": "May 08, 20", "adjDividend": 0.2050,
         "dividend": 0.82, "recordDate": "2020-05-11",
         "paymentDate": "2020-05-14", "declarationDate": "2020-04-30"},
    ],
}


def test_parse_dividends_keeps_declared_and_split_adjusted_apart():
    divs = SI.parse_dividends(_DIVIDEND_PAYLOAD)
    assert [d.ex_date for d in divs] == [date(2020, 5, 8), date(2025, 8, 11)]
    old = divs[0]
    assert old.dividend == 0.82, "as declared, pre-split"
    assert old.adj_dividend == 0.2050, "split adjusted"
    assert old.chart_amount == 0.2050, "a multi-year bar chart needs the adjusted one"


def test_parse_splits_computes_the_ratio():
    payload = {"symbol": "AAPL", "historical": [
        {"date": "2020-08-31", "label": "August 31, 20",
         "numerator": 4.0, "denominator": 1.0},
    ]}
    splits = SI.parse_splits(payload)
    assert splits[0].date == date(2020, 8, 31)
    assert splits[0].ratio == pytest.approx(4.0)


def test_parse_splits_zero_denominator_leaves_ratio_unknown_not_zero():
    payload = {"symbol": "X", "historical": [
        {"date": "2020-08-31", "numerator": 4.0, "denominator": 0},
    ]}
    splits = SI.parse_splits(payload)
    assert splits[0].ratio is None
    assert splits[0].numerator == 4.0


# ---------------------------------------------------------------------------
# The chart bundle: price, PAID dividend bars, REINVESTED cumulative line.
# ---------------------------------------------------------------------------
def test_build_series_cumulative_line_is_reinvested_growth_from_adjclose():
    points = _mixing_trap_points()
    bundle = SI.build_series(points, _mixing_trap_dividends(), [],
                             start=date(2020, 8, 21), end=AS_OF)

    assert [p.cumulative_total_return_pct for p in bundle.points] == [
        pytest.approx(0.0), pytest.approx(15.0), pytest.approx(26.5)]
    # the dividend BAR series is the other quantity: cash paid, not growth
    assert [d.chart_amount for d in bundle.dividends] == [2.5, 2.5]


def test_build_series_cumulative_line_is_unknown_when_the_base_bar_has_no_adjclose():
    points = [SI.PricePoint(date(2025, 8, 21), close=100.0, adj_close=None),
              SI.PricePoint(date(2025, 8, 22), close=110.0, adj_close=110.0)]
    bundle = SI.build_series(points, [], [], start=date(2025, 8, 21), end=AS_OF)
    assert all(p.cumulative_total_return_pct is None for p in bundle.points)
    assert bundle.why("points") != ""


def test_build_series_clips_every_series_to_the_window():
    points = _mixing_trap_points()
    divs = _mixing_trap_dividends()
    splits = [SI.SplitEvent(date(2019, 1, 2), 2.0, 1.0, 2.0),
              SI.SplitEvent(date(2024, 1, 2), 2.0, 1.0, 2.0)]
    bundle = SI.build_series(points, divs, splits,
                             start=date(2023, 1, 1), end=AS_OF)
    assert [p.date for p in bundle.points] == [date(2023, 1, 3), date(2025, 8, 22)]
    assert [d.ex_date for d in bundle.dividends] == [date(2024, 5, 7)]
    assert [s.date for s in bundle.splits] == [date(2024, 1, 2)]


def test_build_series_empty_dividends_is_a_fact_not_an_unknown():
    """A symbol that pays nothing: empty series, and NO reason attached."""
    bundle = SI.build_series(_mixing_trap_points(), [], [],
                             start=date(2020, 8, 21), end=AS_OF)
    assert bundle.dividends == ()
    assert bundle.why("dividends") == ""
    assert bundle.is_unknown("dividends") is False


def test_build_series_unfetchable_dividends_is_an_unknown_not_an_empty_bar_chart():
    bundle = SI.build_series(_mixing_trap_points(), None, [],
                             start=date(2020, 8, 21), end=AS_OF)
    assert bundle.dividends == ()
    assert bundle.is_unknown("dividends") is True
    assert bundle.is_unknown("splits") is False


def test_build_series_unfetchable_splits_is_an_unknown():
    bundle = SI.build_series(_mixing_trap_points(), [], None,
                             start=date(2020, 8, 21), end=AS_OF)
    assert bundle.splits == ()
    assert bundle.is_unknown("splits") is True
    assert bundle.is_unknown("dividends") is False


# ---------------------------------------------------------------------------
# ETF profile + holdings. Numbers are the user's XLK reference.
# ---------------------------------------------------------------------------
_XLK_INFO = {
    "symbol": "XLK", "name": "Technology Select Sector SPDR Fund",
    "assetClass": "Equity", "category": "Technology",
    "assetsUnderManagement": 119_630_000_000.0, "peRatio": 39.32,
    "expenseRatio": 0.0908, "nav": 260.5, "navCurrency": "USD",
    "holdingsCount": 76, "inceptionDate": "1998-12-16", "etfCompany": "SPDR",
    "domicile": "US", "cusip": "81369Y803", "isin": "US81369Y8030",
    "website": "https://www.ssga.com/", "description": "...",
    "sectorsList": [{"industry": "Technology", "exposure": 100.0}],
}


def _holdings_payload(weights):
    return [
        {"asset": f"S{i}", "name": f"Name {i}", "isin": f"ISIN{i}",
         "cusip": f"CUSIP{i}", "sharesNumber": 1000 + i,
         "weightPercentage": w, "marketValue": 1_000_000 + i,
         "updated": "2025-08-22"}
        for i, w in enumerate(weights)
    ]


_XLK_HOLDINGS = _holdings_payload(
    # top 10 sum to exactly 61.25, matching FMP's reported top-10 for XLK
    [14.46, 12.26, 9.90, 5.10, 4.30, 3.90, 3.20, 2.90, 2.70, 2.53]
    + [0.5] * 66
)
_XLK_HOLDINGS[0].update(asset="NVDA", name="NVIDIA Corporation")
_XLK_HOLDINGS[1].update(asset="AAPL", name="Apple Inc.")
_XLK_HOLDINGS[2].update(asset="MSFT", name="Microsoft Corporation")


def test_parse_etf_profile_reads_the_headline_fields():
    p = SI.parse_etf_profile(_XLK_INFO, _XLK_HOLDINGS)
    assert p.holdings_count == 76
    assert p.top10_weight_pct == pytest.approx(61.25)
    assert p.asset_class == "Equity"
    assert p.category == "Technology"
    assert p.assets_under_management == pytest.approx(119_630_000_000.0)
    assert p.pe_ratio == pytest.approx(39.32)
    assert p.expense_ratio == pytest.approx(0.0908)


def test_parse_etf_profile_returns_holdings_sorted_by_weight_desc():
    p = SI.parse_etf_profile(_XLK_INFO, _XLK_HOLDINGS)
    assert len(p.holdings) == 76
    top3 = [(h.symbol, h.weight_pct) for h in p.holdings[:3]]
    assert top3 == [("NVDA", 14.46), ("AAPL", 12.26), ("MSFT", 9.90)]
    assert p.holdings[0].name == "NVIDIA Corporation"
    assert p.holdings[0].shares == 1000


def test_top10_of_a_fund_with_fewer_than_ten_holdings_is_the_sum_of_all():
    """A fact, not an unknown: 4 holdings summing to 100% IS the top-10 weight."""
    p = SI.parse_etf_profile({"holdingsCount": 4}, _holdings_payload([40.0, 30.0, 20.0, 10.0]))
    assert p.top10_weight_pct == pytest.approx(100.0)
    assert p.why("top10_weight_pct") == ""


def test_top10_is_unknown_when_a_top_ten_weight_is_missing():
    """Summing over a null weight would silently UNDERSTATE the concentration."""
    holdings = _holdings_payload([14.0, 12.0, None, 5.0])
    p = SI.parse_etf_profile({"holdingsCount": 4}, holdings)
    assert p.top10_weight_pct is None
    assert p.why("top10_weight_pct") != ""


def test_a_missing_weight_outside_the_top_ten_does_not_poison_the_top10():
    holdings = _holdings_payload([10.0] * 10 + [None])
    p = SI.parse_etf_profile({"holdingsCount": 11}, holdings)
    assert p.top10_weight_pct == pytest.approx(100.0)


def test_a_genuine_zero_weight_holding_is_not_treated_as_unknown():
    holdings = _holdings_payload([10.0, 0.0])
    p = SI.parse_etf_profile({"holdingsCount": 2}, holdings)
    assert p.top10_weight_pct == pytest.approx(10.0)
    assert p.holdings[-1].weight_pct == 0.0


def test_etf_fields_absent_from_the_payload_are_unknown_and_name_the_keys_tried():
    p = SI.parse_etf_profile({"symbol": "XYZ"}, [])
    for name in ("asset_class", "category", "pe_ratio",
                 "assets_under_management", "holdings_count"):
        assert getattr(p, name) is None, name
        assert p.why(name) != "", name
    assert "assetClass" in p.why("asset_class")
    assert "peRatio" in p.why("pe_ratio")


def test_holdings_count_is_never_silently_the_length_of_a_truncated_list():
    """FMP's holdingsCount is authoritative; len(holdings) may be truncated."""
    p = SI.parse_etf_profile({"holdingsCount": 76}, _holdings_payload([1.0] * 5))
    assert p.holdings_count == 76
    assert len(p.holdings) == 5


def test_unfetchable_holdings_is_unknown_not_an_empty_fund():
    p = SI.parse_etf_profile(_XLK_INFO, None)
    assert p.holdings == ()
    assert p.is_unknown("holdings") is True
    assert p.top10_weight_pct is None
    assert p.why("top10_weight_pct") != ""


def test_an_etf_that_really_reports_no_holdings_is_a_fact():
    p = SI.parse_etf_profile(_XLK_INFO, [])
    assert p.holdings == ()
    assert p.is_unknown("holdings") is False


def test_unfetchable_etf_info_marks_every_info_field_unknown():
    p = SI.parse_etf_profile(None, _XLK_HOLDINGS)
    assert p.assets_under_management is None
    assert p.why("assets_under_management") != ""
    # ...but the holdings we DID get still stand, and so does the derived top-10.
    assert len(p.holdings) == 76
    assert p.top10_weight_pct == pytest.approx(61.25)


# ---------------------------------------------------------------------------
# Cached fetchers. The 1-day expiry rides on fmp_common.fmp_live_cached, which
# keeps ONE CACHE PER DISTINCT TTL keyed by ttl_seconds — so the module must
# land in the 86400s cache and nowhere else.
# ---------------------------------------------------------------------------
from ba2_providers import fmp_common  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_fmp_live_cache():
    """Each test starts from an empty live-bulk cache and leaves one behind."""
    fmp_common._LIVE_BULK_CACHES.clear()
    yield
    fmp_common._LIVE_BULK_CACHES.clear()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _record_http(monkeypatch, payload_for=lambda url: {"symbol": "X", "historical": []}):
    seen = []

    def fake_get(url, params=None, **kw):
        seen.append((url, dict(params or {})))
        return _Resp(payload_for(url))

    monkeypatch.setattr(SI, "fmp_http_get", fake_get)
    return seen


def test_etf_info_is_not_available_through_fmpsdk():
    """Pins WHY fetch_etf_info goes over raw HTTP: the installed fmpsdk defines
    ``etf_info`` in ``fmpsdk/etf.py`` but never re-exports it from the package,
    so ``fmpsdk.etf_info`` does not exist. If a future fmpsdk exports it, this
    test fails and the HTTP call can be swapped for the sdk one."""
    assert not hasattr(SI.fmpsdk, "etf_info")
    assert hasattr(SI.fmpsdk, "etf_holders"), "the holder endpoint IS exported"


def test_fetch_etf_info_uses_the_v4_etf_info_endpoint(monkeypatch):
    seen = _record_http(monkeypatch, payload_for=lambda url: [_XLK_INFO])
    SI.fetch_etf_info("key", "XLK")
    url, params = seen[0]
    assert url == "https://financialmodelingprep.com/api/v4/etf-info"
    assert params == {"apikey": "key", "symbol": "XLK"}


def test_fetch_etf_info_hits_fmp_once_then_serves_the_one_day_cache(monkeypatch):
    seen = _record_http(monkeypatch, payload_for=lambda url: [_XLK_INFO])

    first = SI.fetch_etf_info("key", "XLK")
    second = SI.fetch_etf_info("key", "XLK")

    assert first == second == _XLK_INFO
    assert len(seen) == 1, "the second call must be served from cache"
    assert set(fmp_common._LIVE_BULK_CACHES) == {86400.0}, (
        "the panel's fetches must land in the 1-DAY cache; fmp_live_cached keys its "
        "caches by ttl_seconds, so a different TTL silently makes a different cache")


def test_cache_entry_expires_after_one_day_and_not_a_second_before(monkeypatch):
    t = [1000.0]
    fmp_common._LIVE_BULK_CACHES[SI.CACHE_TTL_SECONDS] = fmp_common.TTLCache(
        SI.CACHE_TTL_SECONDS, clock=lambda: t[0])
    seen = _record_http(monkeypatch, payload_for=lambda url: [_XLK_INFO])

    SI.fetch_etf_info("key", "XLK")
    t[0] += 86_399.0
    SI.fetch_etf_info("key", "XLK")
    assert len(seen) == 1, "still inside the 1-day window"

    t[0] += 2.0          # now past 86400s
    SI.fetch_etf_info("key", "XLK")
    assert len(seen) == 2, "past one day the entry must be refetched"


def test_cache_key_does_not_collide_across_symbols(monkeypatch):
    monkeypatch.setattr(SI.fmpsdk, "etf_holders", lambda apikey, symbol: [{"s": symbol}])
    assert SI.fetch_etf_holdings("key", "XLK") == [{"s": "XLK"}]
    assert SI.fetch_etf_holdings("key", "XLV") == [{"s": "XLV"}]


def test_cache_key_does_not_collide_across_endpoints(monkeypatch):
    _record_http(monkeypatch, payload_for=lambda url: [{"which": "info"}])
    monkeypatch.setattr(SI.fmpsdk, "etf_holders",
                        lambda apikey, symbol: [{"which": "holder"}])
    assert SI.fetch_etf_info("key", "XLK") == {"which": "info"}
    assert SI.fetch_etf_holdings("key", "XLK") == [{"which": "holder"}]


def test_price_history_cache_key_includes_the_date_window(monkeypatch):
    seen = _record_http(monkeypatch,
                        payload_for=lambda url: {"symbol": "XLK", "historical": []})

    SI.fetch_price_history("key", "XLK", date(2020, 1, 1), date(2025, 8, 22))
    SI.fetch_price_history("key", "XLK", date(2020, 1, 1), date(2025, 8, 22))
    assert len(seen) == 1, "same window -> cached"

    SI.fetch_price_history("key", "XLK", date(2023, 1, 1), date(2025, 8, 22))
    assert len(seen) == 2, "a different window must NOT reuse the first window's rows"


def test_price_history_cache_key_does_not_collide_across_symbols(monkeypatch):
    seen = _record_http(monkeypatch)
    SI.fetch_price_history("key", "XLK", date(2020, 1, 1), date(2025, 8, 22))
    SI.fetch_price_history("key", "XLV", date(2020, 1, 1), date(2025, 8, 22))
    assert [u for u, _ in seen] == [
        "https://financialmodelingprep.com/api/v3/historical-price-full/XLK",
        "https://financialmodelingprep.com/api/v3/historical-price-full/XLV",
    ]


def test_fetch_price_history_passes_the_window_to_fmp(monkeypatch):
    seen = _record_http(monkeypatch)
    SI.fetch_price_history("key", "XLK", date(2020, 1, 1), date(2025, 8, 22))
    url, params = seen[0]
    assert url.endswith("/historical-price-full/XLK")
    assert params["from"] == "2020-01-01" and params["to"] == "2025-08-22"
    assert params["apikey"] == "key"


def test_fetch_dividends_and_splits_use_their_own_endpoints(monkeypatch):
    seen = _record_http(monkeypatch)
    SI.fetch_dividends("key", "XLK")
    SI.fetch_splits("key", "XLK")
    assert [u for u, _ in seen] == [
        "https://financialmodelingprep.com/api/v3/historical-price-full/stock_dividend/XLK",
        "https://financialmodelingprep.com/api/v3/historical-price-full/stock_split/XLK",
    ]


def test_fetch_etf_info_returns_none_when_fmp_has_no_row(monkeypatch):
    _record_http(monkeypatch, payload_for=lambda url: [])
    assert SI.fetch_etf_info("key", "NOPE") is None


def test_fetch_profile_reuses_symbol_snapshot(monkeypatch):
    """The profile lookup is the existing SYMBOL360 helper, not a second copy."""
    calls = []
    monkeypatch.setattr(SI.symbol_snapshot, "fetch_profile",
                        lambda api_key, symbol: (calls.append(symbol),
                                                 {"symbol": symbol, "isEtf": True})[1])
    assert SI.fetch_profile("key", "XLK") == {"symbol": "XLK", "isEtf": True}
    SI.fetch_profile("key", "XLK")
    assert calls == ["XLK"], "cached, and routed through symbol_snapshot"


def test_fetch_ratios_ttm_returns_the_single_row(monkeypatch):
    monkeypatch.setattr(SI.fmpsdk, "financial_ratios_ttm",
                        lambda apikey, symbol: [{"dividendYieldTTM": 0.0044}])
    assert SI.fetch_ratios_ttm("key", "AAPL") == {"dividendYieldTTM": 0.0044}


# ---------------------------------------------------------------------------
# Income: dividend yield + payout ratio (the two FMPCompanyDetailsProvider
# fields), plus an independently-derived cross-check.
# ---------------------------------------------------------------------------
def test_parse_income_converts_fmps_fraction_to_percent():
    """FMP reports dividendYieldTTM as a FRACTION (0.0044); the panel wants %."""
    inc = SI.parse_income({"dividendYieldTTM": 0.0044, "payoutRatioTTM": 0.1523},
                          [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct == pytest.approx(0.44)
    assert inc.payout_ratio_pct == pytest.approx(15.23)


def test_parse_income_accepts_fmps_misspelled_yield_key():
    """FMP ships ``dividendYielTTM`` on some plans; FMPCompanyDetailsProvider
    already tolerates the typo and so must this."""
    inc = SI.parse_income({"dividendYielTTM": 0.0044}, [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct == pytest.approx(0.44)


def test_parse_income_zero_yield_is_a_fact_not_an_unknown():
    inc = SI.parse_income({"dividendYieldTTM": 0.0, "payoutRatioTTM": 0.0},
                          [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct == 0.0
    assert inc.is_unknown("dividend_yield_pct") is False


def test_parse_income_unfetchable_ratios_is_unknown_not_zero():
    inc = SI.parse_income(None, [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct is None and inc.payout_ratio_pct is None
    assert inc.is_unknown("dividend_yield_pct") is True


def test_parse_income_absent_yield_key_is_unknown_and_names_the_keys_tried():
    inc = SI.parse_income({"peRatioTTM": 30.0}, [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct is None
    assert "dividendYieldTTM" in inc.why("dividend_yield_pct")


def test_trailing_12m_dividend_is_zero_for_a_genuine_non_payer():
    inc = SI.parse_income({}, [], last_close=100.0, as_of=AS_OF)
    assert inc.trailing_12m_dividend_per_share == 0.0
    assert inc.is_unknown("trailing_12m_dividend_per_share") is False


def test_trailing_12m_dividend_is_unknown_when_the_history_could_not_be_fetched():
    inc = SI.parse_income({}, None, last_close=100.0, as_of=AS_OF)
    assert inc.trailing_12m_dividend_per_share is None
    assert inc.is_unknown("trailing_12m_dividend_per_share") is True
    assert inc.dividend_yield_pct_computed is None


def test_trailing_12m_window_excludes_dividends_older_than_a_year():
    divs = [SI.DividendEvent(date(2024, 6, 1), 1.0, 1.0),      # >12m before as_of
            SI.DividendEvent(date(2024, 9, 1), 2.0, 2.0),
            SI.DividendEvent(date(2025, 3, 1), 3.0, 3.0)]
    inc = SI.parse_income({}, divs, last_close=100.0, as_of=AS_OF)
    assert inc.trailing_12m_dividend_per_share == pytest.approx(5.0)
    assert inc.dividend_yield_pct_computed == pytest.approx(5.0)


def test_computed_yield_needs_a_last_close():
    inc = SI.parse_income({}, [SI.DividendEvent(date(2025, 3, 1), 3.0, 3.0)],
                          last_close=None, as_of=AS_OF)
    assert inc.dividend_yield_pct_computed is None
    assert inc.is_unknown("dividend_yield_pct_computed") is True


# ---------------------------------------------------------------------------
# The assembler.
# ---------------------------------------------------------------------------
def _five_years_of_bars(start=date(2020, 1, 2), end=date(2025, 8, 22)):
    """A daily-ish series rising 1% per step, plus a fatter adjClose."""
    rows, d, i = [], start, 0
    while d <= end:
        rows.append({"date": d.isoformat(), "close": 100.0 + i,
                     "adjClose": 100.0 + i * 1.2})
        d = date.fromordinal(d.toordinal() + 7)      # weekly bars keep it small
        i += 1
    return {"symbol": "X", "historical": list(reversed(rows))}


def _install_fetchers(monkeypatch, *, profile, etf_info=None, holdings=None,
                      ratios=None, prices=None, dividends=None, splits=None):
    calls = {}

    def _f(name, value):
        def fn(*a, **kw):
            calls.setdefault(name, []).append((a, kw))
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(SI, name, fn)

    _f("fetch_profile", profile)
    _f("fetch_etf_info", etf_info)
    _f("fetch_etf_holdings", holdings if holdings is not None else [])
    _f("fetch_ratios_ttm", ratios if ratios is not None else {})
    _f("fetch_price_history", prices if prices is not None else _five_years_of_bars())
    _f("fetch_dividends", dividends if dividends is not None
       else {"symbol": "X", "historical": []})
    _f("fetch_splits", splits if splits is not None else {"symbol": "X", "historical": []})
    return calls


def test_get_symbol_info_requires_an_explicit_as_of():
    """Nothing here may read the wall clock: a YTD number from an implicit
    'today' can only be trusted, never tested."""
    with pytest.raises(TypeError):
        SI.get_symbol_info("key", "XLK")


def test_get_symbol_info_for_an_etf_populates_holdings_and_returns(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "XLK", "isEtf": True},
                      etf_info=_XLK_INFO, holdings=_XLK_HOLDINGS,
                      ratios={"dividendYieldTTM": 0.0061})

    info = SI.get_symbol_info("key", "XLK", as_of=AS_OF)

    assert info.symbol == "XLK" and info.as_of == AS_OF
    assert info.is_etf is True
    assert info.etf is not None
    assert info.etf.holdings_count == 76
    assert info.etf.top10_weight_pct == pytest.approx(61.25)
    assert info.etf.holdings[0].symbol == "NVDA"
    assert info.income.dividend_yield_pct == pytest.approx(0.61)
    assert set(info.returns) == set(SI.WINDOWS)
    assert info.returns["1y"].total_return_pct is not None
    assert info.series.points, "the chart needs a series"


def test_get_symbol_info_for_a_stock_says_not_an_etf_as_a_FACT(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "AAPL", "isEtf": False})
    info = SI.get_symbol_info("key", "AAPL", as_of=AS_OF)
    assert info.is_etf is False
    assert info.etf is None
    assert info.is_unknown("is_etf") is False
    assert info.is_unknown("etf") is False, "a stock having no holdings is a fact"


def test_get_symbol_info_when_the_profile_fetch_fails_is_etf_is_UNKNOWN(monkeypatch):
    _install_fetchers(monkeypatch, profile=SI.FMPError("boom"))
    info = SI.get_symbol_info("key", "AAPL", as_of=AS_OF)
    assert info.is_etf is None
    assert info.etf is None
    assert info.is_unknown("is_etf") is True
    assert info.is_unknown("etf") is True, "unknown, NOT 'a stock with no holdings'"
    assert "boom" in info.why("is_etf")


def test_get_symbol_info_profile_without_the_isEtf_key_is_unknown(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "WAT"})
    info = SI.get_symbol_info("key", "WAT", as_of=AS_OF)
    assert info.is_etf is None
    assert info.is_unknown("is_etf") is True


def test_get_symbol_info_when_holdings_fetch_fails_records_the_reason(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "XLK", "isEtf": True},
                      etf_info=_XLK_INFO, holdings=SI.FMPError("holder 429"))
    info = SI.get_symbol_info("key", "XLK", as_of=AS_OF)
    assert info.etf is not None
    assert info.etf.holdings == ()
    assert info.etf.is_unknown("holdings") is True
    assert "holder 429" in info.etf.why("holdings")
    # the ETF's own info survived
    assert info.etf.assets_under_management == pytest.approx(119_630_000_000.0)


def test_get_symbol_info_when_prices_fail_every_window_is_unknown(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "XLK", "isEtf": True},
                      prices=SI.FMPError("price 500"))
    info = SI.get_symbol_info("key", "XLK", as_of=AS_OF)
    for w in SI.WINDOWS:
        assert info.returns[w].total_return_pct is None, w
        assert info.returns[w].is_unknown("total_return_pct") is True, w
    assert info.series.points == ()
    assert info.history_start is None


def test_ytd_is_based_on_the_prior_year_close_not_the_first_january_bar(monkeypatch):
    prices = {"symbol": "X", "historical": [
        {"date": "2025-08-22", "close": 120.0, "adjClose": 120.0},
        {"date": "2025-01-03", "close": 110.0, "adjClose": 110.0},
        {"date": "2024-12-31", "close": 100.0, "adjClose": 100.0},
    ]}
    _install_fetchers(monkeypatch, profile={"symbol": "X", "isEtf": False}, prices=prices)
    info = SI.get_symbol_info("key", "X", as_of=AS_OF)
    ytd = info.returns["ytd"]
    assert ytd.start_date == date(2024, 12, 31)
    assert ytd.total_return_pct == pytest.approx(20.0), "not the 9.09% from Jan 3"


def test_price_history_is_fetched_with_a_buffer_before_the_longest_window(monkeypatch):
    """The 5y base bar is the last bar ON OR BEFORE 2020-08-22, so the request
    must start EARLIER than that or the base can never be in the payload."""
    calls = _install_fetchers(monkeypatch, profile={"symbol": "X", "isEtf": False})
    SI.get_symbol_info("key", "X", as_of=AS_OF)
    (_, _, start, end) = calls["fetch_price_history"][0][0]
    assert end == AS_OF
    assert start < date(2020, 8, 22), "no buffer -> the 5y base bar is unreachable"
    assert start >= date(2020, 6, 22), "the buffer should be weeks, not months"


def test_get_symbols_info_returns_one_consistent_entry_per_symbol(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "X", "isEtf": False})
    out = SI.get_symbols_info("key", ["SPY", "QQQ", "SPY"], as_of=AS_OF)
    assert list(out) == ["SPY", "QQQ"], "deduped, input order preserved"
    assert all(isinstance(v, SI.SymbolInfo) for v in out.values())
    assert set(out["SPY"].returns) == set(SI.WINDOWS)


def test_get_symbols_info_isolates_a_symbol_that_blows_up(monkeypatch):
    def profile(api_key, symbol):
        if symbol == "BAD":
            raise RuntimeError("kaboom")
        return {"symbol": symbol, "isEtf": False}

    _install_fetchers(monkeypatch, profile={"symbol": "X", "isEtf": False})
    monkeypatch.setattr(SI, "fetch_profile", profile)

    out = SI.get_symbols_info("key", ["GOOD", "BAD"], as_of=AS_OF)
    assert out["GOOD"].is_etf is False
    assert out["BAD"].is_etf is None
    assert "kaboom" in out["BAD"].why("is_etf")


def test_symbol_info_to_dict_is_json_serializable(monkeypatch):
    import json
    _install_fetchers(monkeypatch, profile={"symbol": "XLK", "isEtf": True},
                      etf_info=_XLK_INFO, holdings=_XLK_HOLDINGS,
                      ratios={"dividendYieldTTM": 0.0061})
    d = SI.symbol_info_to_dict(SI.get_symbol_info("key", "XLK", as_of=AS_OF))
    text = json.dumps(d)               # must not raise
    assert '"symbol": "XLK"' in text
    assert d["as_of"] == "2025-08-22"
    assert d["etf"]["top10_weight_pct"] == pytest.approx(61.25)
    assert d["returns"]["1y"]["total_return_pct"] is not None
    # ``series.start`` is the REQUESTED window start; the first bar lands on or
    # after it (markets are shut on most calendar days).
    assert d["series"]["start"] == "2020-08-22"
    assert d["series"]["points"][0]["date"] >= d["series"]["start"]
    assert d["series"]["points"][-1]["date"] <= d["series"]["end"]


def test_to_dict_keeps_the_unknown_reasons(monkeypatch):
    _install_fetchers(monkeypatch, profile=SI.FMPError("nope"))
    d = SI.symbol_info_to_dict(SI.get_symbol_info("key", "Z", as_of=AS_OF))
    assert d["is_etf"] is None
    assert "nope" in d["details"]["is_etf"]


# ===========================================================================
# Guards added after a 41-mutation run left these alive. Each one below names
# the mutant it kills.
# ===========================================================================

# --- window boundaries (mutant: YTD measured from 1 Jan) --------------------
def test_window_start_dates_are_exactly_these():
    assert SI.window_start_date("ytd", AS_OF) == date(2024, 12, 31), (
        "YTD is measured from the prior year's LAST CLOSE, not from 1 January")
    assert SI.window_start_date("1y", AS_OF) == date(2024, 8, 22)
    assert SI.window_start_date("3y", AS_OF) == date(2022, 8, 22)
    assert SI.window_start_date("5y", AS_OF) == date(2020, 8, 22)


def test_window_start_date_folds_29_february_back_to_28():
    assert SI.window_start_date("1y", date(2024, 2, 29)) == date(2023, 2, 28)
    assert SI.window_start_date("5y", date(2024, 2, 29)) == date(2019, 2, 28)


def test_window_start_date_rejects_an_unknown_window():
    with pytest.raises(ValueError):
        SI.window_start_date("10y", AS_OF)


# --- no lookahead (mutant: end bar = points[-1]) ---------------------------
def test_bars_dated_after_as_of_are_never_used():
    points = [SI.PricePoint(date(2024, 8, 22), 100.0, 100.0),
              SI.PricePoint(date(2025, 8, 22), 110.0, 110.0),
              SI.PricePoint(date(2025, 9, 30), 500.0, 500.0)]   # the future
    r = SI.compute_window_return("1y", points, [], splits=[], as_of=AS_OF)
    assert r.end_date == AS_OF
    assert r.total_return_pct == pytest.approx(10.0), "not the +400% from the future bar"


def test_build_series_drops_bars_after_the_window_end():
    points = [SI.PricePoint(date(2025, 8, 22), 110.0, 110.0),
              SI.PricePoint(date(2025, 9, 30), 500.0, 500.0)]
    bundle = SI.build_series(points, [], [], start=date(2025, 1, 1), end=AS_OF)
    assert [p.date for p in bundle.points] == [AS_OF]


# --- dividend window is (base, end] (mutant: inclusive start) --------------
def test_a_dividend_dated_on_the_base_bar_belongs_to_the_PRIOR_period():
    """The window is half-open on the left. The base bar's own close has already
    gone ex that dividend, so counting it here would book it twice."""
    points = [SI.PricePoint(date(2024, 8, 22), 100.0, 100.0),
              SI.PricePoint(date(2025, 8, 22), 110.0, 110.0)]
    divs = [SI.DividendEvent(date(2024, 8, 22), 9.0, 9.0),   # ON the base date
            SI.DividendEvent(date(2025, 1, 5), 1.0, 1.0)]
    r = SI.compute_window_return("1y", points, divs, splits=[], as_of=AS_OF)
    assert r.dividends_paid_per_share == pytest.approx(1.0)


def test_a_dividend_dated_on_the_end_bar_IS_inside_the_window():
    points = [SI.PricePoint(date(2024, 8, 22), 100.0, 100.0),
              SI.PricePoint(date(2025, 8, 22), 110.0, 110.0)]
    divs = [SI.DividendEvent(AS_OF, 1.0, 1.0)]
    r = SI.compute_window_return("1y", points, divs, splits=[], as_of=AS_OF)
    assert r.dividends_paid_per_share == pytest.approx(1.0)


# --- a base bar we cannot use (mutant: silently 0%) ------------------------
def test_a_base_bar_without_adjclose_makes_TOTAL_return_unknown_not_zero():
    points = [SI.PricePoint(date(2024, 8, 22), close=100.0, adj_close=None),
              SI.PricePoint(date(2025, 8, 22), close=110.0, adj_close=110.0)]
    r = SI.compute_window_return("1y", points, [], splits=[], as_of=AS_OF)
    assert r.total_return_pct is None
    assert "adjClose" in r.why("total_return_pct")
    # the price-only figure still stands: `close` is present on both bars
    assert r.price_return_pct == pytest.approx(10.0)


def test_an_end_bar_without_adjclose_makes_TOTAL_return_unknown_not_zero():
    points = [SI.PricePoint(date(2024, 8, 22), close=100.0, adj_close=100.0),
              SI.PricePoint(date(2025, 8, 22), close=110.0, adj_close=None)]
    r = SI.compute_window_return("1y", points, [], splits=[], as_of=AS_OF)
    assert r.total_return_pct is None
    assert "end" in r.why("total_return_pct")


def test_a_zero_base_adjclose_is_unknown_not_an_infinite_return():
    points = [SI.PricePoint(date(2024, 8, 22), close=100.0, adj_close=0.0),
              SI.PricePoint(date(2025, 8, 22), close=110.0, adj_close=110.0)]
    r = SI.compute_window_return("1y", points, [], splits=[], as_of=AS_OF)
    assert r.total_return_pct is None
    assert r.why("total_return_pct") != ""


# --- non-numeric FMP values (mutant: _as_float -> 0.0) ---------------------
def test_a_non_numeric_price_is_unknown_never_zero():
    payload = {"symbol": "X", "historical": [
        {"date": "2025-08-22", "close": "N/A", "adjClose": ""}]}
    pts = SI.parse_price_history(payload)
    assert pts[0].close is None and pts[0].adj_close is None


def test_a_non_numeric_etf_field_is_unknown_never_zero():
    p = SI.parse_etf_profile(
        {"assetsUnderManagement": "n/a", "peRatio": "—", "holdingsCount": "lots"}, [])
    assert p.assets_under_management is None and p.why("assets_under_management") != ""
    assert p.pe_ratio is None and p.why("pe_ratio") != ""
    assert p.holdings_count is None and p.why("holdings_count") != ""


def test_a_non_numeric_holding_weight_is_unknown_never_zero():
    holdings = _holdings_payload([1.0])
    holdings[0]["weightPercentage"] = "n/a"
    p = SI.parse_etf_profile({"holdingsCount": 1}, holdings)
    assert p.holdings[0].weight_pct is None
    assert p.top10_weight_pct is None, "a weight we cannot read must not sum as 0"


def test_a_non_numeric_yield_is_unknown_never_zero():
    inc = SI.parse_income({"dividendYieldTTM": "N/A"}, [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct is None
    assert inc.is_unknown("dividend_yield_pct") is True


def test_a_boolean_is_not_a_number():
    """``float(True)`` is 1.0 — a JSON ``true`` must not become a price of 1."""
    payload = {"symbol": "X", "historical": [
        {"date": "2025-08-22", "close": True, "adjClose": False}]}
    pts = SI.parse_price_history(payload)
    assert pts[0].close is None and pts[0].adj_close is None


# --- holdings_count (mutant: falls back to len(holdings)) ------------------
def test_holdings_count_absent_from_etf_info_is_unknown_not_the_list_length():
    """``len(holdings)`` is the size of what the holder endpoint returned, which
    may be truncated — presenting it as the fund's size is a WRONG number, and a
    wrong number is worse than a missing one."""
    p = SI.parse_etf_profile({"symbol": "X"}, _holdings_payload([1.0] * 5))
    assert p.holdings_count is None
    assert p.why("holdings_count") != ""
    assert len(p.holdings) == 5, "the caller can still count them itself"


# --- holdings ordering (mutant: payload order kept) ------------------------
def test_top10_is_the_ten_LARGEST_holdings_not_the_first_ten_in_the_payload():
    weights = [1.0] * 10 + [50.0, 40.0]        # the two biggest arrive LAST
    p = SI.parse_etf_profile({"holdingsCount": 12}, _holdings_payload(weights))
    assert p.holdings[0].weight_pct == 50.0
    assert p.holdings[1].weight_pct == 40.0
    assert p.top10_weight_pct == pytest.approx(98.0), "50 + 40 + eight 1.0s"


def test_weightless_holdings_sink_below_real_ones():
    """A row with no weight must never displace a real holding out of the top N."""
    weights = [None] * 3 + [5.0, 4.0]
    p = SI.parse_etf_profile({"holdingsCount": 5}, _holdings_payload(weights))
    assert [h.weight_pct for h in p.holdings[:2]] == [5.0, 4.0]


# --- symbol case (mutant: no .upper()) -------------------------------------
def test_fetchers_normalise_the_symbol_case(monkeypatch):
    """'xlk' and 'XLK' are one symbol: two cache entries would double the FMP
    spend and could show the panel two different numbers for one ticker."""
    seen = _record_http(monkeypatch)
    SI.fetch_price_history("key", "xlk", date(2020, 1, 1), AS_OF)
    SI.fetch_price_history("key", "XLK", date(2020, 1, 1), AS_OF)
    assert len(seen) == 1
    assert seen[0][0].endswith("/historical-price-full/XLK")


def test_get_symbols_info_normalises_the_symbol_case(monkeypatch):
    _install_fetchers(monkeypatch, profile={"symbol": "X", "isEtf": False})
    out = SI.get_symbols_info("key", ["spy", "SPY"], as_of=AS_OF)
    assert list(out) == ["SPY"]


# --- an explicit JSON null (mutant: `k in payload` without the None check) --
# FMP sends `"assetClass": null` rather than omitting the key. Treating that as
# "present" would render the literal string "None" as the ETF's asset class —
# a wrong value that looks like data, which is worse than an honest n/a.
def test_an_explicit_null_etf_field_is_unknown_not_the_string_None():
    p = SI.parse_etf_profile(
        {"assetClass": None, "category": None, "etfCompany": None,
         "inceptionDate": None, "peRatio": None, "assetsUnderManagement": None}, [])
    for name in ("asset_class", "category", "etf_company", "inception_date",
                 "pe_ratio", "assets_under_management"):
        assert getattr(p, name) is None, f"{name} was {getattr(p, name)!r}"
        assert p.why(name) != "", name


def test_an_explicit_null_yield_is_unknown_not_zero():
    inc = SI.parse_income({"dividendYieldTTM": None, "payoutRatioTTM": None},
                          [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct is None and inc.is_unknown("dividend_yield_pct")
    assert inc.payout_ratio_pct is None and inc.is_unknown("payout_ratio_pct")


def test_an_explicit_null_holding_weight_is_unknown_not_zero():
    holdings = _holdings_payload([1.0])
    holdings[0]["weightPercentage"] = None
    holdings[0]["asset"] = None
    p = SI.parse_etf_profile({"holdingsCount": 1}, holdings)
    assert p.holdings[0].weight_pct is None
    assert p.holdings[0].symbol is None, "not the string 'None'"
    assert p.top10_weight_pct is None


def test_a_null_first_candidate_key_falls_through_to_the_next():
    """``_pick`` skips a null and keeps looking — FMP often nulls the modern
    spelling while still populating the legacy one."""
    inc = SI.parse_income({"dividendYieldTTM": None, "dividendYielTTM": 0.0044},
                          [], last_close=100.0, as_of=AS_OF)
    assert inc.dividend_yield_pct == pytest.approx(0.44)


# ---------------------------------------------------------------------------
# Import-leak gate (Amendment A1), matching test_providers_import.py: the panel
# data layer must not drag the live platform, the experts, langchain or the UI
# into a process that only wants symbol data.
# ---------------------------------------------------------------------------
def test_symbol_info_imports_nothing_from_the_live_platform():
    # Same FORBIDDEN set as test_providers_import.py. pandas/fmpsdk are NOT on it:
    # both are declared runtime dependencies of ba2_providers and are pulled by the
    # package __init__ before this module's own imports run.
    from ._leakgate import assert_no_leak
    assert_no_leak("ba2_providers.symbol_info",
                   ["langchain", "langchain_core", "ba2_trade_platform",
                    "ba2_experts", "nicegui"])


def test_every_name_in_dunder_all_actually_exists():
    """``__all__`` is the UI agent's index of this module; a stale entry there is
    a broken import for them."""
    missing = [n for n in SI.__all__ if not hasattr(SI, n)]
    assert missing == []
