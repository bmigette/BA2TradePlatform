"""FactorRanker fast screener path: when ``screener_store`` is set, ``_screen_universe``
resolves the candidate universe from the fast ``ba2_providers.screener.metric_store``
(opt-in) and must NOT call the slow ``StockScreener``; when unset it falls back to
``StockScreener`` (live default). Also verifies the ``screener_*`` -> unprefixed
translator (passing the raw prefixed keys would match nothing in the store).
"""
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from ba2_experts.FactorRanker import FactorRanker

# The package name (ba2_experts.FactorRanker) collides with the class name, so
# `import ba2_experts.FactorRanker as m` yields the CLASS; grab the real module from
# sys.modules to monkeypatch its module-global StockScreener.
FRmod = sys.modules["ba2_experts.FactorRanker"]

AS_OF = datetime(2024, 1, 8, tzinfo=timezone.utc)


def _fake_self(settings):
    """Duck-typed self: _screen_universe / _metric_store_settings only read
    get_setting_with_interface_default + self.settings + self.logger. We bind the
    real ``_metric_store_settings`` so the fast path exercises the actual translator
    (not a stub) — _screen_universe calls ``self._metric_store_settings()``."""
    me = SimpleNamespace(
        get_setting_with_interface_default=lambda k: settings.get(k),
        settings=settings,
        logger=logging.getLogger("t"),
    )
    me._metric_store_settings = lambda: FactorRanker._metric_store_settings(me)
    return me


def test_screen_universe_uses_metric_store_when_store_set(monkeypatch):
    """With screener_store set, the fast metric_store path is used (uppercased symbols)
    and StockScreener is NOT instantiated."""
    captured = {}

    def fake_load_store(path):
        captured["store_path"] = path
        return "FAKE_DF"

    def fake_screen_as_of(df, day, settings):
        captured["df"] = df
        captured["day"] = day
        captured["settings"] = settings
        return ["aapl", "Msft"]  # lower/mixed case -> must be uppercased

    fake_ms = SimpleNamespace(load_store=fake_load_store,
                              screen_universe_as_of=fake_screen_as_of)

    # Local import target: ba2_providers.screener.metric_store
    import ba2_providers.screener as screener_pkg
    monkeypatch.setattr(screener_pkg, "metric_store", fake_ms, raising=False)
    monkeypatch.setitem(sys.modules, "ba2_providers.screener.metric_store", fake_ms)

    # StockScreener MUST NOT be called on the fast path.
    def boom(*a, **k):
        raise AssertionError("StockScreener must not be called when screener_store is set")
    monkeypatch.setattr(FRmod, "StockScreener", boom)

    me = _fake_self({"screener_store": "/tmp/store", "screener_market_cap_min": 1})
    syms = FactorRanker._screen_universe(me, as_of=AS_OF)

    assert syms == ["AAPL", "MSFT"]  # uppercased from the metric_store
    assert captured["store_path"] == "/tmp/store"
    assert captured["df"] == "FAKE_DF"
    assert captured["day"] == "2024-01-08"  # as_of formatted YYYY-MM-DD


def test_screen_universe_falls_back_to_stockscreener_when_store_unset(monkeypatch):
    """With screener_store unset (live default), the StockScreener path is used unchanged."""
    captured = {}

    class FakeScreener:
        def __init__(self, settings, as_of=None):
            captured["called"] = True
            captured["as_of"] = as_of

        def screen(self):
            return {"results": [{"symbol": "spy"}, {"symbol": "QQQ"}]}

    monkeypatch.setattr(FRmod, "StockScreener", FakeScreener)

    me = _fake_self({"screener_store": "", "screener_min_price": 5})
    syms = FactorRanker._screen_universe(me, as_of=AS_OF)

    assert captured.get("called") is True  # fell back to StockScreener
    assert captured["as_of"] == AS_OF      # as_of still threaded
    assert syms == ["SPY", "QQQ"]


def test_screen_universe_falls_back_on_metric_store_failure(monkeypatch):
    """If the metric_store path raises (e.g. missing store), it logs a warning and
    FALLS BACK to StockScreener rather than crashing/returning empty."""
    def fake_load_store(path):
        raise FileNotFoundError(f"no store at {path}")

    fake_ms = SimpleNamespace(load_store=fake_load_store,
                              screen_universe_as_of=lambda *a, **k: ["NOPE"])
    monkeypatch.setitem(sys.modules, "ba2_providers.screener.metric_store", fake_ms)
    import ba2_providers.screener as screener_pkg
    monkeypatch.setattr(screener_pkg, "metric_store", fake_ms, raising=False)

    fallback = {}

    class FakeScreener:
        def __init__(self, settings, as_of=None):
            fallback["called"] = True

        def screen(self):
            return {"results": [{"symbol": "tlt"}]}

    monkeypatch.setattr(FRmod, "StockScreener", FakeScreener)

    me = _fake_self({"screener_store": "/missing/store"})
    syms = FactorRanker._screen_universe(me, as_of=AS_OF)

    assert fallback.get("called") is True  # degraded to StockScreener
    assert syms == ["TLT"]


def test_metric_store_settings_strips_screener_prefix():
    """_metric_store_settings translates screener_* -> unprefixed metric_store keys
    (passing the raw prefixed keys would match nothing => select everything)."""
    settings = {
        "screener_market_cap_min": 1_000_000_000,
        "screener_market_cap_max": 0,
        "screener_price_min": 20.0,
        "screener_price_max": 0,
        "screener_volume_min": 500_000,
        "screener_volume_max": 0,
        "screener_relative_volume_min": 1.05,
        "screener_price_drop_pct": 15.0,
        "screener_max_stocks": 10,
        "screener_sort_metric": "market_cap",
        "screener_weinstein_stage2_only": True,
        # keys the store does NOT support — must not leak through
        "screener_float_min": 10_000_000,
        "screener_price_drop_days": 1,
        "screener_provider": "fmp",
    }
    out = FactorRanker._metric_store_settings(_fake_self(settings))

    assert out == {
        "market_cap_min": 1_000_000_000,
        "market_cap_max": 0,
        "price_min": 20.0,
        "price_max": 0,
        "volume_min": 500_000,
        "volume_max": 0,
        "relative_volume_min": 1.05,
        "price_drop_pct": 15.0,
        "max_stocks": 10,
        "sort_metric": "market_cap",
        "weinstein_stage2_only": True,
    }
    # None of the unsupported / prefixed keys leaked into the translated dict.
    assert "float_min" not in out and "price_drop_days" not in out and "provider" not in out
    assert not any(k.startswith("screener_") for k in out)
