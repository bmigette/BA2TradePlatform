"""Task 1 (measure first): the counter-only dry run behind ``BA2_REPLAY_DECISION_DRYRUN=1``.

With the flag set and NO replay store, every decision pass runs its recorders into a throwaway context and
counts the bytes it WOULD have written (pass_state + steps + the decision summary) and the pass-thread time
the recorders took; nothing is written and the decision does not change. With a real store the flag is
ignored and the real writes agree with the counter. ``test_measure_realistic_pass`` is the measurement the
design doc quotes (an expert with ~20 recommendations and ~30 open transactions); it writes its numbers to
the file named by ``BA2_DECISION_MEASURE_OUT`` when that is set.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest
from sqlmodel import SQLModel

from ba2_common.core.replay import decision_sizing as ds
from ba2_common.core.replay import ReplayStore, SessionRecord, set_replay_store
from ba2_common.core.types import (
    AnalysisUseCase, ExpertActionType, OrderDirection, OrderOpenType, OrderRecommendation,
    OrderStatus, OrderType, RiskLevel, TimeHorizon, TransactionStatus,
)
from tests import factories
from tests.test_decision_pass_capture_roundtrip import _Account, _reset, _snapshot
from tests.test_funded_entry_loop import FROZEN_NOW, _EnterExpert, _run_enter

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


def realistic_enter_world(n_recs=20, n_open=30, confident=None):
    """An expert with ``n_recs`` BUY recommendations and ``n_open`` OPENED positions (each with its entry
    order and TP/SL legs), a four-condition entry ruleset (bullish, confidence, expected profit, has no
    position) buying with a stop-loss and a take-profit. Recommendations carry a ~1.5 KB ``data`` payload
    and ~600 characters of details, like the experts' real rows."""
    acct_def = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="enter-real", subtype=AnalysisUseCase.ENTER_MARKET)
    rule = factories.create_event_action(
        name="buy-tier", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": {"event_type": "bullish"},
                  "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 60.0},
                  "trigger_2": {"event_type": "expected_profit_target_percent", "operator": ">=",
                                "value": 5.0},
                  "trigger_3": {"event_type": "has_no_position"}},
        actions={"action_0": {"action_type": ExpertActionType.BUY.value},
                 "action_1": {"action_type": ExpertActionType.ADJUST_STOP_LOSS.value,
                              "value": -5.0, "reference_value": "current_price"},
                 "action_2": {"action_type": ExpertActionType.ADJUST_TAKE_PROFIT.value,
                              "value": 12.0, "reference_value": "current_price"}})
    factories.link_rule_to_ruleset(rs.id, rule.id, 0)
    inst = factories.create_expert_instance(account_id=acct_def.id, expert="_EnterExpert",
                                            virtual_equity_pct=100.0, enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"), "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"), "max_virtual_equity_per_instrument_percent": (4.0, "float"),
    })
    payload = {"FMPRating": {f"metric_{i:02d}": round(1.2345 * i, 4) for i in range(40)},
               "notes": "analyst consensus " * 40}
    details = "Strong momentum with improving fundamentals and a supportive analyst revision trend. " * 8
    for index in range(n_recs):
        factories.create_recommendation(
            instance_id=inst.id, symbol=f"S{index:02d}", recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=float(n_recs - index), price_at_date=100.0,
            confidence=90.0 if index % 5 else 55.0, risk_level=RiskLevel.MEDIUM,
            time_horizon=TimeHorizon.SHORT_TERM, details=details, data=payload, created_at=FROZEN_NOW)
    for index in range(n_open):
        symbol = f"O{index:02d}"
        txn = factories.create_transaction(symbol=symbol, quantity=10.0, status=TransactionStatus.OPENED,
                                           open_price=100.0, expert_id=inst.id, stop_loss=95.0,
                                           take_profit=112.0)
        entry = factories.create_trading_order(
            account_id=acct_def.id, symbol=symbol, quantity=10.0, status=OrderStatus.FILLED,
            transaction_id=txn.id, side=OrderDirection.BUY, open_price=100.0,
            open_type=OrderOpenType.AUTOMATIC)
        for leg_type, leg_kw in ((OrderType.SELL_STOP, {"stop_price": 95.0}),
                                 (OrderType.SELL_LIMIT, {"limit_price": 112.0})):
            factories.create_trading_order(
                account_id=acct_def.id, symbol=symbol, quantity=10.0, status=OrderStatus.WAITING_TRIGGER,
                transaction_id=txn.id, side=OrderDirection.SELL, order_type=leg_type,
                depends_on_order=entry.id, depends_order_status_trigger=OrderStatus.FILLED, **leg_kw)
    account = _Account(acct_def.id)
    account._balance = 1_000_000.0
    return inst, expert, account


def _run_world(test_engine, n_recs=20, n_open=30):
    _reset(test_engine)
    inst, expert, account = realistic_enter_world(n_recs, n_open)
    started = time.perf_counter()
    created = _run_enter(expert, account, inst.id)
    return {"created": [o.id for o in created], "seconds": time.perf_counter() - started,
            "submits": account.submits, **_snapshot()}


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.delenv(ds.DRYRUN_ENV, raising=False)
    ds.COUNTERS.reset()
    yield
    ds.COUNTERS.reset()


def test_the_flag_is_off_by_default_and_counts_nothing(test_engine):
    _run_world(test_engine, 4, 3)
    assert ds.COUNTERS.snapshot() == [] and not ds.dry_run_enabled()


def test_the_dry_run_counts_the_bytes_of_every_part_and_changes_no_decision(test_engine, monkeypatch):
    off = _run_world(test_engine, 6, 5)
    assert off["created"]
    monkeypatch.setenv(ds.DRYRUN_ENV, "1")
    on = _run_world(test_engine, 6, 5)
    on_decisions = {k: v for k, v in on.items() if k != "seconds"}
    off_decisions = {k: v for k, v in off.items() if k != "seconds"}
    assert on_decisions == off_decisions

    (measurement,) = ds.COUNTERS.snapshot()
    assert measurement["pass"] == "enter-market" and measurement["outcome"] == "decision"
    assert measurement["steps"]["pass_state"] == 1 and measurement["steps"]["rules"] == 6
    assert measurement["steps"]["size"] == 1 and measurement["steps"]["execute"] == len(off["created"])
    for part in ("pass_state", "rules", "size", "execute", "decision"):
        assert measurement["bytes"][part] > 0, part
    assert measurement["total_bytes"] == sum(measurement["bytes"].values())
    assert measurement["record_ms"] > 0
    assert ds.COUNTERS.summary()["passes"] == 1


def test_the_dry_run_writes_nothing_and_is_ignored_when_a_real_store_exists(test_engine, monkeypatch, tmp_path):
    monkeypatch.setenv(ds.DRYRUN_ENV, "1")
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    set_replay_store(store)
    try:
        _run_world(test_engine, 3, 2)
    finally:
        set_replay_store(None)
    assert ds.COUNTERS.snapshot() == []                  # the real store measured it, not the counter
    assert len(store.index.analyses("s1")) == 1
    store.close(timeout=2.0)


def test_the_counter_agrees_with_what_a_real_store_writes(test_engine, monkeypatch, tmp_path):
    monkeypatch.setenv(ds.DRYRUN_ENV, "1")
    _run_world(test_engine, 6, 5)
    (measurement,) = ds.COUNTERS.snapshot()

    monkeypatch.delenv(ds.DRYRUN_ENV)
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    set_replay_store(store)
    try:
        _run_world(test_engine, 6, 5)
    finally:
        set_replay_store(None)
    (analysis,) = store.index.analyses("s1")
    written = {}
    for observation in store.index.observations(analysis.analysis_id):
        if observation.provider != "decision_step":
            continue
        kind, data, _meta = store.objects.get(observation.payload_object)
        written[observation.method] = written.get(observation.method, 0) + len(data)
    for part, size in written.items():
        assert measurement["bytes"][part] == pytest.approx(size, rel=0.05), part
    store.close(timeout=2.0)


def test_measure_realistic_pass(test_engine, monkeypatch):
    """~20 recommendations, ~30 open transactions: bytes and added milliseconds per enter pass.

    Three alternating off/on pairs; the figure quoted is the median. ``pass_on`` includes the dry run's own
    encoding of the payloads (which a live store does on its writer thread, not the pass thread), so
    ``record_ms`` -- the pass-thread time the recorders themselves took -- is the cleaner cost figure and
    ``pass_on - pass_off`` an upper bound.
    """
    import statistics

    offs, ons, records, last = [], [], [], None
    for _ in range(3):
        monkeypatch.delenv(ds.DRYRUN_ENV, raising=False)
        off = _run_world(test_engine)
        monkeypatch.setenv(ds.DRYRUN_ENV, "1")
        ds.COUNTERS.reset()
        on = _run_world(test_engine)
        assert {k: v for k, v in on.items() if k != "seconds"} == {
            k: v for k, v in off.items() if k != "seconds"}
        (last,) = ds.COUNTERS.snapshot()
        offs.append(off["seconds"])
        ons.append(on["seconds"])
        records.append(last["record_ms"])
    result = {"recommendations": 20, "open_transactions": 30, "funded": len(off["created"]),
              "pass_off_seconds": statistics.median(offs), "pass_on_seconds": statistics.median(ons),
              "added_ms_upper_bound": 1000 * (statistics.median(ons) - statistics.median(offs)),
              "record_ms_on_pass_thread": statistics.median(records), "measurement": last}
    out = os.environ.get("BA2_DECISION_MEASURE_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, default=str)
    assert last["total_bytes"] > 0
