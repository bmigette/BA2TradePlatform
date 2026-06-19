"""Phase 1 Task 9: FactorRanker _gather(as_of)/_process(target weights) split.

Proves: (1) fetch_close_prices threads as_of -> end_date (the data-layer gap fix);
(2) _compute_factor threads as_of to ALL four fetchers; (3) _process is PURE
(composite z-score -> rank -> long-only top-N target weights) and returns a single
basket-level Recommendation whose raw_outputs carries the TARGET WEIGHTS dict + the
ranked book (no ExpertRecommendation seam); (4) empty universe / no enabled factors
=> skip=True (preserving the live _mark_skipped outcomes); (5) analyze_as_of runs the
SAME _gather+_process as the live path (the golden-test logic-equality contract);
(6) as_of=None is logic-identical to as_of=<date> when the fetchers return the same
fixtures (the byte-equality invariant for this expert's decision).

FactorRanker is BASKET-level: current_price is None (the decision is cross-sectional,
not a single instrument's price), and the targets are handed to the engine directly.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_experts.FactorRanker import FactorRanker, data
from ba2_common.core.types import OrderRecommendation, Recommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


def _ohlcv_module():
    """Return the FMPOHLCVProvider *submodule* object (not the class).

    ba2_providers.ohlcv.__init__ re-exports the FMPOHLCVProvider *class* under the
    package attribute ``FMPOHLCVProvider``, shadowing the submodule of the same
    name; so ``import ba2_providers.ohlcv.FMPOHLCVProvider as m`` binds the class.
    The real submodule is registered in sys.modules under its full dotted name —
    that's where ``data.fetch_close_prices`` resolves the class at call time, so
    that's what must be monkeypatched.
    """
    import sys
    import ba2_providers.ohlcv.FMPOHLCVProvider  # noqa: F401 (ensures it is imported)
    return sys.modules["ba2_providers.ohlcv.FMPOHLCVProvider"]

UNIVERSE = ["AAA", "BBB", "CCC", "DDD"]

# Per-symbol value inputs whose value_score (0.5*E/P + 0.5*FCF/EV) ranks
# AAA > BBB > CCC > DDD (monotone earnings yield), so the ranking is observable.
VALUE_INPUTS = {
    "AAA": {"eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "BBB": {"eps_ttm": 7.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "CCC": {"eps_ttm": 4.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "DDD": {"eps_ttm": 1.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
}

# value-only settings (weights: momentum=0, value=1, quality=0, pead=0) so the
# ranking is purely the value factor and momentum's fetch_close_prices is NOT called
# in the factor loop (it IS still called once for the bundle "prices" entry).
SETTINGS_VALUE_ONLY = {
    "winsorize_pct": 0.0,
    "top_n": 2,
    "weighting": "equal",
    "max_weight_per_name": 1.0,
    "gross_exposure": 1.0,
    "pead_drift_window_days": 60,
    "_factor_weights": {"momentum": 0.0, "value": 1.0, "quality": 0.0, "pead": 0.0},
}


class FakeOHLCV:
    """OHLCV provider stub: records the end_date it is called with (as_of spy)."""
    def __init__(self):
        self.calls = []

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=400, interval="1d"):
        self.calls.append({"symbol": symbol, "end_date": end_date,
                           "lookback_days": lookback_days, "interval": interval})
        return pd.DataFrame({"Close": [10.0, 11.0, 12.0]})


def _provider_bundle(ohlcv=None):
    ohlcv = ohlcv or FakeOHLCV()
    return LiveProviderBundle(lambda cat, name, **kw: ohlcv)


def _expert(universe=UNIVERSE, holdings=None, settings=SETTINGS_VALUE_ONLY,
            value_inputs=VALUE_INPUTS, monkeypatch=None):
    """Build a FactorRanker via __new__ (bypassing __init__'s DB read), with the
    universe/holdings resolution + data fetchers stubbed (no network, no DB)."""
    import logging
    e = FactorRanker.__new__(FactorRanker)
    e.id = 1
    e.logger = logging.getLogger("test.FactorRanker")
    e._gather_settings = settings
    e._resolve_universe = lambda *a, **k: list(universe)  # accepts as_of
    e._gather_holdings = lambda: list(holdings or [])
    return e


def _patch_fetchers(monkeypatch, value_inputs=VALUE_INPUTS, captured=None):
    """Patch data.fetch_* on the module so _compute_factor / _gather use fixtures.
    Records the as_of each fetcher was called with into `captured` (a dict)."""
    captured = captured if captured is not None else {}

    def fake_value(symbols, as_of=None):
        captured["value_as_of"] = as_of
        return {s: value_inputs[s] for s in symbols if s in value_inputs}

    def fake_quality(symbols, as_of=None):
        captured["quality_as_of"] = as_of
        return {}

    def fake_pead(symbols, as_of=None):
        captured["pead_as_of"] = as_of
        return {}

    def fake_prices(symbols, as_of=None, **kw):
        captured["prices_as_of"] = as_of
        return {s: pd.Series([10.0, 11.0, 12.0]) for s in symbols}

    monkeypatch.setattr(data, "fetch_value_inputs", fake_value)
    monkeypatch.setattr(data, "fetch_quality_inputs", fake_quality)
    monkeypatch.setattr(data, "fetch_pead_inputs", fake_pead)
    monkeypatch.setattr(data, "fetch_close_prices", fake_prices)
    return captured


# --------------------------------------------------------------------------- #
# Step 1: fetch_close_prices threads as_of -> end_date (data-layer gap fix)
# --------------------------------------------------------------------------- #

def test_fetch_close_prices_passes_end_date_equal_as_of(monkeypatch):
    """The Task 9 gap #1 fix: fetch_close_prices(as_of=X) must call the OHLCV
    provider with end_date == X (point-in-time anchor)."""
    fake = FakeOHLCV()
    monkeypatch.setattr(_ohlcv_module(), "FMPOHLCVProvider", lambda: fake)
    out = data.fetch_close_prices(["AAA", "BBB"], lookback_days=400, as_of=NOW)
    assert set(out) == {"AAA", "BBB"}
    assert all(c["end_date"] == NOW for c in fake.calls), fake.calls
    assert all(c["lookback_days"] == 400 for c in fake.calls)


def test_fetch_close_prices_asof_none_uses_now(monkeypatch):
    """as_of=None (live) maps end_date to 'now' (byte-identical to old live fetch)."""
    fake = FakeOHLCV()
    monkeypatch.setattr(_ohlcv_module(), "FMPOHLCVProvider", lambda: fake)
    before = datetime.now(timezone.utc)
    data.fetch_close_prices(["AAA"], as_of=None)
    after = datetime.now(timezone.utc)
    assert len(fake.calls) == 1
    end = fake.calls[0]["end_date"]
    assert before <= end <= after, "end_date must be ~now when as_of=None"


# --------------------------------------------------------------------------- #
# Step 2: _compute_factor / _gather thread as_of to every fetcher
# --------------------------------------------------------------------------- #

def test_gather_threads_as_of_to_all_fetchers(monkeypatch):
    """Gap #2: _gather threads as_of to value/quality/pead/prices fetchers."""
    captured = {}
    _patch_fetchers(monkeypatch, captured=captured)
    # enable all four factors so each fetcher is exercised
    settings = dict(SETTINGS_VALUE_ONLY)
    settings["_factor_weights"] = {"momentum": 1.0, "value": 1.0, "quality": 1.0, "pead": 1.0}
    e = _expert(settings=settings)
    e._gather(_provider_bundle(), as_of=NOW)
    assert captured["value_as_of"] == NOW
    assert captured["quality_as_of"] == NOW
    assert captured["pead_as_of"] == NOW
    assert captured["prices_as_of"] == NOW


def test_gather_skips_zero_weight_factor(monkeypatch):
    """A 0-weight factor's fetcher is NOT called in the factor loop (only enabled
    factors are fetched). value-only settings => quality/pead/momentum skipped."""
    captured = {}
    _patch_fetchers(monkeypatch, captured=captured)
    e = _expert()
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    assert set(bundle["factors"]) == {"value"}
    # quality/pead/momentum fetchers were never called in the factor loop
    assert "quality_as_of" not in captured
    assert "pead_as_of" not in captured
    # fetch_close_prices is STILL called once for the bundle "prices" entry
    assert captured["prices_as_of"] == NOW


# --------------------------------------------------------------------------- #
# Step 3: _process is pure -> target weights (top_n honored, sum ~ gross_exposure)
# --------------------------------------------------------------------------- #

def test_process_returns_top_n_weights_summing_to_gross(monkeypatch):
    _patch_fetchers(monkeypatch)
    e = _expert()
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    rec = e._process(bundle, SETTINGS_VALUE_ONLY, as_of=NOW)
    assert rec.signal == OrderRecommendation.OVERWEIGHT
    assert rec.skip is False
    assert rec.current_price is None  # basket-level: no single instrument price
    targets = rec.raw_outputs["targets"]
    # top_n=2 honored
    assert len(targets) == 2
    # value ranks AAA > BBB > CCC > DDD => top-2 are AAA, BBB
    assert set(targets) == {"AAA", "BBB"}
    # equal weighting, gross_exposure=1.0 => ~0.5 each, sum ~ gross
    assert sum(targets.values()) == pytest.approx(1.0, abs=1e-9)
    assert targets["AAA"] == pytest.approx(0.5, abs=1e-9)


def test_process_ranking_stable_and_book_carries_targets(monkeypatch):
    _patch_fetchers(monkeypatch)
    e = _expert()
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    rec = e._process(bundle, SETTINGS_VALUE_ONLY, as_of=NOW)
    book = rec.raw_outputs["book"]
    ranked_syms = [r["symbol"] for r in book["ranking"]]
    assert ranked_syms == ["AAA", "BBB", "CCC", "DDD"], "value ranking must be stable"
    assert book["targets"] == rec.raw_outputs["targets"]
    assert book["gross_exposure"] == 1.0
    assert rec.raw_outputs["type"] == "factor_ranking"
    assert rec.details == "Ranked 4 names, holding 2"


def test_process_gross_exposure_scales_weights(monkeypatch):
    """gross_exposure=0.6 => target weights sum to ~0.6 (not 1.0)."""
    _patch_fetchers(monkeypatch)
    settings = dict(SETTINGS_VALUE_ONLY)
    settings["gross_exposure"] = 0.6
    e = _expert(settings=settings)
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert sum(rec.raw_outputs["targets"].values()) == pytest.approx(0.6, abs=1e-9)


def test_process_book_action_reflects_holdings(monkeypatch):
    """The book's per-name action compares targets to the held set captured in
    _gather: BUY (new target), HOLD (kept target), SELL (dropped holding)."""
    _patch_fetchers(monkeypatch)
    # currently holding BBB (kept) and DDD (dropped — not in top-2)
    e = _expert(holdings=["BBB", "DDD"])
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    rec = e._process(bundle, SETTINGS_VALUE_ONLY, as_of=NOW)
    actions = {r["symbol"]: r["action"] for r in rec.raw_outputs["book"]["ranking"]}
    assert actions["AAA"] == "BUY"   # new target
    assert actions["BBB"] == "HOLD"  # kept target
    assert actions["DDD"] == "SELL"  # dropped holding
    assert actions["CCC"] == "—"     # ranked but neither targeted nor held


# --------------------------------------------------------------------------- #
# Step 3b: skip paths preserve the live _mark_skipped outcomes
# --------------------------------------------------------------------------- #

def test_process_empty_universe_skips(monkeypatch):
    _patch_fetchers(monkeypatch)
    e = _expert(universe=[])
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    rec = e._process(bundle, SETTINGS_VALUE_ONLY, as_of=NOW)
    assert rec.skip is True
    assert rec.skip_reason == "No candidate instruments configured"
    assert rec.signal == OrderRecommendation.HOLD


def test_process_no_enabled_factors_skips(monkeypatch):
    _patch_fetchers(monkeypatch)
    settings = dict(SETTINGS_VALUE_ONLY)
    settings["_factor_weights"] = {"momentum": 0.0, "value": 0.0, "quality": 0.0, "pead": 0.0}
    e = _expert(settings=settings)
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.skip is True
    assert rec.skip_reason == "No factors enabled (all weights are 0)"


# --------------------------------------------------------------------------- #
# Step 5 + golden contract: _process is pure; analyze_as_of == live logic
# --------------------------------------------------------------------------- #

def test_process_is_pure_no_provider_reads(monkeypatch):
    """_process must not touch providers/network — passing a bundle that was
    gathered once, _process on it twice yields identical targets."""
    _patch_fetchers(monkeypatch)
    e = _expert()
    bundle = e._gather(_provider_bundle(), as_of=NOW)
    r1 = e._process(bundle, SETTINGS_VALUE_ONLY, as_of=NOW)
    r2 = e._process(bundle, SETTINGS_VALUE_ONLY, as_of=NOW)
    assert r1.raw_outputs["targets"] == r2.raw_outputs["targets"]
    assert r1.details == r2.details


def test_analyze_as_of_equals_live_process(monkeypatch):
    """Golden contract: analyze_as_of(now) == _process(_gather(live, as_of=None))
    on (signal, targets, details) when the fetchers return the same fixtures.
    Proves the as_of=None live path is logic-identical to the as_of path."""
    _patch_fetchers(monkeypatch)
    e = _expert()

    ctx = BacktestContext(providers=_provider_bundle(),
                          settings=SETTINGS_VALUE_ONLY, as_of=NOW)
    rec_asof = e.analyze_as_of(NOW, ctx)

    # live path: _gather(as_of=None) + _process
    bundle_live = e._gather(_provider_bundle(), as_of=None)
    rec_live = e._process(bundle_live, SETTINGS_VALUE_ONLY, as_of=None)

    assert rec_asof.signal == rec_live.signal
    assert rec_asof.raw_outputs["targets"] == rec_live.raw_outputs["targets"]
    assert rec_asof.details == rec_live.details
    # almost_equals ignores raw_outputs/book (which carries a rebalanced_at timestamp)
    assert rec_asof.almost_equals(rec_live)


# --------------------------------------------------------------------------- #
# Piece 1a: BYPASS marker — FactorRanker declares it bypasses the classic RM
# --------------------------------------------------------------------------- #

def test_factor_ranker_declares_bypass_marker():
    """FactorRanker emits portfolio weight targets and rebalances via its own
    FactorPortfolioManager, so it must DECLARE that it bypasses the classic risk
    manager / shared enter-exit ruleset. The engine + optimizer branch on this
    flag (read via getattr(expert, 'bypasses_classic_rm', False))."""
    assert FactorRanker.bypasses_classic_rm is True
    # readable on an instance built via __new__ (no DB), as the engine reads it
    e = FactorRanker.__new__(FactorRanker)
    assert getattr(e, "bypasses_classic_rm", False) is True


def test_base_interface_bypass_default_is_false():
    """The base MarketExpertInterface default must be False so only weight-target
    experts opt in; every other expert keeps the classic RM pipeline."""
    from ba2_common.core.interfaces import MarketExpertInterface
    assert MarketExpertInterface.bypasses_classic_rm is False


def test_clean_expert_does_not_bypass():
    """A clean signal expert (FMPEarningsDrift) inherits the base default False —
    it does NOT bypass the classic risk manager."""
    from ba2_experts import FMPEarningsDrift
    assert getattr(FMPEarningsDrift, "bypasses_classic_rm", False) is False


def test_compute_factor_pead_window_from_settings(monkeypatch):
    """_compute_factor uses the pead_drift_window_days passed by _gather (from
    settings), not a self.get_setting read — proving the pure config path."""
    captured = {}
    window_seen = {}

    def fake_pead(symbols, as_of=None):
        return {s: {"actual": 1.0, "estimate": 0.0, "estimate_std": 0.5, "days_since": 5}
                for s in symbols}

    def fake_calc(inputs, drift_window_days=60):
        window_seen["window"] = drift_window_days
        return {s: 1.0 for s in inputs}

    monkeypatch.setattr(data, "fetch_pead_inputs", fake_pead)
    e = _expert()
    e._compute_factor("pead", "fetch_pead_inputs", fake_calc, ["AAA"],
                      as_of=NOW, pead_drift_window_days=90)
    assert window_seen["window"] == 90
