"""ONE table of the screener settings that decide a selection; no key is left to StockScreener's defaults.

A live instance lacking a key runs it on ``StockScreener._DEFAULTS`` (price_min 20, volume_min 500,000, float_min 10M,
max_stocks 10, price_drop_pct 15, ...).  The simulation REFUSES a missing key; the launcher writes every key; the deploy
writes every key."""
from __future__ import annotations

import pytest

from ba2_common.core.deploy_parity import (SCREENER_OFF_VALUES, SCREENER_SELECTION_KEYS, complete_screener_settings,
                                           missing_screener_keys)
from ba2_providers.StockScreener import StockScreener
from ba2_providers.screener import live_sim as ls


def _interface_defs():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    MarketExpertInterface._ensure_builtin_settings()
    return MarketExpertInterface._builtin_settings


def test_the_key_table_is_live_s_table_and_the_interface_s_table():
    live = {k[len("screener_"):]: v for k, v in StockScreener._DEFAULTS.items() if k.startswith("screener_")}
    for k in ("provider",):
        live.pop(k)
    assert set(SCREENER_SELECTION_KEYS) == set(live), set(live) ^ set(SCREENER_SELECTION_KEYS)
    defs = _interface_defs()
    for k in SCREENER_SELECTION_KEYS:
        d = defs[f"screener_{k}"]["default"]
        assert d == live[k] or (isinstance(d, bool) and bool(live[k]) == d), (k, d, live[k])


def test_every_key_resolves_the_same_value_in_live_and_in_the_simulation():
    """For a complete settings dict, live's StockScreener and the simulation read the SAME value for every key."""
    vals = {"market_cap_min": 5e9, "market_cap_max": 1e10, "price_min": 7.0, "price_max": 90.0, "volume_min": 123456,
            "volume_max": 9e6, "float_min": 2e7, "float_max": 3e8, "relative_volume_min": 1.7, "price_drop_pct": 11.0,
            "price_drop_days": 9, "max_stocks": 33, "sort_metric": "market_cap", "weinstein_stage2_only": 1}
    assert set(vals) == set(SCREENER_SELECTION_KEYS)
    live = StockScreener({f"screener_{k}": v for k, v in vals.items()})._settings
    for k, v in vals.items():
        assert live[f"screener_{k}"] == v or float(live[f"screener_{k}"]) == float(v), k
        assert ls._fnum(vals, k) == float(v) if k != "sort_metric" else True


def test_a_missing_key_is_refused_by_the_simulation_naming_it():
    full = {k: 1 for k in SCREENER_SELECTION_KEYS}
    ls.require_complete_settings(full)
    for k in SCREENER_SELECTION_KEYS:
        bad = {x: v for x, v in full.items() if x != k}
        with pytest.raises(ls.SimulationRefusal, match=k):
            ls.require_complete_settings(bad)
        assert missing_screener_keys(bad) == [k]


def test_the_deploy_writes_every_key_and_never_invents_a_gene():
    got = complete_screener_settings({"screener_market_cap_min": 5e9, "screener_max_stocks": 20,
                                      "screener_relative_volume_min": 1.0, "screener_price_drop_pct": 10.0,
                                      "screener_price_drop_days": 5, "screener_weinstein_stage2_only": 0})
    assert set(got) == {f"screener_{k}" for k in SCREENER_SELECTION_KEYS}
    for k, v in SCREENER_OFF_VALUES.items():
        assert got[f"screener_{k}"] == v          # the bounds the backtest never stated are written as OFF, explicitly
    assert got["screener_price_min"] == 0 and got["screener_volume_min"] == 0 and got["screener_float_min"] == 0
    # an explicit value wins
    assert complete_screener_settings({**{f"screener_{k}": 1 for k in SCREENER_SELECTION_KEYS},
                                       "screener_price_min": 25.0})["screener_price_min"] == 25.0
    with pytest.raises(ValueError, match="max_stocks"):
        complete_screener_settings({"screener_market_cap_min": 5e9})


def test_the_launchers_builtin_base_states_the_floors_off():
    """The explicit base of every --screener job: price_min, volume_min, float_min ... = 0 (the enabled prod instances
    carry explicit zeros), so a genome is backtested AND deployed with the floors off."""
    assert SCREENER_OFF_VALUES["price_min"] == 0 and SCREENER_OFF_VALUES["volume_min"] == 0
    assert SCREENER_OFF_VALUES["float_min"] == 0 and SCREENER_OFF_VALUES["volume_max"] == 0
