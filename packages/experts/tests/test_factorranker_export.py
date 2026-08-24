"""FactorRanker.export_symbol_data: single-symbol universe pin + factor-row export.

FactorRanker is basket-level (see test_factor_ranker_gather_process.py): its
Recommendation carries the WHOLE resolved universe's ranking. export_symbol_data
(Task 8) works by pinning the universe to exactly the requested symbol via
overrides (enabled_instruments + instrument_selection_method/universe_source
"static") -- _resolve_universe reads self.settings directly
(_get_enabled_instruments_config), which is backed by the SAME merged dict
_bypass_instance copies into context.settings, so the override reaches it
without any export_symbol_data override.
"""
import pandas as pd

from ba2_experts.FactorRanker import FactorRanker, data


class _FakeOHLCV:
    """Returned for the unconditional providers.ohlcv() call in _gather.

    Not actually invoked here (momentum's weight is 0, so no OHLCV-based
    fetcher runs), but export_symbol_data's LiveProviderBundle resolves the
    "ohlcv" category unconditionally at the top of _gather.
    """
    def get_ohlcv_data(self, symbol=None, end_date=None, lookback_days=400, interval="1d"):
        return pd.DataFrame({"Close": [10.0, 11.0, 12.0]})


def _resolver(cat, name, **kw):
    return {"ohlcv": _FakeOHLCV()}.get(cat)


def _patch_value_fetcher(monkeypatch, value_by_symbol):
    """Stub data.fetch_value_inputs so value_score is fully deterministic and
    no network/DB call happens. quality/pead are left at weight 0 (skipped by
    _gather's per-factor loop), so they need no stub."""
    def fake_value(symbols, as_of=None):
        return {s: value_by_symbol[s] for s in symbols if s in value_by_symbol}
    monkeypatch.setattr(data, "fetch_value_inputs", fake_value)


_OVERRIDES_BASE = {
    "instrument_selection_method": "static",
    "universe_source": "static",
    "enabled_instruments": {"AAPL": {"enabled": True}},
    "factor_weight_momentum": 0.0, "factor_weight_value": 1.0,
    "factor_weight_quality": 0.0, "factor_weight_pead": 0.0,
    "top_n": 1, "weighting": "equal", "max_weight_per_name": 1.0,
    "gross_exposure": 1.0, "winsorize_pct": 0.0, "pead_drift_window_days": 60,
}


def test_export_symbol_data_pins_single_symbol_universe(monkeypatch):
    """The universe pin (enabled_instruments={"AAPL": ...}) reaches
    _resolve_universe via self.settings/context.settings (they're the same
    dict), so the ranked book contains exactly one row -- AAPL's."""
    _patch_value_fetcher(monkeypatch, {"AAPL": {
        "eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0}})

    result = FactorRanker.export_symbol_data(
        "AAPL", overrides=_OVERRIDES_BASE, providers_resolver=_resolver)

    assert result.error is None, result.error
    assert result.symbol == "AAPL"
    assert not result.skipped
    labels = {m.label for m in result.metrics}
    assert "Composite factor score" in labels
    assert "Factor: value (raw)" in labels
    composite = next(m for m in result.metrics if m.label == "Composite factor score")
    # single-symbol universe -> the composite z-score is arithmetically forced
    # to 0.0 (no cross-section to compare against, zero variance by
    # definition). That is "not computable", NOT a neutral reading, so it is
    # reported as unavailable-with-a-reason rather than as the number 0.000 --
    # see test_factorranker_composite_unavailable.py.
    assert composite.value is None
    assert composite.display == "n/a"
    assert composite.signal is None
    assert "1 symbol" in composite.detail

    # The RAW (pre-z-score) value factor row is the one that actually
    # differs by symbol -- this is the whole point of adding it: eps_ttm/
    # price=10/100 + fcf/ev=0/1 -> value_score = 0.5*0.1 + 0.5*0.0 = 0.05,
    # not the always-0.0 composite above.
    value_raw = next(m for m in result.metrics if m.label == "Factor: value (raw)")
    assert value_raw.value == 0.05
    assert value_raw.signal is None


def test_export_symbol_data_never_touches_db(monkeypatch):
    """No real ExpertInstance id -1 lookup happens (bypass construction)."""
    _patch_value_fetcher(monkeypatch, {"AAPL": {
        "eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0}})
    result = FactorRanker.export_symbol_data(
        "AAPL", overrides=_OVERRIDES_BASE, providers_resolver=_resolver)
    assert result.error is None, result.error


def test_export_symbol_data_empty_universe_reports_skipped(monkeypatch):
    """No candidate instruments -> _process's skip=True path. _build_export_metrics
    must defer to the base class's "Skipped" row (rec.raw_outputs carries no "book"
    at all for a skipped Recommendation) so rec.skip_reason still reaches the UI --
    the ONLY place that text is ever surfaced -- instead of vanishing into an empty
    metrics list."""
    _patch_value_fetcher(monkeypatch, {})  # value is still weight-1.0/enabled; only the universe is empty
    overrides = dict(_OVERRIDES_BASE)
    overrides["enabled_instruments"] = {}
    result = FactorRanker.export_symbol_data(
        "AAPL", overrides=overrides, providers_resolver=_resolver)
    assert result.error is None, result.error
    assert result.skipped is True
    assert len(result.metrics) == 1
    skipped_row = result.metrics[0]
    assert skipped_row.label == "Skipped"
    assert skipped_row.value == "No candidate instruments configured"
    assert "No candidate instruments configured" in skipped_row.display


def test_export_symbol_data_nonzero_min_price_does_not_error(monkeypatch):
    """min_price > 0 would normally make _resolve_universe do a real DB/live-infra
    lookup (get_instance(ExpertInstance, self.id) + get_account_instance(...)) to
    price-filter the universe -- Task 12's settings expander lets a SYMBOL360 user
    dial in any override, including this one, so a bypass (id=EXPORT_BYPASS_ID)
    instance must degrade gracefully (no filtering) rather than erroring out."""
    _patch_value_fetcher(monkeypatch, {"AAPL": {
        "eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0}})
    overrides = dict(_OVERRIDES_BASE)
    overrides["min_price"] = 5.0
    result = FactorRanker.export_symbol_data(
        "AAPL", overrides=overrides, providers_resolver=_resolver)
    assert result.error is None, result.error
    assert not result.skipped
    assert any(m.label == "Composite factor score" for m in result.metrics)


def test_build_export_metrics_matches_by_symbol_not_position(monkeypatch):
    """Directly exercise _build_export_metrics against a MULTI-row ranking (as
    would happen if a caller forgot to pin the universe) to prove it matches
    the requested symbol by name rather than blindly taking ranking[0]."""
    from ba2_common.core.types import OrderRecommendation, Recommendation

    e = FactorRanker.__new__(FactorRanker)
    e._export_symbol = "BBB"
    import logging
    e.logger = logging.getLogger("test.FactorRanker.export")

    rec = Recommendation(
        OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked 2 names, holding 1",
        raw_outputs={"book": {"universe_size": 2, "ranking": [
            {"symbol": "AAA", "rank": 1, "composite": 1.5, "factors": {"value": 1.5},
             "factors_raw": {"value": 0.09}, "target_weight": 1.0, "action": "BUY"},
            {"symbol": "BBB", "rank": 2, "composite": -0.3, "factors": {"value": -0.3},
             "factors_raw": {"value": 0.02}, "target_weight": 0.0, "action": "—"},
        ]}})

    out = e._build_export_metrics(rec, {})
    composite = next(m for m in out if m.label == "Composite factor score")
    assert composite.value == -0.3   # BBB's row, not AAA's (ranking[0])
    assert composite.signal is None  # z-score against a peer -- never a signal badge

    value_raw = next(m for m in out if m.label == "Factor: value (raw)")
    assert value_raw.value == 0.02   # BBB's raw value, not AAA's 0.09


def test_build_export_metrics_unmatched_symbol_returns_no_rows(monkeypatch):
    """A requested symbol absent from a multi-row ranking (unpinned universe,
    wrong symbol) must not silently report someone else's score."""
    from ba2_common.core.types import OrderRecommendation, Recommendation

    e = FactorRanker.__new__(FactorRanker)
    e._export_symbol = "ZZZ"
    import logging
    e.logger = logging.getLogger("test.FactorRanker.export")

    rec = Recommendation(
        OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked 2 names, holding 1",
        raw_outputs={"book": {"universe_size": 2, "ranking": [
            {"symbol": "AAA", "rank": 1, "composite": 1.5, "factors": {"value": 1.5},
             "target_weight": 1.0, "action": "BUY"},
            {"symbol": "BBB", "rank": 2, "composite": -0.3, "factors": {"value": -0.3},
             "target_weight": 0.0, "action": "—"},
        ]}})

    assert e._build_export_metrics(rec, {}) == []
