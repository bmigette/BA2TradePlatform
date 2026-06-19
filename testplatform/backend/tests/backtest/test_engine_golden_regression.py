"""Phase 2 Task 4 / GATE item 5: the engine does NOT perturb decision logic.

For a fixed ``as_of``, ``expert.analyze_as_of(as_of, ctx)`` — called through the EXACT
``BacktestContext`` the daily engine constructs (Phase-1 ``LiveProviderBundle`` over the
host provider resolver + the resolved settings dict) — must equal the Phase-1 golden path:

    rec_live = expert._process(expert._gather(live_providers, as_of=None), settings)
    rec_engine = expert.analyze_as_of(as_of, BacktestContext(... as the engine builds ...))
    rec_live.almost_equals(rec_engine)   # signal / confidence / expected_profit / details / skip

This mirrors ``BA2TradeExperts/tests/test_golden_live_vs_asof.py`` (the Phase-1 gate) but
drives the comparison through the host engine's context-construction so we prove the engine
layer is transparent to the decision. ``current_price`` is pinned identically (the harness
re-pins it) so a price-source diff can never mask a logic drift (Decision 1).

The two CLEAN experts (FMPEarningsDrift, FMPInsiderClusterBuy — no LLM) are covered, the same
two the daily engine runs first. The deterministic provider fakes are TIME-INVARIANT (fixed
earnings surprise / fixed insider cluster) so the only thing exercised is the as_of plumbing.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_engine_golden_regression.py -v
"""
from __future__ import annotations

import contextlib
import importlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import pandas as pd
import pytest

from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.types import Recommendation


NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Wall-clock determinism: the LIVE golden path calls ``_process(..., as_of=None)``,
# and an expert resolves ``now = as_of or datetime.now(timezone.utc)`` (e.g.
# FMPEarningsDrift.py:158). With ``as_of=None`` that ``now`` is the REAL wall
# clock, while the ENGINE path is anchored to the fixed ``NOW`` below. On any
# calendar day other than ``NOW``'s date the two paths compute a different
# ``days_since_report`` (off-by-one over a midnight boundary), which perturbs the
# confidence and breaks the comparison -- a pure date-rollover flake, NOT a logic
# drift. We pin the live path's ``now`` to ``NOW`` so the golden comparison is
# deterministic on every day. The engine path is unaffected (it already passes a
# concrete ``as_of``); the time-invariant fixtures keep the decision identical.
# --------------------------------------------------------------------------- #
_FROZEN_EXPERT_MODULES = (
    "ba2_experts.FMPEarningsDrift",
    "ba2_experts.FMPInsiderClusterBuy",
)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now``/``utcnow`` return the fixed ``NOW`` instant."""

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mirror datetime.now signature
        return NOW if tz is not None else NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return NOW.replace(tzinfo=None)


@contextlib.contextmanager
def _frozen_clock():
    """Freeze ``datetime.now``/``utcnow`` to ``NOW`` inside the expert modules so
    the live ``as_of=None`` path is wall-clock independent."""
    saved = {}
    for mod_name in _FROZEN_EXPERT_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        saved[mod_name] = getattr(mod, "datetime", None)
        mod.datetime = _FrozenDatetime
    try:
        yield
    finally:
        for mod_name, original in saved.items():
            mod = importlib.import_module(mod_name)
            if original is not None:
                mod.datetime = original


# --------------------------------------------------------------------------- #
# Deterministic, time-invariant provider fakes (replicated from the Phase-1
# golden_fixtures so the host test is hermetic / self-contained).
# --------------------------------------------------------------------------- #
class _FakeOHLCV:
    """Constant-close OHLCV provider -> a pinned current_price in both paths."""

    def __init__(self, close: float = 100.0):
        self._close = close

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [self._close]})


def _provider_resolver(mapping: Dict[str, Any]) -> Callable[..., Any]:
    """A get_provider(category, name, **kw) callable backed by a category->provider map."""

    def get_provider(category, name, **kw):
        return mapping[category]

    return get_provider


def _build_earnings_drift():
    from ba2_experts.FMPEarningsDrift import FMPEarningsDrift

    class FakeDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2,
                                  "estimated_eps": 1.0, "surprise_percent": 20.0}]}

    e = FMPEarningsDrift.__new__(FMPEarningsDrift)
    e.id = 1
    e._gather_symbol = "AAPL"
    settings = {"surprise_min_pct": 5.0, "max_days_since_report": 30,
                "expected_profit_percent": 8.0}
    gp = _provider_resolver({"fundamentals_details": FakeDetails(), "ohlcv": _FakeOHLCV()})
    return e, settings, gp


def _build_insider_cluster():
    from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy

    three_buyers = {
        "start_date": "2026-05-14T00:00:00", "end_date": "2026-06-13T00:00:00",
        "transactions": [
            {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
            {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
            {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
        ],
    }

    class FakeInsider:
        def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                     as_of=None, format_type="dict", **kw):
            return three_buyers

    e = FMPInsiderClusterBuy.__new__(FMPInsiderClusterBuy)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._gather_lookback_days = 30
    settings = {"lookback_days": 30, "min_insiders": 3, "min_total_value": 200_000.0,
                "expected_profit_percent": 10.0}
    gp = _provider_resolver({"insider": FakeInsider(), "ohlcv": _FakeOHLCV()})
    return e, settings, gp


CLEAN_EXPERTS = {
    "FMPEarningsDrift": _build_earnings_drift,
    "FMPInsiderClusterBuy": _build_insider_cluster,
}


def _engine_context(get_provider: Callable, settings: Dict[str, Any], symbol: str,
                    account: Any = None) -> BacktestContext:
    """Construct the BacktestContext EXACTLY as ``DailyBacktestEngine._run_expert_bar`` does:
    a Phase-1 ``LiveProviderBundle`` over the resolver + the resolved settings dict + as_of.

    (The engine threads the symbol via the bundle's as_of-aware providers; the clean experts
    read ``self._gather_symbol`` set by the fixture, mirroring the Phase-1 golden harness.)
    """
    return BacktestContext(
        providers=LiveProviderBundle(get_provider),
        settings=settings,
        as_of=NOW,
        account=account,
        subtype=None,
        extra={"symbol": symbol},
    )


@pytest.mark.parametrize("name", list(CLEAN_EXPERTS.keys()))
def test_engine_context_analyze_equals_golden(name):
    """analyze_as_of via the engine's context == the Phase-1 live golden recommendation."""
    expert, settings, get_provider = CLEAN_EXPERTS[name]()

    # Phase-1 live path: _gather(live, None) + _process. The live path resolves
    # ``now`` from the wall clock (as_of=None); freeze it to NOW so the golden
    # comparison is deterministic on every calendar day (see _frozen_clock).
    with _frozen_clock():
        bundle_live = expert._gather(LiveProviderBundle(get_provider), as_of=None)
        rec_live = expert._process(bundle_live, settings, as_of=None)

    # Backtest path through the ENGINE's context construction.
    ctx = _engine_context(get_provider, settings, symbol="AAPL")
    rec_engine = expert.analyze_as_of(NOW, ctx)

    assert isinstance(rec_engine, Recommendation), f"{name}: not a Recommendation"
    # Pin current_price identically so only the decision tuple is compared (Decision 1).
    rec_engine.current_price = rec_live.current_price
    assert rec_live.almost_equals(rec_engine), (
        f"{name} drift:\n  live  ={rec_live}\n  engine={rec_engine}"
    )
    # Sanity: both clean experts BUY on their planted bullish fixture.
    from ba2_common.core.types import OrderRecommendation

    assert rec_engine.signal == OrderRecommendation.BUY
    assert rec_engine.skip is False


def test_both_clean_experts_covered():
    assert set(CLEAN_EXPERTS) == {"FMPEarningsDrift", "FMPInsiderClusterBuy"}
