"""A TWO-PROFILE expert (``ohlcv-v1,ta-structure-v1``) under live replay CAPTURE.

The regression (8082 options-testing, 2026-09-24): the first live entry pass of a deployed
stage-1 O_LC genome gating on both profiles died in ``begin_decision`` with
``AttributeError: 'CompositeMarketConditionReader' object has no attribute '_retain_windows'`` --
capture wrapped the resolver's reader in ONE ``CapturingMarketConditionReader``, which only knows
a single profile's window reader. The whole entry pass aborted: 97 analyses ran, no rule was ever
evaluated, no order placed. (Code review 2026-09-22 finding P7.)

Pinned here, through a REAL ``ReplayStore`` capture and real FMP-cache readers:
* the decision scope opens and every gate of BOTH profiles evaluates to the same result as an
  uncaptured pass;
* each profile's served window is recorded once per (symbol, session), under that profile's own
  identity -- replay needs one recording per profile, not one merged blob;
* a single-profile expert is unchanged (still one wrapper, no composite).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import (
    CAPTURE_PROVIDER,
    CapturingMarketConditionReader,
    CompositeMarketConditionReader,
    FMPCacheMarketConditionReader,
    market_condition_reader_for,
)
from ba2_common.core.replay import ReplayStore, SessionRecord, capture_scope
from ba2_common.core.types import ExpertEventType

SESSION_ID = "S-MKTCOND-2P"
ANALYSIS_ID = "A-MKTCOND-2P-1"
DECISION = datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc)
PROFILES = ("ohlcv-v1", "ta-structure-v1")
#: One field from each profile -- the deployed genome gates on exactly this mix.
FIELDS = (ExpertEventType.N_UNDERLYING_ADX, ExpertEventType.N_CHANNEL_POS_20)


def _write(root, symbol, days):
    folder = os.path.join(root, "FMPOHLCVProvider")
    os.makedirs(folder, exist_ok=True)
    rng = np.random.default_rng(len(symbol))
    c = 50.0 * np.exp(np.cumsum(rng.normal(0, 0.015, len(days))))
    pd.DataFrame({"Date": pd.to_datetime([d.isoformat() for d in days]),
                  "Open": c * 1.002, "High": c * 1.02, "Low": c * 0.98, "Close": c,
                  "Volume": np.full(len(days), 5_000, dtype=np.int64)}).to_parquet(
        os.path.join(folder, f"{symbol}_1d.parquet"), index=False)


def _meta():
    return {"analysis_id": ANALYSIS_ID, "attempt_id": ANALYSIS_ID, "session_id": SESSION_ID,
            "expert_class": "TradeManager", "expert_instance_id": 1, "symbol": "AAA",
            "use_case": "enter_market", "scheduled_at": None, "started_at": DECISION}


def _leaf(field, symbol):
    return TC.create_condition(field, object(), symbol, None, operator_str=">", value=-1e9)


@pytest.fixture
def two_profile_resolver(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_replay_now", lambda: DECISION)
    saved = TC.get_market_condition_context_resolver()
    cache = tmp_path / "cache"
    _write(str(cache), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 180))
    readers = [FMPCacheMarketConditionReader(p, str(cache)) for p in PROFILES]
    reader = market_condition_reader_for(readers)
    assert isinstance(reader, CompositeMarketConditionReader)
    resolver = live.LiveMarketConditionResolver(PROFILES, reader=reader)
    TC.set_market_condition_context_resolver(resolver)
    yield resolver
    TC.set_market_condition_context_resolver(saved)


def _evaluate():
    out = {}
    for field in FIELDS:
        leaf = _leaf(field, "AAA")
        out[field] = (leaf.evaluate(), leaf.last_status, leaf.calculated_value)
    return out


def _store(tmp_path):
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="mktcond-2p-test", started_at=DECISION,
        exchange_tz="America/New_York", app_version="test", package_versions={"ba2_common": "test"},
        source_revision="0" * 40, dirty=False))
    return store


def test_a_two_profile_decision_opens_and_evaluates_under_capture(two_profile_resolver, tmp_path):
    with live.market_condition_decision_scope():
        uncaptured = _evaluate()

    store = _store(tmp_path)
    try:
        with capture_scope(store, _meta()) as capture:
            with live.market_condition_decision_scope() as state:
                assert state.recorder is not None
                captured = _evaluate()
                captured_again = _evaluate()
            observations = [p.observation for p in capture.observations
                            if p.observation.provider == CAPTURE_PROVIDER]
            capture.set_outcome(skip_reason="fixture")
    finally:
        store.close(timeout=5.0)

    assert captured == uncaptured, "capture must not change a gate's answer"
    assert captured_again == captured
    profiles = sorted(o.request_identity["profile"] for o in observations)
    assert profiles == sorted(PROFILES), (
        f"one recording per profile for (AAA, session) expected, got {profiles}")


def test_a_single_profile_expert_is_still_one_capturing_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_replay_now", lambda: DECISION)
    saved = TC.get_market_condition_context_resolver()
    cache = tmp_path / "cache"
    _write(str(cache), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 180))
    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(cache))
    TC.set_market_condition_context_resolver(
        live.LiveMarketConditionResolver("ohlcv-v1", reader=reader))
    store = _store(tmp_path)
    try:
        with capture_scope(store, _meta()) as capture:
            with live.market_condition_decision_scope() as state:
                assert isinstance(state.reader, CapturingMarketConditionReader)
            capture.set_outcome(skip_reason="fixture")
    finally:
        store.close(timeout=5.0)
        TC.set_market_condition_context_resolver(saved)
