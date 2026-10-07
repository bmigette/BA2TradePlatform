"""Experts decide on the CURRENT price (owner rule 2026-10-07): one seam for live and backtest.

* ``MarketExpertInterface._decision_price``: live -> the account quote, backtest -> ``providers.price_at_date``.
* every expert that anchors on a price goes through it (structural parity, (d));
* DeterministicScorer's price is that seam's price, never its daily frame's last close (live too), (b);
* an ATR level is the finished-session ATR magnitude applied to the CURRENT price, and a daily close
  offered as the anchor on an intraday clock is refused (c, e).
"""
from __future__ import annotations

import inspect
import pathlib
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.knowability import (
    DecisionPrice, StaleAnchorPrice, intraday_decisions)
from ba2_experts.DeterministicScorer import DeterministicScorer
from ba2_experts.DeterministicScorer.combine import atr_stop_price, atr_target_price

NOW = datetime(2024, 1, 3, 10, 0, tzinfo=timezone.utc)
EXPERT_DIR = pathlib.Path(inspect.getfile(DeterministicScorer)).parent.parent

# experts whose price anchor is read through the shared seam
SEAM_EXPERTS = ["FMPEarningsDrift", "FMPInsiderClusterBuy", "FinnHubRating", "FMPEarningsEvent",
                "FMPRating", "FMPSenateTraderCopy", "FMPSenateTraderWeight"]


# ----------------------------------------------------------------------------- (d) parity
@pytest.mark.parametrize("name", SEAM_EXPERTS)
def test_expert_reads_its_price_through_the_one_seam(name):
    src = (EXPERT_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert "self._decision_price(" in src, f"{name} does not use the shared price seam"
    # no second live/backtest branch of its own and no direct bundle read for the anchor
    assert "providers.price_at_date(" not in src.replace("self._decision_price(providers", ""), name
    assert "else providers.price_at_date" not in src, name


def test_deterministic_scorer_uses_the_seam_not_its_daily_frame():
    src = (EXPERT_DIR / "DeterministicScorer" / "__init__.py").read_text(encoding="utf-8")
    assert "self._decision_price(providers, symbol, as_of)" in src
    assert 'df["Close"].iloc[-1]' not in src


def test_seam_live_is_the_quote_and_backtest_is_the_bundle():
    e = DeterministicScorer.__new__(DeterministicScorer)
    seen = []
    e._get_current_price = lambda s: seen.append(("quote", s)) or 111.0

    class B:
        def price_at_date(self, s, as_of):
            seen.append(("bundle", s, as_of))
            return 99.0

    assert e._decision_price(B(), "X", None) == 111.0
    assert e._decision_price(B(), "X", NOW) == 99.0
    assert seen == [("quote", "X"), ("bundle", "X", NOW)]


# ----------------------------------------------------------------------------- (b) DeterministicScorer
def _scorer_with(bundle_price, frame_close, quote=None):
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._gather_w_analyst = 0.0
    e._gather_w_earnings = 0.0
    e._gather_index_symbol = "SPY"
    e._gather_use_model_target = False
    if quote is not None:
        e._get_current_price = lambda s: quote

    class FakeOHLCV:
        def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
            return pd.DataFrame({"Close": [frame_close]})

    class FakeNone:
        def __getattr__(self, name):
            return lambda *a, **k: ({"statements": []} if name.startswith("get_") and "statement" in name
                                    or name == "get_balance_sheet" else None)

    class Bundle:
        def ohlcv(self): return FakeOHLCV()
        def fundamentals_details(self): return FakeNone()
        def fundamentals_overview(self): return FakeNone()
        def insider(self): return FakeNone()
        def news(self): return FakeNone()
        def indicators(self): return FakeNone()
        def price_at_date(self, s, as_of): return bundle_price

    return e, Bundle()


def _no_macro(monkeypatch):
    # live (as_of None) reads the FRED cache, which this test has no business refreshing
    from ba2_experts.DeterministicScorer import data
    monkeypatch.setattr(data, "fetch_macro_series", lambda providers, as_of: {})
    monkeypatch.setattr(data, "fetch_index_closes", lambda providers, as_of, sym: None)


def test_backtest_recommendation_price_is_the_seam_price_not_the_daily_close():
    e, providers = _scorer_with(bundle_price=107.25, frame_close=100.0)
    assert e._gather(providers, as_of=NOW)["current_price"] == 107.25


def test_live_recommendation_price_is_the_account_quote_not_the_cached_daily_bar(monkeypatch):
    """The live effect of this change: DeterministicScorer reads the quote like every other expert."""
    _no_macro(monkeypatch)
    e, providers = _scorer_with(bundle_price=None, frame_close=100.0, quote=108.5)
    assert e._gather(providers, as_of=None)["current_price"] == 108.5


def test_no_price_refuses_the_decision_instead_of_falling_back_to_the_frame(monkeypatch):
    _no_macro(monkeypatch)
    e, providers = _scorer_with(bundle_price=None, frame_close=100.0, quote=None)
    e._get_current_price = lambda s: None
    assert e._gather(providers, as_of=None)["current_price"] is None


def test_the_decision_price_object_survives_gather_for_the_guard():
    dp = DecisionPrice(107.25, datetime(2024, 1, 3, 9, 55), NOW)
    e, providers = _scorer_with(bundle_price=dp, frame_close=100.0)
    assert e._gather(providers, as_of=NOW)["current_price"] is dp


# ----------------------------------------------------------------------------- (c, e) ATR level
def test_atr_levels_apply_the_finished_session_atr_to_the_current_price():
    """The ATR is a MAGNITUDE from finished sessions; the level is anchored on the current price."""
    cur = DecisionPrice(110.0, datetime(2024, 1, 3, 9, 55), NOW)
    with intraday_decisions(True):
        assert atr_stop_price(cur, 2.0, "BUY", {"k_stop": 2.0}) == pytest.approx(106.0)
        assert atr_target_price(cur, 2.0, "BUY", {"k_target": 3.0}) == pytest.approx(116.0)
        assert atr_stop_price(cur, 2.0, "SELL", {"k_stop": 2.0}) == pytest.approx(114.0)


def test_a_daily_close_as_anchor_is_refused_on_the_intraday_clock_but_not_on_the_daily_one():
    with intraday_decisions(True):
        with pytest.raises(StaleAnchorPrice):
            atr_stop_price(100.0, 2.0, "BUY", {"k_stop": 2.0})
        with pytest.raises(StaleAnchorPrice):
            atr_target_price(100.0, 2.0, "BUY", {"k_target": 3.0})
    assert atr_stop_price(100.0, 2.0, "BUY", {"k_stop": 2.0}) == pytest.approx(96.0)      # daily/live
    with intraday_decisions(False):
        assert atr_target_price(100.0, 2.0, "BUY", {"k_target": 3.0}) == pytest.approx(106.0)


def test_safeguard_stop_builder_is_guarded():
    from ba2_common.core.position_sizing import synthesize_safeguard_stop
    cur = DecisionPrice(110.0, datetime(2024, 1, 3, 9, 55), NOW)
    with intraday_decisions(True):
        assert synthesize_safeguard_stop(cur, True, 5.0, min_stop_pct=0.0) == pytest.approx(104.5)
        with pytest.raises(StaleAnchorPrice):
            synthesize_safeguard_stop(110.0, True, 5.0, min_stop_pct=0.0)
    assert synthesize_safeguard_stop(110.0, True, 5.0, min_stop_pct=0.0) == pytest.approx(104.5)
