"""Capture and replay of the market-condition windows a live decision consumed
(design 2026-09-15 sections 4.1, 4.2: "Capture the exact normalized window [...], the three
values and per-field status. A hash without retained bytes is not replayable.").

Recorded through a REAL ``ReplayStore`` (sync writer), exported, reloaded with ``load_bundle`` and
served by ``ReplayMarketConditionReader`` -- the same round trip every other replay fixture uses.
"""
from __future__ import annotations

import base64
import os
import shutil
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import (
    CAPTURE_METHOD,
    CAPTURE_PROVIDER,
    FMPCacheMarketConditionReader,
    ReplayMarketConditionReader,
)
from ba2_common.core.market_condition_source import (
    window_digest,
    window_digest_of_bytes,
    window_from_bytes,
)
from ba2_common.core.market_conditions import STATUS_INSUFFICIENT_HISTORY, STATUS_VALID, WINDOW
from ba2_common.core.replay import (
    CaptureContext,
    ReplayMiss,
    ReplayStore,
    SessionRecord,
    capture_scope,
    load_bundle,
    use_capture_context,
)
from ba2_common.core.types import ExpertEventType

SESSION_ID = "S-MKTCOND"
ANALYSIS_ID = "A-MKTCOND-1"
DECISION = datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc)
PRIOR = date(2025, 6, 30)
FIELDS = (ExpertEventType.N_UNDERLYING_TREND_SLOPE, ExpertEventType.N_UNDERLYING_ADX,
          ExpertEventType.N_UNDERLYING_RV_RATIO)


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


def _leaf(field, symbol, op, value):
    return TC.create_condition(field, object(), symbol, None, operator_str=op, value=value)


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(live, "_replay_now", lambda: DECISION)


@pytest.fixture
def seam():
    saved = TC.get_market_condition_context_resolver()
    yield
    TC.set_market_condition_context_resolver(saved)


@pytest.fixture
def recorded(tmp_path, fixed_clock, seam):
    """Run one captured decision pass (two symbols, every field evaluated twice) and export it."""
    cache = tmp_path / "cache"
    _write(str(cache), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 180))
    _write(str(cache), "YNG", regular_sessions_ending_at(PRIOR, 40))
    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(cache))
    TC.set_market_condition_context_resolver(live.LiveMarketConditionResolver("ohlcv-v1", reader=reader))

    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="mktcond-test", started_at=DECISION,
        exchange_tz="America/New_York", app_version="test", package_versions={"ba2_common": "test"},
        source_revision="0" * 40, dirty=False))
    results = {}
    try:
        with capture_scope(store, _meta()) as capture:
            with live.market_condition_decision_scope() as state:
                assert state.recorder is not None
                for symbol in ("AAA", "YNG"):
                    for field in FIELDS:
                        for _ in range(2):
                            leaf = _leaf(field, symbol, ">", -1e9)
                            results[(symbol, field)] = (leaf.evaluate(), leaf.last_status, leaf.calculated_value)
            pending = [p.observation for p in capture.observations]
            capture.set_outcome(skip_reason="fixture")
        exported = store.export_session(SESSION_ID, tmp_path / "export")
    finally:
        store.close(timeout=5.0)
    return {"bundle_dir": exported, "results": results, "pending": pending, "cache": cache,
            "reader": reader}


def test_one_observation_per_symbol_session_window(recorded):
    obs = [o for o in recorded["pending"] if o.provider == CAPTURE_PROVIDER]
    assert [o.method for o in obs] == [CAPTURE_METHOD, CAPTURE_METHOD]
    assert sorted(o.request_identity["symbol"] for o in obs) == ["AAA", "YNG"]
    for o in obs:
        assert o.request_identity["session"] == PRIOR.isoformat()
        assert o.request_identity["source_profile"] == "fmp-daily-split-adjusted-v1"
        assert o.request_identity["timing_policy"] == "prior_session_v1"
        assert o.request_identity["calc_version"] == "ohlcv-v1/calc-1"
    assert recorded["results"][("AAA", ExpertEventType.N_UNDERLYING_ADX)][:2] == (True, STATUS_VALID)
    assert recorded["results"][("YNG", ExpertEventType.N_UNDERLYING_ADX)][:2] == (False, STATUS_INSUFFICIENT_HISTORY)


def test_the_bundle_retains_the_window_bytes_and_values(recorded):
    bundle = load_bundle(recorded["bundle_dir"])
    payloads = {p["symbol"]: p for p in (bundle.decode(o.payload_object)
                                         for o in bundle.observations_for(ANALYSIS_ID)
                                         if o.provider == CAPTURE_PROVIDER)}
    aaa = payloads["AAA"]
    raw = base64.b64decode(aaa["window_f8_b64"])
    assert aaa["window_shape"] == [WINDOW, 5] and len(raw) == WINDOW * 5 * 8
    assert window_digest_of_bytes(raw) == aaa["window_digest"]
    o, h, l, c, v = window_from_bytes(raw)
    assert window_digest(o, h, l, c, v) == aaa["window_digest"]
    assert c.dtype == np.float64 and c[-1] > 0
    assert aaa["window_first_session"] == regular_sessions_ending_at(PRIOR, WINDOW)[0]
    live_row = recorded["reader"].observe("AAA", PRIOR)
    assert aaa["values"] == {f: o.value for f, o in live_row.by_field().items()}
    assert set(aaa["statuses"].values()) == {STATUS_VALID}

    yng = payloads["YNG"]
    assert yng["window_f8_b64"] is None and yng["window_digest"] is None
    assert set(yng["statuses"].values()) == {STATUS_INSUFFICIENT_HISTORY}


def _replay_reader(bundle_dir):
    return ReplayMarketConditionReader.from_bundle(
        load_bundle(bundle_dir), ANALYSIS_ID, profile="ohlcv-v1",
        source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1")


def test_replay_serves_the_recorded_rows(recorded):
    reader = _replay_reader(recorded["bundle_dir"])
    assert reader.observe("AAA", PRIOR) == recorded["reader"].observe("AAA", PRIOR)
    assert reader.observe("YNG", PRIOR) == recorded["reader"].observe("YNG", PRIOR)
    arrays = reader.recorded_window("AAA", PRIOR)
    assert len(arrays) == 5 and all(len(a) == WINDOW for a in arrays)


def test_replay_miss_instead_of_reading_the_cache(recorded):
    reader = _replay_reader(recorded["bundle_dir"])
    with pytest.raises(ReplayMiss) as miss:
        reader.observe("AAA", date(2025, 6, 27))
    assert miss.value.kind == "market_condition_window"
    with pytest.raises(ReplayMiss):
        reader.observe("BBB", PRIOR)


def test_replay_refuses_a_tampered_window(recorded):
    bundle = load_bundle(recorded["bundle_dir"])
    payloads = [bundle.decode(o.payload_object) for o in bundle.observations_for(ANALYSIS_ID)
                if o.provider == CAPTURE_PROVIDER]
    for p in payloads:
        if p["symbol"] == "AAA":
            raw = bytearray(base64.b64decode(p["window_f8_b64"]))
            raw[-16] ^= 0x01   # flip one bit of the last close
            p["window_f8_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
    reader = ReplayMarketConditionReader.from_payloads(
        payloads, profile="ohlcv-v1", source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1")
    with pytest.raises(ReplayMiss) as miss:
        reader.observe("AAA", PRIOR)
    assert miss.value.kind == "market_condition_window_digest"


def test_replay_refuses_a_hash_without_bytes(recorded):
    bundle = load_bundle(recorded["bundle_dir"])
    payloads = [dict(bundle.decode(o.payload_object)) for o in bundle.observations_for(ANALYSIS_ID)
                if o.provider == CAPTURE_PROVIDER]
    for p in payloads:
        p["window_f8_b64"] = None
    reader = ReplayMarketConditionReader.from_payloads(
        payloads, profile="ohlcv-v1", source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1")
    with pytest.raises(ReplayMiss) as miss:
        reader.observe("AAA", PRIOR)
    assert miss.value.kind == "market_condition_window_bytes"


def test_a_replayed_decision_uses_the_tape_and_never_the_cache(recorded, fixed_clock, seam):
    shutil.rmtree(recorded["cache"])   # any cache read would now return None -> missing_session
    inner = FMPCacheMarketConditionReader("ohlcv-v1", str(recorded["cache"]))
    TC.set_market_condition_context_resolver(live.LiveMarketConditionResolver("ohlcv-v1", reader=inner))
    replay = CaptureContext.for_replay(analysis_id=ANALYSIS_ID, clock_reads=[])

    with use_capture_context(replay):
        with pytest.raises(ReplayMiss):
            with live.market_condition_decision_scope():
                pass
        with live.market_condition_decision_scope(replay_reader=_replay_reader(recorded["bundle_dir"])):
            got = {}
            for symbol in ("AAA", "YNG"):
                for field in FIELDS:
                    leaf = _leaf(field, symbol, ">", -1e9)
                    got[(symbol, field)] = (leaf.evaluate(), leaf.last_status, leaf.calculated_value)
    assert got == recorded["results"]
    assert inner.computed == 0


def test_two_profiles_for_one_symbol_session_do_not_overwrite(recorded):
    bundle = load_bundle(recorded["bundle_dir"])
    payloads = [dict(bundle.decode(o.payload_object)) for o in bundle.observations_for(ANALYSIS_ID)
                if o.provider == CAPTURE_PROVIDER]
    other = [dict(p, profile="ta-structure-v1", window_digest="sha256:" + "0" * 64,
                  window_f8_b64=None) for p in payloads]
    reader = ReplayMarketConditionReader.from_payloads(
        payloads + other, profile="ohlcv-v1", source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1")
    assert reader.observe("AAA", PRIOR) == recorded["reader"].observe("AAA", PRIOR)


def test_capture_dedupes_windows_by_digest_and_failures_by_status_and_reason():
    from ba2_common.core.market_condition_readers import (
        CapturingMarketConditionReader,
        ObservedWindow,
    )
    from ba2_common.core.market_conditions import OHLCV_V1, FeatureRow

    valid = FeatureRow.uniform(OHLCV_V1, STATUS_INSUFFICIENT_HISTORY, "a")
    key = CapturingMarketConditionReader._dedupe_key
    with_digest = ObservedWindow(row=valid, window=None, digest="sha256:abc")
    assert key("AAA", PRIOR, with_digest) == ("AAA", PRIOR, "sha256:abc")
    a = key("AAA", PRIOR, ObservedWindow(row=valid, window=None, digest=None))
    b = key("AAA", PRIOR, ObservedWindow(row=FeatureRow.uniform(OHLCV_V1, STATUS_INSUFFICIENT_HISTORY, "b"),
                                         window=None, digest=None))
    assert a != b, "a different reason is a different observation"
    assert key("AAA", PRIOR, None) == ("AAA", PRIOR, None)
