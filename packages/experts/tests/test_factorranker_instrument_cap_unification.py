"""FactorRanker's per-name cap unification (2026-09-29): ``max_weight_per_name``
(fraction 0-1, FactorRanker-only, retired) -> ``max_virtual_equity_per_instrument_percent``
(percent, the platform-wide per-instrument cap every expert shares).

Pins:
  1. ``_refuse_legacy_max_weight_per_name`` raises loudly whenever the retired setting is
     present, naming the replacement and the conversion; no-ops otherwise.
  2. ``_validate_instrument_cap_percent`` accepts (0, 100] and refuses everything else
     (missing, zero, negative, >100), naming the setting.
  3. The LIVE settings-resolution path (``_resolve_factor_settings``, reading
     ``self.settings``) refuses a stored legacy row, resolves a stored cap, and falls back
     to the class's declared 10% default when nothing is stored.
  4. The BACKTEST path (``analyze_as_of``, reading ``context.settings``) refuses a legacy
     key riding along on the trial's settings, and falls back to the same declared default
     when the engine did not pass the cap.
  5. ``_process`` actually enforces the resolved cap end to end: 5.0 -> weights capped at
     0.05, 20.0 -> weights capped at 0.20 (the gene-plumbing proof at the pure-function
     level; the full GA-gene-to-trial-book proof lives in
     testplatform/backend/tests/backtest/test_factorranker_instrument_cap_gene.py).
"""
import logging

import pytest

from ba2_experts.FactorRanker import (
    FactorRanker, _refuse_legacy_max_weight_per_name, _validate_instrument_cap_percent,
)
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle


# --------------------------------------------------------------------------- #
# 1. _refuse_legacy_max_weight_per_name
# --------------------------------------------------------------------------- #

def test_refuse_legacy_raises_naming_both_settings_and_the_conversion():
    with pytest.raises(ValueError) as exc:
        _refuse_legacy_max_weight_per_name({"max_weight_per_name": 0.15})
    msg = str(exc.value)
    assert "max_weight_per_name" in msg
    assert "max_virtual_equity_per_instrument_percent" in msg
    assert "15" in msg  # 0.15 * 100


def test_refuse_legacy_is_a_noop_when_absent():
    _refuse_legacy_max_weight_per_name({})
    _refuse_legacy_max_weight_per_name({"max_virtual_equity_per_instrument_percent": 10.0})


def test_refuse_legacy_is_a_noop_for_none_value():
    """A settings dict that carries the key with value None (e.g. a definitions-seeded
    dict with no DB row) is NOT a stored legacy value."""
    _refuse_legacy_max_weight_per_name({"max_weight_per_name": None})


# --------------------------------------------------------------------------- #
# 2. _validate_instrument_cap_percent
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected_fraction", [
    (10.0, 0.10), (100.0, 1.0), (0.01, 0.0001), (5.0, 0.05), (20.0, 0.20),
])
def test_validate_instrument_cap_percent_accepts_the_valid_range(value, expected_fraction):
    assert _validate_instrument_cap_percent(value) == pytest.approx(expected_fraction)


@pytest.mark.parametrize("bad", [None, 0, 0.0, -5.0, 100.0001, 150.0])
def test_validate_instrument_cap_percent_refuses_everything_else(bad):
    with pytest.raises(ValueError, match="max_virtual_equity_per_instrument_percent"):
        _validate_instrument_cap_percent(bad)


# --------------------------------------------------------------------------- #
# Shared harness: a FactorRanker built via __new__ (bypasses __init__'s DB read).
# settings/_settings_cache is the ExtendableSettingsInterface property's own cache
# slot, so setting it directly simulates "these are the stored DB rows" without
# touching a real database.
# --------------------------------------------------------------------------- #

def _expert(settings_cache=None):
    e = FactorRanker.__new__(FactorRanker)
    e.id = 1
    e.logger = logging.getLogger("test.FactorRanker.cap")
    e._settings_cache = dict(settings_cache) if settings_cache is not None else {}
    e._factor_weights = lambda: {"momentum": 1.0, "value": 0.0, "quality": 0.0, "pead": 0.0}
    return e


_DECLARED_DEFAULT_PCT = (
    FactorRanker.get_merged_settings_definitions()
    ["max_virtual_equity_per_instrument_percent"]["default"]
)


def test_declared_default_is_ten_percent():
    """Pins the number the fallback tests below assert against."""
    assert _DECLARED_DEFAULT_PCT == 10.0


# --------------------------------------------------------------------------- #
# 3. LIVE path: _resolve_factor_settings
# --------------------------------------------------------------------------- #

def test_resolve_factor_settings_refuses_a_stored_legacy_row():
    e = _expert({"max_weight_per_name": 0.15})
    with pytest.raises(ValueError, match="max_weight_per_name"):
        e._resolve_factor_settings()


def test_resolve_factor_settings_falls_back_to_the_declared_default():
    e = _expert({})  # nothing stored at all
    settings = e._resolve_factor_settings()
    assert settings["max_virtual_equity_per_instrument_percent"] == _DECLARED_DEFAULT_PCT


def test_resolve_factor_settings_uses_a_stored_value_over_the_default():
    e = _expert({"max_virtual_equity_per_instrument_percent": 15.0})
    settings = e._resolve_factor_settings()
    assert settings["max_virtual_equity_per_instrument_percent"] == 15.0


# --------------------------------------------------------------------------- #
# 4. BACKTEST path: analyze_as_of
# --------------------------------------------------------------------------- #

UNIVERSE = ["AAA", "BBB", "CCC", "DDD"]

VALUE_INPUTS = {
    "AAA": {"eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "BBB": {"eps_ttm": 7.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "CCC": {"eps_ttm": 4.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "DDD": {"eps_ttm": 1.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
}

_BASE_CTX_SETTINGS = {
    "winsorize_pct": 0.0,
    "top_n": 4,
    "weighting": "equal",
    "gross_exposure": 1.0,
    "pead_drift_window_days": 60,
    "factor_weight_momentum": 0.0,
    "factor_weight_value": 1.0,
    "factor_weight_quality": 0.0,
    "factor_weight_pead": 0.0,
}


def _backtest_expert(monkeypatch):
    """A FactorRanker whose _gather/universe/data are hermetic, and whose OWN settings
    (the live-path fallback ``get_setting_with_interface_default`` reads) are empty --
    ``_settings_cache = {}`` resolves every unset key to its declared default, exactly
    like a fresh, never-configured instance."""
    from ba2_experts.FactorRanker import data as fr_data

    e = FactorRanker.__new__(FactorRanker)
    e.id = 1
    e.logger = logging.getLogger("test.FactorRanker.cap.backtest")
    e._settings_cache = {}
    e._resolve_universe = lambda *a, **k: list(UNIVERSE)
    e._gather_holdings = lambda: []

    def fake_value(symbols, as_of=None):
        return {s: VALUE_INPUTS[s] for s in symbols if s in VALUE_INPUTS}

    monkeypatch.setattr(fr_data, "fetch_value_inputs", fake_value)
    return e


def _ctx(settings):
    return BacktestContext(
        providers=LiveProviderBundle(lambda cat, name, **kw: None),
        settings=settings, as_of=None)


def test_analyze_as_of_refuses_a_legacy_key_riding_on_context_settings(monkeypatch):
    """An old genome/deploy payload decoded verbatim would carry model:max_weight_per_name
    straight onto context.settings; it must refuse, not silently size against it."""
    e = _backtest_expert(monkeypatch)
    settings = dict(_BASE_CTX_SETTINGS, max_weight_per_name=0.15)
    with pytest.raises(ValueError, match="max_weight_per_name"):
        e.analyze_as_of(None, _ctx(settings))


def test_analyze_as_of_falls_back_to_the_declared_default_when_absent(monkeypatch):
    e = _backtest_expert(monkeypatch)
    rec = e.analyze_as_of(None, _ctx(dict(_BASE_CTX_SETTINGS)))
    assert not rec.skip
    # 4 equal-weighted picks want 25% each; capped at the declared 10% default ->
    # every held name sits at exactly 0.10, well short of full deployment.
    targets = rec.raw_outputs["targets"]
    assert targets, targets
    assert all(w == pytest.approx(0.10) for w in targets.values()), targets


def test_analyze_as_of_honours_the_genes_cap_value(monkeypatch):
    """The engine/optimizer-resolved value on context.settings wins over the declared
    default -- this is the gene-plumbing seam a GA trial actually uses."""
    e = _backtest_expert(monkeypatch)
    settings = dict(_BASE_CTX_SETTINGS, max_virtual_equity_per_instrument_percent=20.0)
    rec = e.analyze_as_of(None, _ctx(settings))
    targets = rec.raw_outputs["targets"]
    assert targets
    assert all(w == pytest.approx(0.20) for w in targets.values()), targets


# --------------------------------------------------------------------------- #
# 5. _process enforces the resolved cap: 5% and 20%, at the pure-function level
# --------------------------------------------------------------------------- #

def _process_with_cap(monkeypatch, cap_percent: float):
    e = _backtest_expert(monkeypatch)
    settings = dict(_BASE_CTX_SETTINGS, max_virtual_equity_per_instrument_percent=cap_percent)
    settings["_factor_weights"] = {
        "momentum": 0.0, "value": 1.0, "quality": 0.0, "pead": 0.0}
    e._gather_settings = settings
    bundle = e._gather(LiveProviderBundle(lambda cat, name, **kw: None), as_of=None)
    return e._process(bundle, settings, as_of=None)


def test_process_caps_weights_at_5_percent(monkeypatch):
    rec = _process_with_cap(monkeypatch, 5.0)
    targets = rec.raw_outputs["targets"]
    assert targets
    assert all(w == pytest.approx(0.05) for w in targets.values()), targets


def test_process_caps_weights_at_20_percent(monkeypatch):
    rec = _process_with_cap(monkeypatch, 20.0)
    targets = rec.raw_outputs["targets"]
    assert targets
    assert all(w == pytest.approx(0.20) for w in targets.values()), targets


def test_process_refuses_an_out_of_range_cap():
    e = _expert({})
    with pytest.raises(ValueError, match="max_virtual_equity_per_instrument_percent"):
        e._process(
            {"universe": ["AAA"], "factors": {"value": {"AAA": 1.0}},
             "holdings": [], "current_price": None},
            {"_factor_weights": {"momentum": 0.0, "value": 1.0, "quality": 0.0, "pead": 0.0},
             "winsorize_pct": 0.0, "gross_exposure": 1.0, "top_n": 1, "weighting": "equal",
             "max_virtual_equity_per_instrument_percent": 0.0},
        )
