"""``use_atr_stop`` and ``regime_overlay_enabled`` must stay OFF, and stay out of the search.

WHY THIS EXISTS. Both settings are bool-declared, and the GA hands genes over as integers.
``save_settings`` stored a bool as ``json.dumps(value)``, so ON was written as the JSON string
``"1"`` -- which the reader tested against ``'true'`` and read as False. Every run on record
therefore executed with the ATR stop-leg disabled and the regime overlay off, whatever its
genome claimed.

Fixing that encoding (``coerce_bool``) means the genes would suddenly START working. That is
correct in the long run and wrong as a side effect: it would silently convert the grid into a
different experiment and make every new result incomparable with every result on record. So the
two toggles are pinned OFF, and removed from the search space.

THE TRAP THIS PINS. ``use_atr_stop`` DECLARES ``default: True``. Dropping it from the search
without pinning the value would ENABLE it -- the exact inverse of the behaviour being preserved.
``regime_overlay_enabled`` declares False, so it would survive either way; the asymmetry is
precisely why the pin is explicit for both rather than relying on the defaults.
"""
import pytest

# ``ba2test_launcher`` is importable because tests/backtest/conftest.py puts ``testplatform/``
# on sys.path. It is NOT installed: a bare import here resolved only through a dev box's
# editable install of ba2test_app, and raised ModuleNotFoundError on CI -- a collection error,
# which fails the entire tests/backtest step before any test runs.
import ba2test_launcher as L


class TestTheTogglesArePinnedOff:
    def test_both_are_declared_inert(self):
        assert L._INERT_RM_TOGGLES == {"use_atr_stop": False, "regime_overlay_enabled": False}

    def test_a_run_gets_them_off(self):
        settings = L._expert_run_settings({"fixed_settings": {"sizing_mode": "risk_atr"}}, [])
        assert settings["use_atr_stop"] is False
        assert settings["regime_overlay_enabled"] is False

    def test_the_pin_survives_alongside_the_specs_own_fixed_settings(self):
        settings = L._expert_run_settings(
            {"fixed_settings": {"sizing_mode": "risk_atr", "min_trader_hold_roundtrips": 3}}, [])
        assert settings["sizing_mode"] == "risk_atr"
        assert settings["min_trader_hold_roundtrips"] == 3
        assert settings["use_atr_stop"] is False

    def test_use_atr_stop_is_pinned_against_its_own_declared_default(self):
        """THE TRAP: the interface declares default True, so 'not searched' is not 'off'."""
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

        MarketExpertInterface._ensure_builtin_settings()
        declared = MarketExpertInterface._builtin_settings["use_atr_stop"]
        assert declared["default"] is True, "if this ever flips, revisit the pin's rationale"
        assert L._expert_run_settings({"fixed_settings": {}}, [])["use_atr_stop"] is False


class TestTheyAreNoLongerSearched:
    """A gene left in the space decodes into expert_overrides, which are merged after the run
    settings -- so it would overwrite the pin. Removing it from the space is half the fix."""

    @pytest.mark.parametrize("gene", ["use_atr_stop", "regime_overlay_enabled"])
    def test_the_gene_is_not_optimized(self, gene):
        assert L._RM_OPT[gene]["optimize"] is False

    @pytest.mark.parametrize("gene", ["use_atr_stop", "regime_overlay_enabled"])
    def test_the_gene_is_absent_from_the_collected_param_space(self, gene):
        from app.services.strategy_param_space import collect_param_space

        space = collect_param_space(None, expert_cfg=L._rm_opt_for("S1"))
        assert f"model:{gene}" not in space

    def test_the_still_live_rm_genes_are_untouched(self):
        """The inverse -- pinning two toggles must not quietly shrink the rest of the search."""
        from app.services.strategy_param_space import collect_param_space

        space = collect_param_space(None, expert_cfg=L._rm_opt_for("S1"))
        for gene in ("risk_per_trade_pct", "atr_multiplier", "atr_period",
                     "min_stop_loss_pct", "max_virtual_equity_per_instrument_percent"):
            assert f"model:{gene}" in space

    def test_the_regime_scales_keep_their_historical_shape(self):
        """They are inert with the overlay off -- exactly as in every run on record -- and are
        deliberately LEFT in the space so the genome shape does not change underneath a
        comparison."""
        from app.services.strategy_param_space import collect_param_space

        space = collect_param_space(None, expert_cfg=L._rm_opt_for("S1"))
        for gene in ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale"):
            assert f"model:{gene}" in space


def test_an_explicit_override_can_still_turn_them_on():
    """The pin is a floor, not a wall: a deliberate experiment passes them through overrides.
    If this stopped working there would be no way to ever test the features."""
    settings = L._expert_run_settings(
        {"fixed_settings": {"sizing_mode": "risk_atr"}}, [],
        overrides={"use_atr_stop": True, "regime_overlay_enabled": True})
    assert settings["use_atr_stop"] is True
    assert settings["regime_overlay_enabled"] is True


class TestTheStoredGenomePathIsPinnedToo:
    """The half that the search-space removal does NOT cover.

    A re-run, a warm start and a deploy all decode the genome ALREADY ON DISK, and
    `_build_daily_trial_config` merges those decoded `expert_overrides` OVER the run-level
    settings. Four of the six live deployed genomes carry `model:use_atr_stop: 1`, so without a
    pin above the overrides, re-running a saved backtest would run a DIFFERENT strategy from the
    one whose results are recorded on the row -- the exact thing a re-run exists to check.
    """

    def test_the_two_constants_are_the_same_pin(self):
        """One is applied to run-level settings (launcher), the other above the decoded genes
        (trial config). They are hand-kept mirrors, so pin them equal."""
        from app.services.strategy_param_space import INERT_RM_TOGGLES

        assert INERT_RM_TOGGLES == L._INERT_RM_TOGGLES

    def test_a_decoded_gene_saying_ON_does_not_win(self):
        from app.services.strategy_optimization_handler import _build_daily_trial_config

        backtest_cfg = {
            "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
            "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0,
            "seed": 42, "account_settings": {},
            "experts": [{"class": "FMPRating", "settings": {"sizing_mode": "risk_atr"}}],
        }
        decoded = {"expert_overrides": {"use_atr_stop": 1, "regime_overlay_enabled": 1,
                                        "risk_per_trade_pct": 2.5},
                   "screener_overrides": {}, "schedule_days": None,
                   "entry_rules": None, "exit_rules": None}
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None)
        settings = cfg["experts"][0]["settings"]
        assert settings["use_atr_stop"] is False
        assert settings["regime_overlay_enabled"] is False
        # The inverse: an ordinary gene must still win over the run-level settings.
        assert settings["risk_per_trade_pct"] == 2.5
