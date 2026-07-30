"""Hermetic cache-miss loudness contract: BacktestCacheMiss/FMPHistoryCacheMiss raised by an
expert's ``analyze_as_of`` must ABORT the run, never be silently swallowed as a per-symbol/
per-bar failure. A missing pre-warm is a setup BUG (every subsequent symbol/bar would hit the
same gap); treating it like an ordinary per-symbol data hiccup degrades results invisibly.

Audited 2026-07-15 while building the FMPSenateTraderWeight trader-skill/scalper-filter
feature (which reads fmp_history_disk_cached under hermetic_fmp_history()). Found the
ENTER_MARKET path (_run_expert_bar) already did this correctly; the OPEN_POSITIONS path
(_manage_open_positions) and the bypass-expert path (_run_bypass_expert_bar) did NOT — an
ordinary exception and a hermetic cache miss were both silently logged-and-skipped. Fixed to
match ENTER_MARKET's handling in both cases; these tests lock all three sites to the same
contract (and confirm a genuinely ordinary exception is still swallowed, not over-corrected
into aborting on everything).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation
from ba2_providers.fmp_common import FMPHistoryCacheMiss
from app.services.backtest.price_source import BacktestCacheMiss

from tests.backtest.test_daily_engine_unit import _build_run, BARS


class _RaisingExpert(MarketExpertInterface):
    """analyze_as_of raises whatever exception the test configures (default: a normal BUY
    on the first call, then the configured exception on every subsequent call — so the
    OPEN_POSITIONS test can open a position on bar 1 via the normal path, then hit the raise
    on a later bar's management call)."""

    def __init__(self, id: int, price_source, exc: Exception, buy_on: date | None = None):
        super().__init__(id)
        self._ps = price_source
        self._exc = exc
        self._buy_on = buy_on
        self.calls = 0

    @classmethod
    def description(cls) -> str:
        return "Stub expert whose analyze_as_of raises a configured exception."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        self.calls += 1
        d = as_of.date() if hasattr(as_of, "date") else as_of
        if self._buy_on is not None and d == self._buy_on:
            close = self._ps.close_at("AAPL", as_of)
            return Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                                  current_price=float(close), details="stub buy",
                                  expected_profit_percent=10.0)
        raise self._exc


@pytest.mark.parametrize("exc_cls", [FMPHistoryCacheMiss, BacktestCacheMiss])
def test_enter_market_reraises_hermetic_cache_miss(exc_cls):
    """Baseline: confirms the ALREADY-correct ENTER_MARKET path (no fix needed here)."""
    engine, account, expert, ctx, ps = _build_run()
    try:
        raising = _RaisingExpert(expert.id, ps, exc_cls("not pre-warmed"))
        with pytest.raises(exc_cls):
            engine._run_expert_bar(raising, expert.id, {}, 1, ["AAPL"], datetime(2024, 1, 2))
    finally:
        ctx.__exit__(None, None, None)


def test_enter_market_swallows_ordinary_exception():
    """An ordinary per-symbol failure must still be swallowed (not over-corrected)."""
    engine, account, expert, ctx, ps = _build_run()
    try:
        raising = _RaisingExpert(expert.id, ps, RuntimeError("transient data hiccup"))
        engine._run_expert_bar(raising, expert.id, {}, 1, ["AAPL"], datetime(2024, 1, 2))  # no raise
        assert raising.calls == 1
    finally:
        ctx.__exit__(None, None, None)


def _open_position_via_full_run(engine, account, expert, ps):
    """Run the fixture's normal engine.run() once (well-tested to open+fill an AAPL long by
    day 2), so _manage_open_positions has a real held position to iterate."""
    # _ensure_safeguard_stop always attempts an ATR lookup now regardless of sizing_mode;
    # None is its documented no-ATR-available fallback.
    engine._indicator_provider = None
    engine.run()
    assert engine._held_transactions(expert.id).get("AAPL"), \
        "fixture must have an open AAPL position to exercise the OPEN_POSITIONS path"


@pytest.mark.parametrize("exc_cls", [FMPHistoryCacheMiss, BacktestCacheMiss])
def test_open_positions_reraises_hermetic_cache_miss(exc_cls):
    """Regression: this path was silently swallowing cache misses before the 2026-07-15 fix."""
    from app.services.backtest.default_rulesets import seed_open_positions_ruleset
    from ba2_common.core.db import get_instance, update_instance
    from ba2_common.core.models import ExpertInstance

    engine, account, expert, ctx, ps = _build_run()
    try:
        _open_position_via_full_run(engine, account, expert, ps)

        open_ruleset_id = seed_open_positions_ruleset([])
        instance = get_instance(ExpertInstance, expert.id)
        instance.open_positions_ruleset_id = open_ruleset_id
        update_instance(instance)

        raising = _RaisingExpert(expert.id, ps, exc_cls("not pre-warmed"))
        with pytest.raises(exc_cls):
            engine._manage_open_positions(raising, expert.id, {}, datetime(2024, 1, 8))
    finally:
        ctx.__exit__(None, None, None)


def test_open_positions_swallows_ordinary_exception():
    from app.services.backtest.default_rulesets import seed_open_positions_ruleset
    from ba2_common.core.db import get_instance, update_instance
    from ba2_common.core.models import ExpertInstance

    engine, account, expert, ctx, ps = _build_run()
    try:
        _open_position_via_full_run(engine, account, expert, ps)

        open_ruleset_id = seed_open_positions_ruleset([])
        instance = get_instance(ExpertInstance, expert.id)
        instance.open_positions_ruleset_id = open_ruleset_id
        update_instance(instance)

        raising = _RaisingExpert(expert.id, ps, RuntimeError("transient data hiccup"))
        engine._manage_open_positions(raising, expert.id, {}, datetime(2024, 1, 8))  # no raise
        assert raising.calls == 1
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("exc_cls", [FMPHistoryCacheMiss, BacktestCacheMiss])
def test_bypass_expert_reraises_hermetic_cache_miss(exc_cls):
    """Regression: the bypass path's own docstring claimed parity with ENTER_MARKET's
    handling but did not actually re-raise before the 2026-07-15 fix."""
    engine, account, expert, ctx, ps = _build_run()
    try:
        raising = _RaisingExpert(expert.id, ps, exc_cls("not pre-warmed"))
        with pytest.raises(exc_cls):
            engine._run_bypass_expert_bar(raising, expert.id, {}, datetime(2024, 1, 2))
    finally:
        ctx.__exit__(None, None, None)


def test_bypass_expert_swallows_ordinary_exception():
    engine, account, expert, ctx, ps = _build_run()
    try:
        raising = _RaisingExpert(expert.id, ps, RuntimeError("transient data hiccup"))
        engine._run_bypass_expert_bar(raising, expert.id, {}, datetime(2024, 1, 2))  # no raise
        assert raising.calls == 1
    finally:
        ctx.__exit__(None, None, None)
