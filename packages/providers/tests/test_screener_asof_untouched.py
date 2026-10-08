"""The as_of (backtest / hermetic) path of StockScreener must be byte-identical to before the
2026-10-08 live fixes. These tests pass on the pre-fix code too (that is the point): they pin
what the as_of path did, so any live-only behaviour leaking into it fails here.
"""
from datetime import datetime, timezone

import pytest

import ba2_providers
import ba2_providers.fmp_common as fmp_common
import ba2_providers.StockScreener as S

AS_OF = datetime(2024, 3, 15, tzinfo=timezone.utc)


class _Prov:
    def __init__(self, rows):
        self.rows, self.filters = rows, None

    def screen_stocks(self, filters, as_of=None):
        self.filters = filters
        return [dict(r) for r in self.rows]


ROWS = [
    {"symbol": "AAA", "price": 50.0, "market_cap": 5e9, "volume": 1000, "float_shares": None},
    {"symbol": "BBB", "price": 40.0, "market_cap": 4e9, "volume": 2000, "float_shares": None},
]


def _hermetic_guard(monkeypatch):
    """Any attempt to reach the network from the screen is a test failure (a hermetic run
    raises FMPHermeticViolation there)."""
    def boom(*a, **k):
        raise AssertionError("HTTP attempted on the as_of path")
    monkeypatch.setattr(fmp_common, "fmp_http_get", boom)
    monkeypatch.setattr(S.StockScreener, "_fetch_quotes_chunked", boom)


def _run(monkeypatch, settings, bars=None, guard=True):
    prov = _Prov(ROWS)
    monkeypatch.setattr(ba2_providers, "get_provider", lambda cat, name, **kw: prov)
    if guard:
        _hermetic_guard(monkeypatch)
    sc = S.StockScreener(settings, as_of=AS_OF)
    if bars is not None:
        monkeypatch.setattr(sc, "_fetch_history_bulk", lambda syms, lookback_days: bars)
    return sc.screen(), prov


BASE = {"screener_price_drop_pct": 0, "screener_max_stocks": 10, "screener_price_min": 0,
        "screener_market_cap_min": 0}


def test_rvol_zero_skips_stage_2_entirely_and_makes_no_http_call(monkeypatch):
    out, prov = _run(monkeypatch, {**BASE, "screener_relative_volume_min": 0, "screener_volume_min": 500_000,
                                   "screener_volume_max": 9_000_000, "screener_float_min": 1e7,
                                   "screener_float_max": 5e8})
    # the provider rows come back untouched (no avg_volume / relative_volume added, same values)
    assert out["results"] == ROWS
    assert out["stats"] == {"screener_candidates": 2, "final_count": 2}
    # as_of keeps sending volume_min / float_max to the historical provider, as before
    assert prov.filters["volume_min"] == 500_000 and prov.filters["float_max"] == 5e8


def test_rvol_positive_keeps_the_old_semantics(monkeypatch):
    day = lambda i: f"2024-03-{i + 1:02d}"
    bars = {
        # last session 9M, mean ~1.4M: the OLD volume_max tests the last session -> dropped
        "AAA": [{"date": day(i), "close": 50.0, "high": 50, "low": 50, "volume": 1_000_000} for i in range(9)]
               + [{"date": day(9), "close": 50.0, "high": 50, "low": 50, "volume": 9_000_000}],
        "BBB": [],     # no bars: OLD behaviour = rvol 0 -> dropped by RVOL, not by a "no history" rule
    }
    out, _ = _run(monkeypatch, {**BASE, "screener_relative_volume_min": 0.5, "screener_volume_max": 5_000_000,
                                "screener_volume_min": 5_000_000_000}, bars=bars)
    assert out["results"] == []
    assert out["stats"] == {"screener_candidates": 2, "dropped_rvol": 1, "dropped_float": 0,
                            "dropped_volume_max": 1}     # empty after stage 2: early return


def test_rvol_positive_volume_min_is_not_applied_here(monkeypatch):
    day = lambda i: f"2024-03-{i + 1:02d}"
    bars = {s: [{"date": day(i), "close": 50.0, "high": 50, "low": 50, "volume": 1_000} for i in range(10)]
            for s in ("AAA", "BBB")}
    out, _ = _run(monkeypatch, {**BASE, "screener_relative_volume_min": 0.5, "screener_volume_min": 500_000},
                  bars=bars)
    assert {r["symbol"] for r in out["results"]} == {"AAA", "BBB"}      # the provider owns volume_min
    assert "dropped_volume_min" not in out["stats"] and "dropped_no_history" not in out["stats"]


def test_as_of_never_loads_the_float_table(monkeypatch):
    from ba2_providers.screener import float_filter as ff
    monkeypatch.setattr(ff, "load_float_table", lambda: (_ for _ in ()).throw(AssertionError("float table")))
    out, _ = _run(monkeypatch, {**BASE, "screener_relative_volume_min": 0, "screener_float_min": 1e7})
    assert len(out["results"]) == 2
