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
def _fill_missing_attrs(expert, call):
    """Run ``call`` and give the expert the plain config attributes its ``_gather`` reads (the
    ``_gather_*`` fields run_analysis normally sets) one AttributeError at a time."""
    import re
    for _ in range(40):
        try:
            return call()
        except AttributeError as exc:
            m = re.search(r"no attribute '(\w+)'", str(exc))
            if not m:
                raise
            setattr(expert, m.group(1), "key" if "key" in m.group(1) else 30)
    raise AssertionError("could not satisfy the expert's gather attributes")


BEHAVIOURAL = ["FMPEarningsDrift", "FMPInsiderClusterBuy", "FMPEarningsEvent", "FMPRating"]


@pytest.mark.parametrize("name", BEHAVIOURAL)
def test_expert_gather_asks_the_seam_and_uses_its_value(name):
    """Patch the seam: the expert's gather must call it for the symbol/instant and put ITS value in
    the bundle, with the bundle's own ``price_at_date`` forbidden (no second source)."""
    import importlib
    from unittest.mock import MagicMock
    cls = getattr(importlib.import_module(f"ba2_experts.{name}"), name)
    e = cls.__new__(cls)
    e.id = 1
    e.logger = MagicMock()
    e._gather_symbol = "AAPL"
    seen = []
    e._decision_price = lambda providers, symbol, as_of: seen.append((symbol, as_of)) or 123.456
    providers = MagicMock()
    providers.price_at_date.side_effect = AssertionError("expert read the bundle directly")
    bundle = _fill_missing_attrs(e, lambda: e._gather(providers, NOW))
    assert seen == [("AAPL", NOW)]
    assert bundle["current_price"] == 123.456


@pytest.mark.parametrize("name", [n for n in SEAM_EXPERTS if n not in BEHAVIOURAL])
def test_remaining_experts_call_the_seam_and_never_the_bundle_directly(name):
    """FinnHubRating and the two Senate experts need network-shaped fixtures to run ``_gather``; they
    are checked on the syntax tree (calls, not text): a ``_decision_price`` call, no ``price_at_date``
    call on a bundle."""
    import ast
    tree = ast.parse((EXPERT_DIR / f"{name}.py").read_text(encoding="utf-8"))
    attrs = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "_decision_price" in attrs, f"{name} does not use the shared price seam"
    assert "price_at_date" not in attrs, f"{name} reads the bundle's price directly"


def test_deterministic_scorer_uses_the_seam_not_its_daily_frame():
    src = (EXPERT_DIR / "DeterministicScorer" / "__init__.py").read_text(encoding="utf-8")
    assert "self._decision_price(providers, symbol, as_of)" in src
    assert 'df["Close"].iloc[-1]' not in src


def test_seam_is_the_account_live_and_backtest_and_the_bundle_only_without_an_account():
    """ONE path: the account interface. Live -> the quote; backtest -> the run's account (its decision
    price); the bundle's historical close is reachable ONLY for a bundle with no account behind it (the
    historical-replay tool)."""
    e = DeterministicScorer.__new__(DeterministicScorer)
    seen = []
    e._get_current_price = lambda s: seen.append(("quote", s)) or 111.0

    class B:
        def price_at_date(self, s, as_of):
            seen.append(("bundle", s, as_of))
            return 99.0

    class Acct:
        def get_instrument_current_price(self, s):
            seen.append(("account", s))
            return 105.0

    bundle = B()
    assert e._decision_price(bundle, "X", None) == 111.0
    e._decision_account_cache = (bundle, Acct())
    with intraday_decisions(True):
        assert e._decision_price(bundle, "X", NOW) == 105.0             # intraday backtest: the account
    # the DAILY clock keeps the pre-existing bundle read, the account is never asked
    assert e._decision_price(bundle, "X", NOW) == 99.0
    e._decision_account_cache = (bundle, None)
    assert e._decision_price(bundle, "X", NOW) == 99.0                  # no account: replay fallback
    assert seen == [("quote", "X"), ("account", "X"), ("bundle", "X", NOW), ("bundle", "X", NOW)]


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


# ----------------------------------------------------------------------------- no quote: its own skip
def test_live_without_a_quote_is_skipped_as_no_price_not_as_thin_history():
    """The wrong reason used to be recorded (``insufficient_history``) and the price fell back to 0.0."""
    from unittest.mock import MagicMock
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 7
    e.logger = MagicMock()
    rec = e._process({"symbol": "AAPL", "ohlcv": pd.DataFrame({"Close": [1.0] * 400}),
                      "current_price": None}, {"min_history_days": 260}, None)
    assert rec.skip and rec.skip_reason == "no_price"
    assert rec.current_price is None
    assert "price" in rec.details.lower() and "history" not in rec.details.lower()
    e.logger.error.assert_called_once()
    assert "AAPL" in e.logger.error.call_args[0][0]


def test_thin_history_with_a_price_is_still_insufficient_history():
    e = DeterministicScorer.__new__(DeterministicScorer)
    rec = e._process({"symbol": "AAPL", "ohlcv": pd.DataFrame({"Close": [1.0] * 10}),
                      "current_price": 100.0}, {"min_history_days": 260}, None)
    assert rec.skip and rec.skip_reason == "insufficient_history" and rec.current_price == 100.0


# ----------------------------------------------------------------------------- never absorbed
@pytest.mark.parametrize("mode", ["enforce", "observe", "legacy"])
def test_stale_anchor_price_is_never_absorbed_in_any_error_mode(monkeypatch, mode):
    from ba2_common.core.failure_modes import absorb_if_benign, is_never_absorbed
    monkeypatch.setenv("BA2_ERROR_MODE", mode)
    exc = StaleAnchorPrice("daily close as anchor")
    assert is_never_absorbed(exc)
    with pytest.raises(StaleAnchorPrice):
        try:
            raise exc
        except Exception as e:       # the shape of every broad handler
            absorb_if_benign(e)
            pytest.fail("the handler absorbed a StaleAnchorPrice")


@pytest.mark.parametrize("mode", ["enforce", "observe", "legacy"])
def test_the_sizing_loop_handler_lets_it_through(monkeypatch, mode):
    """TradeRiskManagement's per-order handler routes every exception through ``absorb_if_benign``
    (so the never-absorb rule applies to it) and the guard sits inside that try, before the price is
    used. Checked on the syntax tree: the handler of the ``try`` containing the guard."""
    import ast
    from ba2_common.core import TradeRiskManagement as trm
    monkeypatch.setenv("BA2_ERROR_MODE", mode)
    tree = ast.parse(inspect.getsource(trm))
    guarded = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                isinstance(n, ast.Call) and getattr(n.func, "id", "") == "require_decision_price"
                for b in node.body for n in ast.walk(b)):
            guarded.append(node)
    assert guarded, "no try block contains the size-anchor guard"
    for node in guarded:
        for h in node.handlers:
            calls = [getattr(n.func, "id", "") for b in h.body for n in ast.walk(b) if isinstance(n, ast.Call)]
            assert "absorb_if_benign" in calls, "the sizing handler does not apply the never-absorb rule"


@pytest.mark.parametrize("mode", ["enforce", "observe", "legacy"])
def test_trade_action_price_anchor_raises_in_every_mode(monkeypatch, mode):
    from types import SimpleNamespace
    from ba2_common.core.TradeActions import TradeAction

    monkeypatch.setenv("BA2_ERROR_MODE", mode)
    from ba2_common.core.TradeActions import AdjustTakeProfitAction
    action = AdjustTakeProfitAction.__new__(AdjustTakeProfitAction)     # a concrete TradeAction
    action.instrument_name = "AAPL"
    action.account = SimpleNamespace(get_instrument_current_price=lambda s: 100.0)   # a plain float
    with intraday_decisions(True):
        with pytest.raises(StaleAnchorPrice):
            action.get_current_price()
    assert action.get_current_price() == 100.0           # live / daily clock: untouched
