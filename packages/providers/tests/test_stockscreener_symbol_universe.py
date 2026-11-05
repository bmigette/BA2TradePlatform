"""``screener_symbol_universe``: the live/as-of screen restricted to a fixed list BEFORE the filters, the rank and the cut.

The backtest's panel gate applies ``universe_restriction`` in its valid mask (before the max_stocks cut); live must pick
the best ``max_stocks`` OF THE LIST the same way, or a deployed option job selects different names than it was scored on.
No network: a stub provider, the as-of path with every HTTP stage off."""
from datetime import datetime, timezone

import pytest

import ba2_providers
import ba2_providers.StockScreener as S

AS_OF = datetime(2024, 3, 15, tzinfo=timezone.utc)


class _Provider:
    def __init__(self, rows):
        self.rows = rows

    def screen_stocks(self, filters, as_of=None):
        return [dict(r) for r in self.rows]


def _rows():
    # market cap descending: AAA is the market's biggest
    return [{"symbol": s, "market_cap": cap, "price": 50.0, "volume": 1e6}
            for s, cap in (("AAA", 9e11), ("BBB", 8e11), ("CCC", 7e11), ("DDD", 6e11), ("EEE", 5e11), ("FFF", 4e11))]


def _screen(monkeypatch, **extra):
    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: _Provider(_rows()))
    st = {"screener_relative_volume_min": 0, "screener_price_drop_pct": 0, "screener_float_min": 0,
          "screener_volume_min": 0, "screener_max_stocks": 2, **extra}
    return S.StockScreener(st, as_of=AS_OF).screen()


def test_without_the_setting_the_screen_is_the_markets_top_n(monkeypatch):
    out = _screen(monkeypatch)
    assert [r["symbol"] for r in out["results"]] == ["AAA", "BBB"]
    assert "dropped_symbol_universe" not in out["stats"]


def test_the_cut_is_taken_from_the_list_not_the_market(monkeypatch):
    out = _screen(monkeypatch, screener_symbol_universe="ccc, EEE,fff")
    assert [r["symbol"] for r in out["results"]] == ["CCC", "EEE"]          # best 2 OF THE LIST by market cap
    assert out["stats"]["dropped_symbol_universe"] == 3


def test_a_list_that_matches_nothing_returns_no_candidates(monkeypatch):
    out = _screen(monkeypatch, screener_symbol_universe="ZZZ")
    assert out["results"] == [] and out["stats"]["dropped_symbol_universe"] == 6


def test_share_class_spelling_and_comments_are_normalised_like_the_backtest_side(monkeypatch):
    rows = _rows() + [{"symbol": "BRK.B", "market_cap": 3e11, "price": 400.0, "volume": 1e6}]
    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: _Provider(rows))
    st = {"screener_relative_volume_min": 0, "screener_price_drop_pct": 0, "screener_float_min": 0,
          "screener_volume_min": 0, "screener_max_stocks": 5,
          "screener_symbol_universe": "brk-b  # share class\n# whole-line comment\nccc"}
    out = S.StockScreener(st, as_of=AS_OF).screen()
    assert [r["symbol"] for r in out["results"]] == ["CCC", "BRK.B"]       # vendor dotted spelling matches the dash


def test_an_empty_intersection_logs_a_warning(monkeypatch):
    seen = []

    class _Log:
        def __getattr__(self, level):
            return lambda msg, *a, **k: seen.append((level, str(msg)))
    monkeypatch.setattr(S, "logger", _Log())
    _screen(monkeypatch, screener_symbol_universe="ZZZ")
    assert any(lv == "warning" and "NOTHING is left" in m for lv, m in seen)
    seen.clear()
    _screen(monkeypatch, screener_symbol_universe="CCC")
    assert not [1 for lv, _ in seen if lv == "warning"]


def test_parse_forms_and_malformed_value():
    assert S.parse_symbol_universe(None) is None and S.parse_symbol_universe("") is None
    assert S.parse_symbol_universe(" a ,B,,a") == frozenset({"A", "B"})
    assert S.parse_symbol_universe(["x", "Y"]) == frozenset({"X", "Y"})
    with pytest.raises(ValueError, match="screener_symbol_universe"):
        S.parse_symbol_universe(123)
    with pytest.raises(ValueError):
        S.parse_symbol_universe([1, 2])


def test_the_setting_is_declared_on_the_interface_and_in_the_deploy_contract():
    from ba2_common.core.deploy_parity import LIVE_SCREENER_SETTINGS
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    MarketExpertInterface._ensure_builtin_settings()
    d = MarketExpertInterface._builtin_settings["screener_symbol_universe"]
    assert d["default"] == "" and d["type"] == "str"
    assert S.SYMBOL_UNIVERSE_KEY in LIVE_SCREENER_SETTINGS
