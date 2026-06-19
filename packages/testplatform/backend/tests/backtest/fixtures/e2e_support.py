"""Shared wiring for the Phase 2 Task 6 e2e / reproducibility GATE tests.

Drives the REAL ``handle_daily_backtest`` handler against the hermetic provider fixtures
(no network, no FMP key). The fixture ``get_provider`` is injected into BOTH provider seams:

  1. ``ba2_providers.get_provider`` — monkeypatched so the handler's
     ``_run_engine`` constructs the AsOfPriceSource over the FIXTURE ohlcv provider
     (the handler does ``from ba2_providers import get_provider`` inside the function,
     so patching the module attribute is picked up at call time).
  2. ``ba2_common.core.TradeConditions``'s provider resolver — re-pointed at the same
     fixture callable so the engine's ``LiveProviderBundle`` (built from
     ``TradeConditions._get_provider``) resolves the fixture providers too.

Both are restored after the run so the suite stays hermetic for other tests.

The host ``Backtest`` RESULTS row lives in the default ``SessionLocal`` DB (created on the
default engine, as the other backend tests do). Each call creates a fresh row and returns
its id so a test can load the persisted result.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

from app.models.backtest import Backtest
from app.models.database import Base, SessionLocal, engine

from tests.backtest.fixtures.hermetic_providers import (
    EARNINGS_DRIFT_SETTINGS,
    INSIDER_CLUSTER_SETTINGS,
    TRADE_END,
    TRADE_START,
    UNIVERSE,
    make_fixture_get_provider,
)


def ensure_host_schema() -> None:
    """Create the host ``backtests`` table on the default engine if missing."""
    Base.metadata.create_all(bind=engine)


def new_backtest_row(name: str = "e2e-daily") -> int:
    """Insert a fresh pending ``Backtest`` row (model_id=None, daily expert) and return id."""
    db = SessionLocal()
    try:
        bt = Backtest(
            name=name,
            model_id=None,
            start_date=TRADE_START,
            end_date=TRADE_END,
            initial_capital=100_000.0,
            commission=1.0,
            slippage=0.0,
            status="pending",
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        return bt.id
    finally:
        db.close()


def load_backtest(backtest_id: int) -> Backtest:
    """Load a persisted ``Backtest`` row (a detached copy of its column values)."""
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == backtest_id).first()
        if bt is None:
            raise AssertionError(f"Backtest {backtest_id} not found")
        # Touch the lazy JSON columns so they are loaded before the session closes.
        _ = (bt.equity_curve, bt.drawdown_curve, bt.trades, bt.results)
        db.expunge(bt)
        return bt
    finally:
        db.close()


def earnings_drift_payload(backtest_id: int, *, seed: int = 42) -> Dict[str, Any]:
    """A ``daily_backtest`` payload for FMPEarningsDrift over the fixture cache."""
    return _payload(backtest_id, "FMPEarningsDrift", EARNINGS_DRIFT_SETTINGS, seed)


def insider_cluster_payload(backtest_id: int, *, seed: int = 42) -> Dict[str, Any]:
    """A ``daily_backtest`` payload for FMPInsiderClusterBuy over the fixture cache."""
    return _payload(backtest_id, "FMPInsiderClusterBuy", INSIDER_CLUSTER_SETTINGS, seed)


def _payload(
    backtest_id: int, expert_class: str, settings: Dict[str, Any], seed: int
) -> Dict[str, Any]:
    return {
        "backtest_id": backtest_id,
        "name": f"e2e-{expert_class}",
        "enabled_instruments": list(UNIVERSE),
        "experts": [{"class": expert_class, "settings": settings}],
        "start_date": TRADE_START.isoformat(),
        "end_date": TRADE_END.isoformat(),
        "initial_capital": 100_000.0,
        "commission": 1.0,
        "slippage": 0.0,
        "fill_model": "next_bar_open",
        "warmup_days": 30,
        "seed": seed,
    }


@contextmanager
def hermetic_providers() -> Iterator[None]:
    """Point BOTH provider seams at the fixture cache for the duration of the block.

    Restores the original ``ba2_providers.get_provider`` and the original
    ``TradeConditions`` provider resolver on exit (so the suite stays hermetic).
    """
    import ba2_providers
    from ba2_common.core import TradeConditions

    fixture_get_provider = make_fixture_get_provider()

    orig_module_get = ba2_providers.get_provider
    orig_resolver: Optional[Any] = TradeConditions.get_provider_resolver()

    ba2_providers.get_provider = fixture_get_provider  # type: ignore[assignment]
    TradeConditions.set_provider_resolver(fixture_get_provider)
    try:
        yield
    finally:
        ba2_providers.get_provider = orig_module_get  # type: ignore[assignment]
        if orig_resolver is not None:
            TradeConditions.set_provider_resolver(orig_resolver)


def run_daily_backtest(payload: Dict[str, Any], task_id: str = "e2e") -> Dict[str, Any]:
    """Run ``handle_daily_backtest`` under the hermetic providers and return its result dict."""
    from app.services.backtest import daily_backtest_handler as H

    with hermetic_providers():
        return H.handle_daily_backtest(task_id, payload)
