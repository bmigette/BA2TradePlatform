"""FactorRanker universe resolution: static (enabled_instruments) vs screener."""
from unittest.mock import MagicMock

# Note: `settings` is a read-only property, so tests inject via `_settings_cache`
# (which the property returns) rather than assigning `inst.settings`.
# The sub-package and the class share the name "FactorRanker", and experts/__init__
# binds the class over the submodule attribute — so import the module via importlib
# (sys.modules), giving the real module object whose StockScreener global is patchable.
import importlib

fr_mod = importlib.import_module("ba2_trade_platform.modules.experts.FactorRanker")


def test_screen_universe_returns_uppercased_symbols(monkeypatch):
    fake = MagicMock()
    fake.screen.return_value = {
        "results": [{"symbol": "aapl"}, {"symbol": "MSFT"}, {"nope": 1}],
        "stats": {},
    }
    monkeypatch.setattr(fr_mod, "StockScreener", lambda settings, **k: fake)
    inst = fr_mod.FactorRanker.__new__(fr_mod.FactorRanker)  # bypass __init__/DB
    inst.logger = MagicMock()
    inst._settings_cache = {"screener_market_cap_min": 1}
    syms = inst._screen_universe()
    assert syms == ["AAPL", "MSFT"]  # uppercased; dicts without 'symbol' skipped


def test_screen_universe_returns_empty_on_error(monkeypatch):
    def boom(settings, **k):
        raise RuntimeError("screener down")
    monkeypatch.setattr(fr_mod, "StockScreener", boom)
    inst = fr_mod.FactorRanker.__new__(fr_mod.FactorRanker)
    inst.logger = MagicMock()
    inst._settings_cache = {}
    assert inst._screen_universe() == []  # failures degrade to empty, not raise


def _bare_expert():
    inst = fr_mod.FactorRanker.__new__(fr_mod.FactorRanker)
    inst.logger = MagicMock()
    return inst


def _settings_stub(values):
    return MagicMock(side_effect=lambda key, **kw: values.get(key))


def test_resolve_universe_uses_screener_when_configured(monkeypatch):
    inst = _bare_expert()
    monkeypatch.setattr(inst, "_screen_universe", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(inst, "_get_enabled_instruments_config", lambda: {"ZZZ": {}})
    inst.get_setting_with_interface_default = _settings_stub(
        {"universe_source": "screener", "min_price": 0.0, "min_dollar_volume": 0.0}
    )
    assert inst._resolve_universe() == ["AAA", "BBB"]


def test_resolve_universe_uses_static_by_default(monkeypatch):
    inst = _bare_expert()
    monkeypatch.setattr(inst, "_screen_universe", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(inst, "_get_enabled_instruments_config", lambda: {"ZZZ": {}, "YYY": {}})
    inst.get_setting_with_interface_default = _settings_stub(
        {"universe_source": "static", "min_price": 0.0, "min_dollar_volume": 0.0}
    )
    assert sorted(inst._resolve_universe()) == ["YYY", "ZZZ"]
