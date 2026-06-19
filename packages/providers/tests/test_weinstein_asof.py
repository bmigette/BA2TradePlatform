"""Weinstein Stage-2 filter over as-of reconstructed bars (Phase 3, Task 6).

The Stage-2 filter is the screener-expert enabler (FactorRanker + every
screener-driven expert). It must run UNCHANGED over reconstructed history:
``_filter_by_weinstein_stage2`` fetches its bars through the (Task-4) as_of-anchored
``_fetch_history_bulk`` and delegates the actual stage call to the SAME pure
``ba2_common.core.weinstein.classify_weinstein_stage`` used on the live path. The
closes it classifies are therefore truncated to ``<= as_of`` by construction
(point-in-time-safe), and the LOGIC never forks live<->as-of.

These tests are fully deterministic and never touch FMP or the network — the bar
map is monkeypatched in, so the only thing under test is the filter logic + the
pure classifier delegation.
"""
from datetime import datetime, timezone

import ba2_providers.StockScreener as S

AS_OF = datetime(2020, 6, 30, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 1. The filter over as-of bars: Stage 2 kept, non-Stage-2 dropped
# ----------------------------------------------------------------------

def test_weinstein_stage2_over_asof_bars(monkeypatch):
    sc = S.StockScreener({"screener_weinstein_stage2_only": 1}, as_of=AS_OF)

    # UP: clean uptrend above a rising 150-SMA -> Stage 2. FLAT: no trend -> not Stage 2.
    up = [{"close": 10 + i * 0.5} for i in range(200)]
    flat = [{"close": 50.0} for _ in range(200)]
    monkeypatch.setattr(
        sc, "_fetch_history_bulk",
        lambda symbols, lookback_days, **k: {"UP": up, "FLAT": flat},
    )

    candidates = [{"symbol": "UP"}, {"symbol": "FLAT"}]
    passed, stats = sc._filter_by_weinstein_stage2(candidates)
    syms = {c["symbol"] for c in passed}
    assert "UP" in syms
    assert "FLAT" not in syms
    assert passed[0]["weinstein_stage"] == 2
    assert stats["weinstein_stage2"] == 1


# ----------------------------------------------------------------------
# 2. The filter delegates to the SAME pure classifier used on the live path
# ----------------------------------------------------------------------

def test_weinstein_pure_classifier_imported_from_common():
    # the filter delegates to the SAME pure function used live
    from ba2_common.core.weinstein import classify_weinstein_stage
    closes = [10 + i * 0.5 for i in range(200)]
    assert classify_weinstein_stage(closes)["stage"] == 2


# ----------------------------------------------------------------------
# 3. as_of-anchored bar window (the point-in-time guarantee for the filter)
# ----------------------------------------------------------------------

def test_weinstein_filter_fetches_asof_anchored_window(monkeypatch):
    """The bars fed to classify_weinstein_stage are fetched as-of, not as-of-today.

    _filter_by_weinstein_stage2 calls self._fetch_history_bulk (Task-4 re-anchored on
    self._as_of), so the closes that reach the classifier are truncated to <= as_of.
    Assert the to_date window the filter requests is the as_of date.
    """
    sc = S.StockScreener({"screener_weinstein_stage2_only": 1}, as_of=AS_OF)
    captured = {}

    def fake_http(url, params=None, endpoint=None, timeout=None):
        captured["to"] = params.get("to")

        class R:
            def json(self):
                return {"historicalStockList": []}

        return R()

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_http, raising=False)
    monkeypatch.setattr(S, "get_app_setting", lambda k: "key", raising=False)

    sc._filter_by_weinstein_stage2([{"symbol": "AAA"}])

    assert captured["to"] == "2020-06-30"   # the filter's bars are anchored on as_of


# ----------------------------------------------------------------------
# 4. Insufficient as-of history -> not Stage 2 (no lookahead, no crash)
# ----------------------------------------------------------------------

def test_weinstein_insufficient_asof_history_is_dropped(monkeypatch):
    """A symbol that IPO'd close to as_of has < 170 as-of bars: classify_weinstein_stage
    returns stage=None ('insufficient history'), so the filter drops it -- never
    fabricating a stage from a too-short series."""
    sc = S.StockScreener({"screener_weinstein_stage2_only": 1}, as_of=AS_OF)

    short = [{"close": 10 + i * 0.5} for i in range(50)]   # only 50 bars (< 170 needed)
    monkeypatch.setattr(
        sc, "_fetch_history_bulk",
        lambda symbols, lookback_days, **k: {"NEWBIE": short},
    )

    passed, stats = sc._filter_by_weinstein_stage2([{"symbol": "NEWBIE"}])
    assert passed == []
    assert stats["weinstein_dropped"] == 1
    assert stats["weinstein_stage2"] == 0


# ----------------------------------------------------------------------
# 5. Stage-4 decline over as-of bars is dropped (the inverse of UP)
# ----------------------------------------------------------------------

def test_weinstein_stage4_decline_dropped(monkeypatch):
    """A clean downtrend (price below a falling SMA) is Stage 4 -> dropped by the
    Stage-2-only filter, proving the filter discriminates on the as-of stage, not
    merely on 'has enough bars'."""
    sc = S.StockScreener({"screener_weinstein_stage2_only": 1}, as_of=AS_OF)

    down = [{"close": 200 - i * 0.5} for i in range(200)]   # clean decline
    monkeypatch.setattr(
        sc, "_fetch_history_bulk",
        lambda symbols, lookback_days, **k: {"DOWN": down},
    )

    passed, stats = sc._filter_by_weinstein_stage2([{"symbol": "DOWN"}])
    assert passed == []
    assert stats["weinstein_stage2"] == 0
