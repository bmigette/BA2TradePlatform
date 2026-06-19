"""No survivorship bias: FactorRanker must thread as_of into StockScreener so a backtest
screens the point-in-time universe, not today's (survivor) universe.
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


def _fake_self():
    # _screen_universe reads self.settings/self.logger plus get_setting_with_interface_default
    # ("screener_store", the opt-in fast-path switch); FactorRanker.settings is a read-only
    # property, so call the unbound method with a duck-typed self. screener_store unset (""),
    # so these tests exercise the StockScreener fallback path unchanged.
    return SimpleNamespace(
        settings={"screener_min_price": 5},
        logger=logging.getLogger("t"),
        get_setting_with_interface_default=lambda k: {"screener_store": ""}.get(k),
    )


def test_screen_universe_threads_as_of(monkeypatch):
    captured = {}

    class FakeScreener:
        def __init__(self, settings, as_of=None):
            captured["as_of"] = as_of

        def screen(self):
            return {"results": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}

    monkeypatch.setattr(FRmod, "StockScreener", FakeScreener)
    syms = FactorRanker._screen_universe(_fake_self(), as_of=AS_OF)
    assert captured["as_of"] == AS_OF, "as_of not threaded into StockScreener (survivorship bias)"
    assert syms == ["AAPL", "MSFT"]


def test_screen_universe_live_as_of_none(monkeypatch):
    captured = {}

    class FakeScreener:
        def __init__(self, settings, as_of=None):
            captured["as_of"] = as_of

        def screen(self):
            return {"results": []}

    monkeypatch.setattr(FRmod, "StockScreener", FakeScreener)
    FactorRanker._screen_universe(_fake_self())  # live path: no as_of
    assert captured["as_of"] is None  # live screen unchanged
