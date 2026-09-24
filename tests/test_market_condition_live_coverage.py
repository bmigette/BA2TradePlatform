"""Live-side coverage of the pinned snapshot, and the capture scope the decision pass needs.

TWO SILENT FAILURES ARE CLOSED HERE.

1. COVERAGE. A pinned manifest covers exactly the symbols the warmup could warm -- the first
   real one covers 85 of the 98-symbol option universe. For a symbol it omits, every live gate
   reads ``missing_session`` forever: the sleeve never enters it, the strategy is quietly
   reduced to a subset of its universe, and nothing anywhere says which subset or why. Now
   each uncovered symbol gets ONE ERROR naming the digest, and its gates report ``no_context``
   with that reason rather than the generic "no decision scope is open".

2. CAPTURE. The market-condition gates are read in the decision pass, after every analysis of
   the pass has closed its own capture scope. Without a scope of its own the pass records no
   windows, and a replay of a gated bundle raises ``ReplayMiss`` in ``begin_decision``. The
   scope must open BEFORE the decision scope, because ``begin_decision`` reads
   ``current_capture()`` to decide whether to wrap the reader.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
from ba2_common.core.market_conditions import PROFILES, STATUS_NO_CONTEXT, STATUS_VALID
from ba2_common.core.types import ExpertEventType

DECISION = datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc)
PRIOR = date(2025, 6, 30)
SESSION_ID = "2025-07-01-coverage"
FIELD = ExpertEventType.N_UNDERLYING_ADX


def _write_cache(root, symbol, days):
    folder = os.path.join(root, "FMPOHLCVProvider")
    os.makedirs(folder, exist_ok=True)
    rng = np.random.default_rng(len(symbol))
    c = 50.0 * np.exp(np.cumsum(rng.normal(0, 0.015, len(days))))
    pd.DataFrame({"Date": pd.to_datetime([d.isoformat() for d in days]),
                  "Open": c * 1.002, "High": c * 1.02, "Low": c * 0.98, "Close": c,
                  "Volume": np.full(len(days), 5_000, dtype=np.int64)}).to_parquet(
        os.path.join(folder, f"{symbol}_1d.parquet"), index=False)


def _publish(root, symbols):
    """A published snapshot carrying VALID rows for ``symbols`` and nothing else."""
    from ba2_common.core.market_condition_store import MarketConditionStore, month_of

    profile = PROFILES["ohlcv-v1"]
    fields = [f.name for f in profile.fields]
    sessions = regular_sessions_ending_at(date(2025, 7, 1), 40)
    store = MarketConditionStore(root)
    objects, by_month = [], {}
    for symbol in symbols:
        by_month.clear()
        for session in sessions:
            by_month.setdefault(month_of(session), []).append(
                {"session": session, "values": [0.05, 20.0, 0.9],
                 "status": [STATUS_VALID] * len(fields), "reasons": [""] * len(fields),
                 "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                 "raw_row_lo": 0, "raw_row_hi": 0})
        objects += [store.write_feature_object(profile, symbol, rows)[0]
                    for _, rows in sorted(by_month.items())]
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1",
        timing_policy="prior_session_v1", objects=objects, raw_objects=[],
        coverage={s: {"rows": len(sessions)} for s in symbols}, universe=list(symbols),
        sessions=list(sessions), window_start=sessions[0], window_end=sessions[-1])
    return store, store.write_manifest(manifest)


class _Records(logging.Handler):
    """``caplog`` cannot see these messages: ``ba2_common``/``ba2_trade_platform`` are
    configured with ``propagate = False``, so nothing reaches the root handler pytest
    installs. Attach directly to the loggers the code under test actually uses."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=logging.ERROR):
        return [r.getMessage() for r in self.records if r.levelno >= level]


@pytest.fixture
def logs():
    handler = _Records()
    loggers = [logging.getLogger("ba2_common"), logging.getLogger("ba2_trade_platform")]
    for log in loggers:
        log.addHandler(handler)
    yield handler
    for log in loggers:
        log.removeHandler(handler)


@pytest.fixture
def seam():
    saved = TC.get_market_condition_context_resolver()
    yield
    TC.set_market_condition_context_resolver(saved)


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(live, "_replay_now", lambda: DECISION)


@pytest.fixture
def pinned(tmp_path):
    """A resolver over a snapshot that covers AAA and NOT BBB."""
    store, digest = _publish(tmp_path / "cache", ["AAA"])
    _write_cache(str(store.cache_root), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 180))
    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(store.cache_root),
                                           manifest_digest=digest)
    return live.LiveMarketConditionResolver("ohlcv-v1", reader=reader, manifest_digest=digest)


def _leaf(symbol, op=">", value=-1e9):
    return TC.create_condition(FIELD, object(), symbol, None, operator_str=op, value=value)


# --------------------------------------------------------------------------- coverage
def test_an_uncovered_symbol_is_reported_once_with_the_digest(pinned, logs):
    missing = pinned.refresh_coverage(["AAA", "BBB"])
    pinned.refresh_coverage(["AAA", "BBB"], force=True)         # a second pass, same universe
    assert missing == ["BBB"]
    errors = [m for m in logs.messages() if "BBB" in m]
    assert len(errors) == 1, errors
    assert pinned.manifest_digest in errors[0]


def test_a_fully_covered_universe_reports_nothing(pinned, logs):
    assert pinned.refresh_coverage(["AAA"]) == []
    assert logs.messages() == []


def test_the_check_is_skipped_when_no_manifest_is_pinned(tmp_path, logs):
    """Research mode has no snapshot to be uncovered BY; refusing symbols there would be an
    invented failure."""
    _write_cache(str(tmp_path), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 180))
    resolver = live.LiveMarketConditionResolver(
        "ohlcv-v1", reader=FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path)))
    assert resolver.refresh_coverage(["AAA", "BBB", "CCC"]) == []
    assert logs.messages() == []
    assert resolver.uncovered == {}


def test_a_repeated_universe_does_not_re_check(pinned, monkeypatch):
    """Spied, not inferred: asserting the cached key is unchanged cannot fail if the re-check
    ran anyway."""
    calls = []
    real = live.missing_coverage
    monkeypatch.setattr(live, "missing_coverage",
                        lambda mapped, universe: calls.append(tuple(universe)) or real(mapped, universe))
    assert pinned.refresh_coverage(["AAA", "BBB"]) == ["BBB"]
    assert pinned.refresh_coverage(["AAA", "BBB"]) == ["BBB"]      # same answer, no work
    assert calls == [("AAA", "BBB")]
    # ...and a CHANGED universe IS re-checked, which is the point of doing it per pass.
    assert pinned.refresh_coverage(["AAA"]) == []
    assert calls == [("AAA", "BBB"), ("AAA",)]
    assert pinned.uncovered == {}
    # force re-runs it even on the cached key.
    pinned.refresh_coverage(["AAA"], force=True)
    assert len(calls) == 3


def test_an_uncovered_symbols_gate_reads_no_context_naming_the_digest(pinned, seam, fixed_clock,
                                                                      monkeypatch):
    monkeypatch.setattr(live, "gated_live_universe", lambda: (("AAA", "BBB"), ()))
    TC.set_market_condition_context_resolver(pinned)
    pinned.refresh_coverage()
    with live.market_condition_decision_scope():
        covered, uncovered = _leaf("AAA"), _leaf("BBB")
        assert covered.evaluate() is True
        assert covered.last_status == STATUS_VALID
        assert uncovered.evaluate() is False
        assert uncovered.last_status == STATUS_NO_CONTEXT
    assert pinned.manifest_digest in uncovered.last_reason
    assert "BBB" in uncovered.last_reason
    # NOT the generic sentence: the whole point is that the refusal names its own cause.
    assert uncovered.last_reason != live.NO_DECISION_SCOPE_REASON


def test_the_decision_scope_refreshes_the_coverage_itself(pinned, seam, fixed_clock, monkeypatch):
    """The live universe changes between passes (an instance enabled, an instrument added),
    so the check cannot only run at install."""
    seen = []
    monkeypatch.setattr(live, "gated_live_universe",
                        lambda: (seen.append(1) or (("AAA", "BBB"), ())))
    TC.set_market_condition_context_resolver(pinned)
    with live.market_condition_decision_scope():
        pass
    assert seen == [1]
    assert sorted(pinned.uncovered) == ["BBB"]


# --------------------------------------------------------------------------- the universe
def _instance(id, enabled, entry, exit=None):
    return SimpleNamespace(id=id, enabled=enabled, enter_market_ruleset_id=entry,
                           open_positions_ruleset_id=exit)


def test_the_universe_is_the_union_of_the_GATED_instances_enabled_instruments(monkeypatch):
    instances = [
        _instance(1, True, 10),            # gated
        _instance(2, True, 20, 20),        # not gated (entry nor exit)
        _instance(3, False, 10, 30),       # disabled
        _instance(4, True, None, None),    # no ruleset at all
        _instance(5, True, 30),            # gated, dynamic
    ]
    # ``name`` is a NOT NULL column on EventAction and the leaf walk labels its findings with
    # it, so the doubles carry one (a double missing a required column is a test artefact, not
    # a shape the DB can produce).
    rules = {
        10: [SimpleNamespace(name="entry", triggers={"cond_0": {"event_type": "confidence"},
                                                     "cond_1": {"event_type": FIELD.value}})],
        20: [SimpleNamespace(name="entry", triggers={"cond_0": {"event_type": "confidence"}})],
        30: [SimpleNamespace(name="entry", triggers={"cond_0": {"event_type": FIELD.value}})],
    }
    experts = {1: SimpleNamespace(get_enabled_instruments=lambda: ["aaa", "BBB"]),
               5: SimpleNamespace(get_enabled_instruments=lambda: ["SCREENER"])}
    import ba2_common.core.db as db
    import ba2_common.core.instance_resolver as ir

    monkeypatch.setattr(db, "get_all_instances", lambda model: instances)
    monkeypatch.setattr(db, "ruleset_event_actions", lambda rid: rules.get(rid, []))
    monkeypatch.setattr(ir, "get_instance_resolver",
                        lambda: SimpleNamespace(get_expert_instance=experts.__getitem__))

    assert live.gated_expert_instances() == (1, 5)
    symbols, deferred = live.gated_live_universe()
    # Upper-cased (the manifest's symbols are), and the dynamic instance contributes a
    # SENTINEL rather than a symbol -- a coverage check against "SCREENER" would refuse a
    # symbol that does not exist.
    assert symbols == ("AAA", "BBB")
    assert deferred == ((5, "SCREENER"),)


def test_an_expert_gated_only_on_its_exits_is_coverage_checked(monkeypatch):
    """Plan 2026-09-24 B2: a market leaf may sit on an open-positions rule. An expert gated ONLY
    there must still be listed, or its uncovered symbols' exits read unknown with nothing at
    startup naming the snapshot that misses them. A ``None`` slot is skipped, not read."""
    instances = [
        _instance(6, True, 20, 40),        # entry ungated, EXIT gated
        _instance(7, True, None, 40),      # no entry ruleset, exit gated
        _instance(8, True, 20, None),      # neither gated, no exit ruleset
        _instance(9, False, None, 40),     # disabled
    ]
    rules = {
        20: [SimpleNamespace(name="entry", triggers={"cond_0": {"event_type": "confidence"}})],
        40: [SimpleNamespace(name="market exit",
                             triggers={"cond_0": {"event_type": FIELD.value}})],
    }
    read: list = []
    experts = {6: SimpleNamespace(get_enabled_instruments=lambda: ["ccc"]),
               7: SimpleNamespace(get_enabled_instruments=lambda: ["DDD", "SCREENER"])}
    import ba2_common.core.db as db
    import ba2_common.core.instance_resolver as ir

    monkeypatch.setattr(db, "get_all_instances", lambda model: instances)
    monkeypatch.setattr(db, "ruleset_event_actions",
                        lambda rid: read.append(rid) or rules.get(rid, []))
    monkeypatch.setattr(ir, "get_instance_resolver",
                        lambda: SimpleNamespace(get_expert_instance=experts.__getitem__))

    assert live.gated_expert_instances() == (6, 7)
    assert None not in read
    symbols, deferred = live.gated_live_universe()
    assert symbols == ("CCC", "DDD")
    assert deferred == ((7, "SCREENER"),)


def test_an_unreadable_universe_at_install_warns_and_re_checks_later(pinned, logs, monkeypatch):
    """``wire_all_seams`` runs BEFORE ``init_db``: the expert/ruleset read can legitimately
    fail at install, and that must not be the end of the check."""
    def boom():
        raise RuntimeError("no such table: expertinstance")

    monkeypatch.setattr(live, "gated_live_universe", boom)
    assert pinned.refresh_coverage(at_install=True) == []
    assert pinned.refresh_coverage(at_install=True) == []
    warnings = [m for m in logs.messages(logging.WARNING) if "could not be read" in m]
    assert len(warnings) == 1
    assert logs.messages(logging.ERROR) == []    # expected at install; not an error there
    assert pinned._checked_universe is None      # nothing was checked, so nothing is cached


def test_an_unreadable_universe_at_DECISION_time_is_an_error_per_cause(pinned, logs, monkeypatch):
    """A DIFFERENT EVENT from the install one, and it must not be swallowed by the install
    flag's once-per-process budget: it means coverage has stopped being checked at all."""
    causes = iter(["database is locked",            # the install attempt
                   "database is locked", "database is locked",   # twice at decision time
                   "no such table: ruleset"])       # a DIFFERENT cause

    def boom():
        raise RuntimeError(next(causes))

    monkeypatch.setattr(live, "gated_live_universe", boom)
    pinned.refresh_coverage(at_install=True)     # spend the install budget first
    for _ in range(3):
        assert pinned.refresh_coverage() == []
    errors = [m for m in logs.messages(logging.ERROR) if "NOT being checked" in m]
    assert len(errors) == 2                      # one per distinct cause, not one per process
    assert any("database is locked" in m for m in errors)
    assert any("no such table: ruleset" in m for m in errors)


def test_a_deferred_universe_is_reported_once(pinned, logs, monkeypatch):
    monkeypatch.setattr(live, "gated_live_universe", lambda: (("AAA",), ((5, "SCREENER"),)))
    pinned.refresh_coverage()
    pinned.refresh_coverage(force=True)
    said = [m for m in logs.messages(logging.WARNING)
            if "pick their universe at analysis time" in m]
    assert len(said) == 1


# --------------------------------------------------------------------------- capture
@pytest.fixture
def store(tmp_path):
    from ba2_common.core.replay import set_replay_store
    from ba2_common.core.replay.schemas import SessionRecord
    from ba2_common.core.replay.service import ReplayStore

    st = ReplayStore(tmp_path / "store", writer="sync")
    st.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="coverage-test", started_at=DECISION,
        exchange_tz="America/New_York", app_version="test",
        package_versions={"ba2_common": "test"}, source_revision="0" * 40, dirty=False))
    set_replay_store(st)
    yield st
    set_replay_store(None)
    st.close(timeout=5.0)


@pytest.fixture
def live_reader(tmp_path, seam):
    _write_cache(str(tmp_path / "c"), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 180))
    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path / "c"))
    TC.set_market_condition_context_resolver(
        live.LiveMarketConditionResolver("ohlcv-v1", reader=reader))
    return reader


def test_the_wrapper_records_the_windows_a_replay_of_the_decision_needs(
        store, live_reader, fixed_clock, monkeypatch, tmp_path):
    """THE ROUND TRIP. A gated live decision, recorded through the real TradeManager wrapper
    and replayed from the exported bundle -- with mock brokers only (the pass body is a stub
    that evaluates one leaf, which is all the gates touch)."""
    from ba2_common.core.market_condition_readers import ReplayMarketConditionReader
    from ba2_common.core.replay import load_bundle
    from ba2_trade_platform.core.TradeManager import TradeManager

    seen = {}

    def body(self, expert_instance_id, lookback_days=1):
        state = live.current_decision()
        seen["recorder"] = state.recorder
        leaf = _leaf("AAA")
        seen["result"] = (leaf.evaluate(), leaf.last_status, leaf.calculated_value)
        return []

    monkeypatch.setattr(TradeManager, "_process_expert_recommendations_after_analysis", body)
    tm = TradeManager.__new__(TradeManager)
    assert tm.process_expert_recommendations_after_analysis(1) == []

    # The decision pass ran INSIDE a capture scope, so the reader was the capturing one.
    assert seen["recorder"] is not None
    assert seen["result"][1] == STATUS_VALID

    exported = store.export_session(SESSION_ID, tmp_path / "export")
    bundle = load_bundle(exported)
    analysis_id = next(a.analysis_id for a in bundle.analyses)
    replay = ReplayMarketConditionReader.from_bundle(
        bundle, analysis_id, profile="ohlcv-v1",
        source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1")
    row = replay.observe("AAA", PRIOR)
    assert row is not None
    live_row = live_reader.observe("AAA", PRIOR)
    assert row.by_field()["underlying_adx_14"] == live_row.by_field()["underlying_adx_14"]


def test_the_wrapper_runs_unchanged_with_capture_off(live_reader, fixed_clock, monkeypatch):
    """Capture off is the default; the wrapper must be the two bare calls it replaces."""
    from ba2_common.core.replay import set_replay_store
    from ba2_trade_platform.core.TradeManager import TradeManager

    set_replay_store(None)
    seen = []
    monkeypatch.setattr(
        TradeManager, "_process_expert_recommendations_after_analysis",
        lambda self, expert_id, lookback_days=1: seen.append(live.current_decision()) or [])
    tm = TradeManager.__new__(TradeManager)
    assert tm.process_expert_recommendations_after_analysis(3) == []
    assert len(seen) == 1 and seen[0] is not None and seen[0].recorder is None


def test_an_open_session_is_required_and_its_absence_is_loud(live_reader, fixed_clock,
                                                             monkeypatch, logs, tmp_path):
    """A pass that silently records nothing is the failure this whole scope exists to avoid."""
    from ba2_common.core.replay import set_replay_store
    from ba2_common.core.replay.service import ReplayStore
    from ba2_trade_platform.core.TradeManager import TradeManager

    st = ReplayStore(tmp_path / "s2", writer="sync")      # no begin_session
    set_replay_store(st)
    try:
        monkeypatch.setattr(TradeManager, "_process_expert_recommendations_after_analysis",
                            lambda self, eid, lookback_days=1: [])
        tm = TradeManager.__new__(TradeManager)
        assert tm.process_expert_recommendations_after_analysis(4) == []
        assert any("not recorded" in m for m in logs.messages())
    finally:
        set_replay_store(None)
        st.close(timeout=5.0)


def test_a_covered_symbol_keeps_its_own_answer(pinned, seam, fixed_clock, monkeypatch):
    """The coverage refusal must apply ONLY to the symbols the snapshot omits: a covered
    symbol still reads its row and its own status."""
    monkeypatch.setattr(live, "gated_live_universe", lambda: (("AAA", "BBB"), ()))
    TC.set_market_condition_context_resolver(pinned)
    with live.market_condition_decision_scope():
        leaf = _leaf("AAA")
        assert leaf.evaluate() is True
        assert leaf.last_status == STATUS_VALID
    assert pinned.no_context_reason_for("AAA") is None
    assert pinned.no_context_reason_for("BBB")
