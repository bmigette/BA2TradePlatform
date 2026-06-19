"""Perf regression gate (no network, no timing): assert the backtest perf wins via call
counts on the hermetic fixture path.

  * Weekly cadence  -> the engine skips analyze_as_of() on off-cadence bars (far fewer
                       expensive expert evaluations than daily).
  * ActivityLogging -> disabled for the whole backtest, so NOTHING is enqueued to the
                       ActivityLog queue/DB (no per-bar write churn).

The FMP TTLCache freeze (one fetch/symbol) is covered at the unit level in
BA2TradeProviders/tests/test_fmp_common_ttl.py — the hermetic fixture providers do not go
through the TTLCache, so it can't be exercised here.
"""
from __future__ import annotations

import pytest

from ba2_experts.FMPEarningsDrift import FMPEarningsDrift

from tests.backtest.fixtures.e2e_support import (
    earnings_drift_payload,
    ensure_host_schema,
    hermetic_providers,
    new_backtest_row,
    run_daily_backtest,
)

_WEEKLY_MONDAY = {
    "days": {
        "monday": True, "tuesday": False, "wednesday": False, "thursday": False,
        "friday": False, "saturday": False, "sunday": False,
    }
}


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    ensure_host_schema()
    yield


def _count_analyze_calls(monkeypatch) -> dict:
    counter = {"n": 0}
    orig = FMPEarningsDrift.analyze_as_of

    def spy(self, *args, **kwargs):
        counter["n"] += 1
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(FMPEarningsDrift, "analyze_as_of", spy)
    return counter


def test_weekly_cadence_reduces_expert_evaluations(monkeypatch):
    """Weekly entry cadence must call analyze_as_of() far fewer times than daily."""
    # daily (legacy: analyse every bar)
    daily = _count_analyze_calls(monkeypatch)
    run_daily_backtest(earnings_drift_payload(new_backtest_row("perf-daily")), task_id="perf-daily")
    daily_n = daily["n"]

    # weekly (Monday-only entry analysis)
    weekly = _count_analyze_calls(monkeypatch)
    payload = earnings_drift_payload(new_backtest_row("perf-weekly"))
    payload["run_schedule_override"] = _WEEKLY_MONDAY
    run_daily_backtest(payload, task_id="perf-weekly")
    weekly_n = weekly["n"]

    assert daily_n > 0 and weekly_n > 0
    # ~5x fewer in principle; assert a conservative >=2x reduction so the gate is robust
    # to the fixture calendar.
    assert weekly_n * 2 <= daily_n, f"weekly={weekly_n} not <= daily/2={daily_n / 2}"


def test_activity_logging_silenced_during_backtest(monkeypatch):
    """No ActivityLog entries are enqueued during a backtest (per-bar churn eliminated)."""
    from app.services.backtest import daily_backtest_handler as H
    from ba2_common.core import db

    puts: list = []
    monkeypatch.setattr(db._activity_log_queue, "put", lambda *a, **k: puts.append(a))

    config = H._build_config(earnings_drift_payload(new_backtest_row("perf-actlog")))
    with hermetic_providers():
        result = H.run_daily_backtest(config)

    assert result, "backtest should produce a result blob"
    assert puts == [], f"expected 0 ActivityLog enqueues during backtest, got {len(puts)}"
