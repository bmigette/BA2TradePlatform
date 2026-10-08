"""Live StockScreener: float, average-volume and rvol_min == 0 behaviour (2026-10-08).

Measured facts these tests pin (vendor = FMP):
  * the vendor screener has NO float parameter (``floatSharesUnder`` was silently ignored and no
    float comes back) -> the float bounds are OUR filter, fed by the bulk float table;
  * the vendor's ``volumeMoreThan`` tests the ``volume`` field = the CURRENT session's volume so far,
    not an average -> the average-volume floor/ceiling are OUR filters over the daily bars;
  * ``screener_relative_volume_min == 0`` used to skip the whole volume/RVOL stage, and with it
    volume_max and the live price / market-cap refresh.

No network: every vendor boundary is faked.
"""
import pytest

import ba2_providers
import ba2_providers.StockScreener as S
from ba2_providers.screener import float_filter as ff
from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider


def _bars(vols, close=50.0):
    """Oldest-first finished sessions, all dated in the past."""
    return [{"date": f"2020-01-{i + 1:02d}", "open": close, "high": close, "low": close,
             "close": close, "volume": v} for i, v in enumerate(vols)]


def _cand(sym, price=50.0, mcap=5e9, volume=123):
    # ``volume`` mimics the vendor's session-so-far volume (tiny at the open).
    return {"symbol": sym, "price": price, "market_cap": mcap, "volume": volume}


class _FakeProv:
    def __init__(self, rows):
        self.rows = rows
        self.filters = None

    def screen_stocks(self, filters, as_of=None):
        self.filters = filters
        return [dict(r) for r in self.rows]


def _screen(monkeypatch, rows, settings, bars_map=None, quotes=None):
    prov = _FakeProv(rows)
    monkeypatch.setattr(ba2_providers, "get_provider", lambda cat, name, **kw: prov)
    base = {"screener_price_drop_pct": 0, "screener_max_stocks": 100, "screener_float_min": 0,
            "screener_volume_min": 0, "screener_relative_volume_min": 0, "screener_price_min": 0,
            "screener_market_cap_min": 0}
    sc = S.StockScreener({**base, **settings})
    monkeypatch.setattr(sc, "_fetch_history_bulk", lambda syms, lookback_days: bars_map or {})
    calls = {"quotes": 0}

    def fake_quotes(syms, *a, **k):
        calls["quotes"] += 1
        return quotes or {}

    monkeypatch.setattr(sc, "_fetch_quotes_chunked", fake_quotes)
    out = sc.screen()
    return out, prov, calls


# ------------------------------------------------------------------ vendor request
def test_stockscreener_never_sends_float_or_volume_floor_to_the_vendor(monkeypatch):
    out, prov, _ = _screen(monkeypatch, [_cand("AAA")],
                           {"screener_float_min": 1e7, "screener_float_max": 5e8, "screener_volume_min": 500_000,
                            "screener_volume_max": 9e9},
                           bars_map={"AAA": _bars([1_000_000] * 20)})
    # stage 1b needs the float table: not under test here, so it is faked below
    assert "float_max" not in prov.filters and "float_min" not in prov.filters
    assert "volume_min" not in prov.filters and "volume_max" not in prov.filters


@pytest.fixture(autouse=True)
def _float_table(monkeypatch):
    monkeypatch.setattr(ff, "load_float_table",
                        lambda: {"SMALL": 2e6, "OK": 1e8, "HUGE": 9e9, "AAA": 1e8})


def test_provider_params_have_no_float_and_use_average_volume():
    p = FMPScreenerProvider.__new__(FMPScreenerProvider)
    p.api_key = "k"
    params = p._build_params({"float_max": 5e8, "float_min": 1e7, "volume_min": 500_000, "price_min": 20})
    assert "floatSharesUnder" not in params
    assert "volumeMoreThan" not in params
    assert params["avgVolumeMoreThan"] == 500_000


# ------------------------------------------------------------------ float
def test_apply_float_filter_rules():
    rows = [{"symbol": "SMALL"}, {"symbol": "OK"}, {"symbol": "HUGE"}, {"symbol": "NODATA"}, {"symbol": "ZERO"}]
    table = {"SMALL": 2e6, "OK": 1e8, "HUGE": 9e9, "ZERO": 0}
    kept, st = ff.apply_float_filter(rows, table, 1e7, 5e8)
    assert [r["symbol"] for r in kept] == ["OK", "NODATA", "ZERO"]      # unknown/zero float passes
    assert st == {"dropped_float": 2, "float_unknown": 2}
    assert kept[0]["float_shares"] == 1e8
    # inclusive on the keeping side, a bound of 0 is off
    kept, _ = ff.apply_float_filter([{"symbol": "OK"}], {"OK": 1e8}, 1e8, 1e8)
    assert len(kept) == 1
    kept, _ = ff.apply_float_filter(rows, table, 0, 0)
    assert len(kept) == 5


def test_screen_applies_float_bounds(monkeypatch):
    rows = [_cand("SMALL"), _cand("OK"), _cand("HUGE"), _cand("NODATA")]
    bars = {s: _bars([1_000_000] * 20) for s in ("SMALL", "OK", "HUGE", "NODATA")}
    out, _, _ = _screen(monkeypatch, rows, {"screener_float_min": 1e7, "screener_float_max": 5e8}, bars_map=bars)
    assert {r["symbol"] for r in out["results"]} == {"OK", "NODATA"}
    assert out["stats"]["dropped_float"] == 2


def test_screen_with_float_bound_fails_loudly_when_the_table_is_unavailable(monkeypatch):
    from ba2_providers.StockScreener import ScreenerDataError

    def boom():
        raise ScreenerDataError("shares_float/all unavailable")
    monkeypatch.setattr(ff, "load_float_table", boom)
    with pytest.raises(ScreenerDataError):
        _screen(monkeypatch, [_cand("OK")], {"screener_float_min": 1e7},
                bars_map={"OK": _bars([1_000_000] * 20)})


def test_float_table_rejects_a_non_list_payload():
    with pytest.raises(RuntimeError):
        ff.parse_float_table({"Error Message": "Limit Reach."})
    assert ff.parse_float_table([{"symbol": "a", "floatShares": 5}, {"symbol": "B", "floatShares": 0}]) == {"A": 5.0}


def test_provider_screen_stocks_applies_float_for_other_callers(monkeypatch):
    """Penny / the Tools page call the provider directly with float_max."""
    import ba2_providers.fmp_common as fmp_common
    payload = [{"symbol": "OK", "price": 5, "marketCap": 1e8, "volume": 1}, {"symbol": "HUGE", "price": 5,
               "marketCap": 1e8, "volume": 1}]

    class _R:
        def json(self):
            return payload

    seen = {}

    def fake_get(url, params=None, **k):
        seen["params"] = params
        return _R()

    monkeypatch.setattr(fmp_common, "fmp_http_get", fake_get)
    p = FMPScreenerProvider.__new__(FMPScreenerProvider)
    p.api_key = "k"
    res = p.screen_stocks({"float_max": 5e8})
    assert [r["symbol"] for r in res] == ["OK"]
    assert "floatSharesUnder" not in seen["params"]


# ------------------------------------------------------------------ average volume
def test_volume_min_is_an_average_volume_floor_not_session_volume(monkeypatch):
    # Both candidates have a tiny vendor "volume" (session so far = 123); only the average differs.
    rows = [_cand("BUSY"), _cand("QUIET")]
    bars = {"BUSY": _bars([2_000_000] * 20), "QUIET": _bars([100_000] * 20)}
    out, _, _ = _screen(monkeypatch, rows, {"screener_volume_min": 500_000}, bars_map=bars)
    assert [r["symbol"] for r in out["results"]] == ["BUSY"]
    assert out["stats"]["dropped_volume_min"] == 1
    assert out["results"][0]["avg_volume"] == 2_000_000


def test_volume_min_uses_the_mean_of_the_last_20_finished_sessions(monkeypatch):
    # 30 sessions: the first 10 huge, the last 20 at 400k -> mean(last 20) = 400k < 500k -> dropped.
    bars = {"X": _bars([50_000_000] * 10 + [400_000] * 20)}
    out, _, _ = _screen(monkeypatch, [_cand("X")], {"screener_volume_min": 500_000}, bars_map=bars)
    assert out["results"] == []


def _twenty(extra_nobars=0):
    rows = [_cand(f"S{i}") for i in range(20 - extra_nobars)] + [_cand(f"NB{i}") for i in range(extra_nobars)]
    bars = {f"S{i}": _bars([1_000_000] * 20) for i in range(20 - extra_nobars)}
    return rows, bars


@pytest.mark.parametrize("setting", [{"screener_volume_min": 500_000}, {"screener_volume_max": 9e9},
                                     {"screener_relative_volume_min": 0.5}])
def test_symbol_with_no_bars_is_dropped_as_no_history_whichever_bound_is_set(monkeypatch, setting):
    # 1 of 20 without bars = 5% <= 10%: dropped under its OWN label, never as "low volume"
    rows, bars = _twenty(); rows.append(_cand("NOBARS"))
    out, _, _ = _screen(monkeypatch, rows[:19] + [rows[-1]], setting, bars_map={k: v for k, v in bars.items() if k != "S19"})
    st = out["stats"]
    assert "NOBARS" not in {r["symbol"] for r in out["results"]}
    assert st["dropped_no_history"] == 1
    assert st["dropped_volume_min"] == 0 and st["dropped_volume_max"] == 0 and st["dropped_rvol"] == 0


def test_no_bars_with_no_volume_bound_set_passes(monkeypatch):
    # nothing needs bars (rvol 0, no floor, no ceiling): a symbol without history is not judged
    out, _, _ = _screen(monkeypatch, [_cand("NOBARS")], {}, bars_map={})
    assert [r["symbol"] for r in out["results"]] == ["NOBARS"]
    assert out["stats"]["dropped_no_history"] == 0


def test_too_many_symbols_without_history_fails_loudly(monkeypatch):
    from ba2_providers.StockScreener import ScreenerDataError, SCREENER_DATA_FAILURE_MAX_FRACTION
    assert SCREENER_DATA_FAILURE_MAX_FRACTION == 0.10
    rows, bars = _twenty(extra_nobars=3)          # 15% without history
    with pytest.raises(ScreenerDataError, match="3/20"):
        _screen(monkeypatch, rows, {"screener_volume_min": 500_000}, bars_map=bars)
    rows, bars = _twenty(extra_nobars=2)          # exactly 10%: tolerated
    out, _, _ = _screen(monkeypatch, rows, {"screener_volume_min": 500_000}, bars_map=bars)
    assert out["stats"]["dropped_no_history"] == 2


def test_fewer_than_20_bars_uses_the_mean_of_what_exists(monkeypatch):
    bars = {"NEW": _bars([600_000, 800_000, 1_000_000])}          # 3 finished sessions
    out, _, _ = _screen(monkeypatch, [_cand("NEW")], {"screener_volume_min": 700_000}, bars_map=bars)
    assert out["results"][0]["avg_volume"] == 800_000.0
    out, _, _ = _screen(monkeypatch, [_cand("NEW")], {"screener_volume_min": 900_000}, bars_map=bars)
    assert out["results"] == []


def test_todays_forming_bar_is_excluded_from_the_average(monkeypatch):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bars = _bars([1_000_000] * 20)
    bars.append({"date": today, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 900_000_000})
    out, _, _ = _screen(monkeypatch, [_cand("F")], {"screener_volume_max": 5_000_000}, bars_map={"F": bars})
    assert [r["symbol"] for r in out["results"]] == ["F"]
    assert out["results"][0]["avg_volume"] == 1_000_000.0


def test_every_stage_accounting_key_exists_on_a_live_run(monkeypatch):
    out, _, _ = _screen(monkeypatch, [_cand("A")], {}, bars_map={})
    for k in ("dropped_float", "dropped_volume_min", "dropped_volume_max", "dropped_no_history", "dropped_rvol"):
        assert out["stats"][k] == 0


def test_dropped_float_is_the_float_stage_count_after_stage_2(monkeypatch):
    rows = [_cand("SMALL"), _cand("OK")]
    bars = {s: _bars([1_000_000] * 20) for s in ("SMALL", "OK")}
    out, _, _ = _screen(monkeypatch, rows, {"screener_float_min": 1e7}, bars_map=bars)
    assert out["stats"]["dropped_float"] == 1      # not overwritten by stage 2's own 0


# ------------------------------------------------------------------ live vs metric store
def test_live_avg_volume_equals_the_metric_store_volume_column(monkeypatch):
    """Live ``avg_volume`` (mean of the last <= 20 finished sessions INCLUDING the latest) is
    the store's ``volume`` column (``rolling(20, min_periods=1).mean()`` including the day)."""
    import pandas as pd
    from ba2_providers.screener.metric_store import compute_daily_metrics
    vols = [1_000_000 + 37_000 * i + (i % 3) * 11_111 for i in range(45)]
    idx = pd.date_range("2020-01-01", periods=len(vols), freq="B")
    ohlcv = pd.DataFrame({"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.0, "Volume": vols}, index=idx)
    store_vol = compute_daily_metrics(ohlcv)["volume"]
    sc = S.StockScreener({})
    bars = [{"date": d.strftime("%Y-%m-%d"), "open": 10, "high": 11, "low": 9, "close": 10, "volume": v}
            for d, v in zip(idx, vols)]
    for n in (3, 19, 20, 33, 45):                    # incl. fewer than 20 sessions
        monkeypatch.setattr(sc, "_fetch_history_bulk", lambda syms, lookback_days, n=n: {"X": bars[:n]})
        live = sc._quotes_from_bars(["X"])["X"]["avgVolume"]
        assert live == pytest.approx(round(float(store_vol.iloc[n - 1]), 2))


def test_live_rvol_denominator_includes_the_numerator_session_unlike_the_store(monkeypatch):
    """Documented difference the simulation must mirror: live rvol = V[-1] / mean(last 20 incl. V[-1])."""
    sc = S.StockScreener({})
    bars = _bars([1_000_000] * 19 + [3_000_000])
    monkeypatch.setattr(sc, "_fetch_history_bulk", lambda syms, lookback_days: {"X": bars})
    q = sc._quotes_from_bars(["X"])["X"]
    assert q["avgVolume"] == 1_100_000.0 and q["volume"] == 3_000_000
    assert round(q["volume"] / q["avgVolume"], 2) == 2.73       # the store would give 3.0


def test_volume_max_is_an_average_not_the_last_session(monkeypatch):
    # last session 9M (a spike) but the 20-session mean is ~1.4M: a 5M ceiling must NOT drop it,
    # while a 1M ceiling must.
    bars = {"SPIKE": _bars([1_000_000] * 19 + [9_000_000])}
    out, _, _ = _screen(monkeypatch, [_cand("SPIKE")], {"screener_volume_max": 5_000_000}, bars_map=bars)
    assert [r["symbol"] for r in out["results"]] == ["SPIKE"]
    out, _, _ = _screen(monkeypatch, [_cand("SPIKE")], {"screener_volume_max": 1_000_000}, bars_map=bars)
    assert out["results"] == []


# ------------------------------------------------------------------ rvol_min == 0
def test_rvol_zero_still_applies_volume_max(monkeypatch):
    bars = {"HEAVY": _bars([8_000_000] * 20), "LIGHT": _bars([1_000_000] * 20)}
    out, _, _ = _screen(monkeypatch, [_cand("HEAVY"), _cand("LIGHT")],
                        {"screener_relative_volume_min": 0, "screener_volume_max": 5_000_000}, bars_map=bars)
    assert [r["symbol"] for r in out["results"]] == ["LIGHT"]


def test_rvol_zero_still_refreshes_live_price_and_market_cap(monkeypatch):
    bars = {"AAA": _bars([1_000_000] * 20)}
    out, _, calls = _screen(monkeypatch, [_cand("AAA", price=50.0, mcap=5e9)],
                            {"screener_relative_volume_min": 0}, bars_map=bars,
                            quotes={"AAA": {"price": 41.5, "marketCap": 4.2e9}})
    assert calls["quotes"] == 1
    assert out["results"][0]["price"] == 41.5
    assert out["results"][0]["market_cap"] == 4.2e9


def test_rvol_zero_does_not_filter_on_rvol(monkeypatch):
    # last session 100k vs mean ~1M => rvol ~0.1; with the filter off it stays.
    bars = {"LOWR": _bars([1_000_000] * 19 + [100_000])}
    out, _, _ = _screen(monkeypatch, [_cand("LOWR")], {"screener_relative_volume_min": 0}, bars_map=bars)
    assert [r["symbol"] for r in out["results"]] == ["LOWR"]
    # and with the filter on it goes
    out, _, _ = _screen(monkeypatch, [_cand("LOWR")], {"screener_relative_volume_min": 1.0}, bars_map=bars)
    assert out["results"] == []


def test_rvol_zero_ranks_by_the_refreshed_market_cap(monkeypatch):
    bars = {"A": _bars([1_000_000] * 20), "B": _bars([1_000_000] * 20)}
    out, _, _ = _screen(monkeypatch, [_cand("A", mcap=9e9), _cand("B", mcap=5e9)],
                        {"screener_relative_volume_min": 0, "screener_max_stocks": 1}, bars_map=bars,
                        quotes={"A": {"price": 50, "marketCap": 3e9}, "B": {"price": 50, "marketCap": 6e9}})
    assert [r["symbol"] for r in out["results"]] == ["B"]
