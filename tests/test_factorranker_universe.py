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
    # _resolve_universe_source() reads self.settings (the real cached-settings property,
    # NOT get_setting_with_interface_default) FIRST to check for an explicit
    # instrument_selection_method override — an empty cache means "unset", so it falls
    # through to the universe_source stub below (see that method's docstring).
    inst._settings_cache = {}
    monkeypatch.setattr(inst, "_screen_universe", lambda as_of=None: ["AAA", "BBB"])
    monkeypatch.setattr(inst, "_get_enabled_instruments_config", lambda: {"ZZZ": {}})
    inst.get_setting_with_interface_default = _settings_stub(
        {"universe_source": "screener", "min_price": 0.0, "min_dollar_volume": 0.0}
    )
    assert inst._resolve_universe() == ["AAA", "BBB"]


def test_resolve_universe_uses_static_by_default(monkeypatch):
    inst = _bare_expert()
    inst._settings_cache = {}
    monkeypatch.setattr(inst, "_screen_universe", lambda as_of=None: ["AAA", "BBB"])
    monkeypatch.setattr(inst, "_get_enabled_instruments_config", lambda: {"ZZZ": {}, "YYY": {}})
    inst.get_setting_with_interface_default = _settings_stub(
        {"universe_source": "static", "min_price": 0.0, "min_dollar_volume": 0.0}
    )
    assert sorted(inst._resolve_universe()) == ["YYY", "ZZZ"]


# ---------------------------------------------------------------------------
# The metric_store is a point-in-time RESEARCH artifact: it is a prebuilt weekly
# snapshot, so resolving "today" against it returns the newest scan date <= today.
# On 2026-07-27 that was 2026-06-27 -- a 31-day-stale universe driving live orders.
# Live (as_of is None) must therefore always screen with the real StockScreener.
# The precomputed-momentum path already guards this way ("if ... as_of is None:
# return None, None"); the universe path did not, which is the inconsistency here.
# ---------------------------------------------------------------------------

def _store_expert(monkeypatch, store=r"C:\some\metric_store"):
    inst = _bare_expert()
    inst._settings_cache = {}
    inst.get_setting_with_interface_default = _settings_stub({"screener_store": store})
    return inst


def _record_store(monkeypatch, symbols=("msft",)):
    """Record store access. Must NOT raise: _screen_universe catches broad Exception and
    silently degrades to StockScreener, so an exception-based probe would make a
    'live skipped the store' assertion pass even when the store WAS read."""
    from ba2_providers.screener import metric_store as ms
    calls = []
    monkeypatch.setattr(ms, "load_store", lambda store: calls.append(store) or "DF")
    monkeypatch.setattr(ms, "screen_universe_as_of",
                        lambda df, day, settings: list(symbols))
    return calls


def test_live_never_reads_the_metric_store(monkeypatch):
    """as_of=None (live) must bypass the store even when screener_store is set."""
    store_calls = _record_store(monkeypatch)
    fake = MagicMock()
    fake.screen.return_value = {"results": [{"symbol": "aapl"}], "stats": {}}
    monkeypatch.setattr(fr_mod, "StockScreener", lambda settings, **k: fake)

    inst = _store_expert(monkeypatch)
    got = inst._screen_universe()

    assert store_calls == [], (
        f"live resolved its universe from the metric_store ({store_calls}) -- that snapshot "
        f"is a weekly prebuilt artifact and was 31 days stale on 2026-07-27"
    )
    assert fake.screen.called, "live must fall through to the real StockScreener"
    assert got == ["AAPL"]


def test_backtest_still_uses_the_metric_store(monkeypatch):
    """as_of set (backtest) keeps the fast precomputed path -- speed/fidelity preserved."""
    from datetime import datetime, timezone

    store_calls = _record_store(monkeypatch, symbols=("msft",))
    screener_calls = []
    monkeypatch.setattr(fr_mod, "StockScreener",
                        lambda *a, **k: screener_calls.append(1) or MagicMock())

    inst = _store_expert(monkeypatch)
    got = inst._screen_universe(as_of=datetime(2025, 1, 2, tzinfo=timezone.utc))

    assert store_calls, "backtest must still use the fast metric_store path"
    assert screener_calls == [], "backtest must not fall back to the slow StockScreener"
    assert got == ["MSFT"]
