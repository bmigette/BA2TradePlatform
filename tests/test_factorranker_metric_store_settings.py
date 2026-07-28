"""FactorRanker must not silently drop screener filters the metric_store supports.

``_metric_store_settings`` hand-rolled its own screener_* -> unprefixed mapping and omitted
``price_drop_days`` and ``float_min``/``float_max``, on the (stale) belief that the store did
not support them. It does: ``_METRIC_STORE_KEYS`` lists all three, ``screen_universe_for_day``
selects the precomputed ``price_drop_pct_<Y>`` column from ``price_drop_days`` (the live store
carries price_drop_pct_2..30), and gates float via ``float_shares``.

Consequence found live on 2026-07-27: instances 26 and 27 differ on
``screener_price_drop_days`` (3 vs 2), that difference was dropped before the screen, and the
two instances resolved the SAME universe -> byte-identical portfolios -> double-sized positions
on a shared broker account.
"""

import importlib

from ba2_providers.screener.metric_store import (
    _METRIC_STORE_KEYS,
    normalize_screener_settings,
)

fr_mod = importlib.import_module("ba2_trade_platform.modules.experts.FactorRanker")


def _expert_with(values):
    """FactorRanker whose get_setting_with_interface_default reads from ``values``."""
    inst = fr_mod.FactorRanker.__new__(fr_mod.FactorRanker)  # bypass __init__/DB
    inst.get_setting_with_interface_default = lambda key: values.get(key)
    return inst


def test_price_drop_days_reaches_the_metric_store():
    inst = _expert_with({
        "screener_market_cap_min": 6_000_000_000,
        "screener_price_drop_pct": 10.0,
        "screener_price_drop_days": 3,
        "screener_max_stocks": 30,
    })
    out = inst._metric_store_settings()
    assert out.get("price_drop_days") == 3, (
        "price_drop_days was dropped -- the store would fall back to the legacy "
        "single-window price_drop_pct column and two differently-configured instances "
        "collapse onto the same universe"
    )


def test_float_bounds_reach_the_metric_store():
    inst = _expert_with({
        "screener_float_min": 5_000_000,
        "screener_float_max": 900_000_000,
    })
    out = inst._metric_store_settings()
    assert out.get("float_min") == 5_000_000
    assert out.get("float_max") == 900_000_000


def test_mapping_covers_every_key_the_store_recognises():
    """Guard against future drift between the store's key set and this translator."""
    values = {f"screener_{k}": 1 for k in _METRIC_STORE_KEYS}
    out = _expert_with(values)._metric_store_settings()
    missing = sorted(set(_METRIC_STORE_KEYS) - set(out))
    assert not missing, f"metric_store keys never passed by FactorRanker: {missing}"


def test_unset_settings_are_omitted_not_passed_as_none():
    """None values must be stripped -- the store treats a present key as a real filter."""
    out = _expert_with({"screener_market_cap_min": 6_000_000_000})._metric_store_settings()
    assert out == {"market_cap_min": 6_000_000_000}
    assert all(v is not None for v in out.values())


def test_output_is_exactly_what_the_shared_normalizer_produces():
    """The translator must agree with metric_store's own canonical normalizer."""
    values = {
        "screener_market_cap_min": 9_000_000_000,
        "screener_price_drop_days": 2,
        "screener_max_stocks": 30,
        "screener_sort_metric": "market_cap",
    }
    out = _expert_with(values)._metric_store_settings()
    assert out == normalize_screener_settings(values)
