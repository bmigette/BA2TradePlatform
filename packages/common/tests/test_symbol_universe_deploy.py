"""A restricted option job's candidate list travels from the run config to the live setting ``screener_symbol_universe``."""
from ba2_common.core.deploy_parity import live_settings_from_universe, unmapped_screener_keys


def test_the_universe_block_maps_the_restriction_to_the_live_setting_without_a_warning():
    universe = {"mode": "screener", "screener_settings": {
        "market_cap_min": 1e10, "max_stocks": 20, "screener_symbol_universe": "AAA,BBB,CCC"}}
    out = live_settings_from_universe(universe)
    assert out["screener_symbol_universe"] == "AAA,BBB,CCC"
    assert out["screener_market_cap_min"] == 1e10 and out["instrument_selection_method"] == "screener"
    assert unmapped_screener_keys(universe) == []
