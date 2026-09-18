"""The UI's expert import must apply the payload's UNIVERSE, not just settings.expert_params.

`_derive_export_payload` carries the screened universe in its own top-level `universe` block.
An import that reads only `settings.expert_params` deploys the genome onto whatever universe
the live instance happened to have -- the defect fixed in tools/import_deploy_payload.py at the
2026-09-07 parity review and, until now, never here.

Measured on bt1298 before the fix, this path dropped EIGHT settings:
    instrument_selection_method=screener, screener_market_cap_min/max, screener_max_stocks,
    screener_price_drop_days/pct, screener_relative_volume_min, screener_weinstein_stage2_only
The first is the one that bites hardest: without it the expert does not screen at all.

These pin the shared helper's contract at the seam the UI depends on -- the UI handler itself
is a NiceGUI upload callback with no seam to call it through, so what is pinned here is that
the mapping the handler now applies produces the settings a deploy needs, including the
unprefixed-name canonicalisation whose live default (0 = no ceiling) silently removed the top
of prod's cap bands.
"""
import pytest

from ba2_common.core.deploy_parity import (
    live_settings_from_universe, unmapped_screener_keys,
)

#: The shape _derive_export_payload emits for a screener run (bt1298's real values).
BT1298_UNIVERSE = {
    "mode": "screener",
    "screener_settings": {
        "screener_market_cap_min": 5_000_000_000.0,
        "market_cap_max": 10_000_000_000.0,          # the metric store's UNPREFIXED spelling
        "screener_max_stocks": 40,
        "screener_price_drop_days": 18,
        "screener_price_drop_pct": 16.0,
        "screener_relative_volume_min": 0.6,
        "screener_weinstein_stage2_only": 0,
    },
}


class TestWhatTheImportNowApplies:
    def test_the_selection_method_is_switched_to_screener(self):
        """THE ONE THAT BITES: without it the expert trades its static instrument list and the
        whole screened universe is inert, silently."""
        assert live_settings_from_universe(BT1298_UNIVERSE)["instrument_selection_method"] \
            == "screener"

    def test_the_cap_band_arrives_with_BOTH_bounds(self):
        """market_cap_max's live default is 0 = NO CEILING, which is how prod instances 8-12
        were deployed with the top of their band missing."""
        out = live_settings_from_universe(BT1298_UNIVERSE)
        assert out["screener_market_cap_min"] == 5_000_000_000.0
        assert out["screener_market_cap_max"] == 10_000_000_000.0, \
            "the unprefixed name must be canonicalised, or nothing reads it"
        assert "market_cap_max" not in out, "the un-prefixed key must not be carried through"

    def test_every_screener_gene_travels(self):
        out = live_settings_from_universe(BT1298_UNIVERSE)
        assert out["screener_max_stocks"] == 40
        assert out["screener_price_drop_days"] == 18
        assert out["screener_price_drop_pct"] == 16.0
        assert out["screener_relative_volume_min"] == 0.6
        assert out["screener_weinstein_stage2_only"] == 0
        assert len(out) == 8, f"expected the 7 screener genes + the selection method, got {out}"

    def test_the_universe_wins_over_expert_params(self):
        """The universe is part of WHAT WAS SCORED, so the handler merges it LAST -- same order
        as import_deploy_payload.py."""
        expert_params = {"screener_max_stocks": 999, "theta_buy": 0.25}
        merged = {**expert_params, **live_settings_from_universe(BT1298_UNIVERSE)}
        assert merged["screener_max_stocks"] == 40
        assert merged["theta_buy"] == 0.25, "unrelated tuned params survive the merge"


class TestWhatItRefusesToGuess:
    def test_a_static_universe_maps_to_nothing(self):
        """A static run's symbols are its candidate list, not a setting -- overwriting a live
        instance's enabled_instruments from a backtest is a different decision entirely."""
        assert live_settings_from_universe(
            {"mode": "static", "symbols": ["AAPL", "MSFT"]}) == {}

    @pytest.mark.parametrize("universe", [None, {}, {"mode": None}, "not-a-dict", 42])
    def test_a_missing_or_malformed_universe_is_a_no_op(self, universe):
        assert live_settings_from_universe(universe) == {}
        assert unmapped_screener_keys(universe) == []

    def test_a_key_with_no_live_setting_is_CARRIED_but_reported(self):
        """The contract is "carried and announced", not "filtered".

        A key that is neither a live screener setting nor one prefix away lands in expertsetting
        under a name nothing reads -- harmless in itself, and exactly what market_cap_max did.
        Dropping it silently and carrying it silently are equally bad; the helper carries it (so
        the file is not quietly edited) and the importer SAYS so. The UI now notifies, matching
        import_deploy_payload.py.
        """
        universe = {"mode": "screener",
                    "screener_settings": {"screener_max_stocks": 40,
                                          "some_backtest_only_knob": 7}}
        assert unmapped_screener_keys(universe) == ["some_backtest_only_knob"]
        assert live_settings_from_universe(universe)["some_backtest_only_knob"] == 7

    def test_a_clean_universe_reports_no_strays(self):
        assert unmapped_screener_keys(BT1298_UNIVERSE) == []


def test_the_handler_calls_the_shared_helper():
    """Guards against a second implementation drifting from the tool's: the UI import must go
    through deploy_parity, which is where the canonicalisation and the stray check live."""
    import inspect
    from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab
    src = inspect.getsource(ExpertSettingsTab._render_import_export_tab)
    assert "live_settings_from_universe" in src
    assert "unmapped_screener_keys" in src
