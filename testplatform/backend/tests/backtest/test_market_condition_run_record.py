"""``MarketConditionRunRecord`` and ``attach_entry_states`` -- the per-run counters (design 6)
and the trade -> entry-state binding the attribution report reads.

The binding is the part worth pinning hardest. An option entry is SUBMITTED on the decision
bar and FILLS on a later one (``backtest_account._option_order_day`` records the submission
date; ``refresh_orders`` fills when a bar allows it), so a trade's ``entry_time`` is not the
session the gate measured. Matching on equality would attach nothing at all for options --
silently, since an absent key just means "no state recorded" -- and the attribution tables
would come out empty for exactly the structures the profile exists to gate.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ba2_common.core.market_condition_context import (  # noqa: E402
    DictMarketConditionReader, MarketConditionContext,
)
from ba2_common.core.market_conditions import (  # noqa: E402
    PROFILES, STATUS_INSUFFICIENT_HISTORY, STATUS_VALID, FeatureRow, Observation,
)

from app.services.backtest.market_condition_bt import (  # noqa: E402
    MarketConditionRunRecord, attach_entry_states,
)

SPEC = PROFILES["ohlcv-v1"]
FIELDS = [f.name for f in SPEC.fields]


def _row(**values):
    obs = {}
    for name in FIELDS:
        value = values.get(name)
        obs[name] = (Observation(value=float(value), status=STATUS_VALID) if value is not None
                     else Observation(value=None, status=STATUS_INSUFFICIENT_HISTORY,
                                      reason="young listing"))
    return FeatureRow(values=obs, calc_versions={name: SPEC.calc_version for name in FIELDS})


class _Resolver:
    """The shape ``BacktestMarketConditionResolver`` presents to the record: callable, and
    carrying a ``reader``/``source_profile`` for the metadata block."""

    source_profile = "fmp-daily-split-adjusted-v1"

    def __init__(self, rows, session, prior):
        self.reader = DictMarketConditionReader(rows)
        self.reader.calc_version = SPEC.calc_version
        self.session, self.prior = session, prior
        self.calls = 0

    def __call__(self, account, symbol, recommendation):
        self.calls += 1
        return MarketConditionContext(
            decision_time=datetime(self.session.year, self.session.month, self.session.day,
                                   21, tzinfo=timezone.utc),
            session_label=self.session, prior_session=self.prior,
            source_profile=self.source_profile, timing_policy="prior_session_v1",
            calc_version=SPEC.calc_version, reader=self.reader)


SESSION, PRIOR = date(2024, 3, 5), date(2024, 3, 4)


@pytest.fixture
def record():
    rows = {("AAA", PRIOR): _row(**dict(zip(FIELDS, (0.05, 20.0, 0.9))))}
    return MarketConditionRunRecord(_Resolver(rows, SESSION, PRIOR), profile="ohlcv-v1",
                                    manifest_digest="sha256:" + "b" * 64)


# --------------------------------------------------------------------------- counters
def test_a_recommendation_with_no_market_leaf_is_counted_nowhere_but_eligible(record):
    record.note_eligible()
    record.note_conditions([{"event_type": "confidence", "condition_result": True}])
    stats = record.stats()
    assert stats["eligible_recommendations"] == 1
    assert stats["market_evaluated"] == 0
    assert stats["market_gate_rejected"] == 0
    assert stats["market_unknown_input_by_reason"] == {}


def test_a_measured_refusal_and_an_unknown_input_are_counted_apart(record):
    """Design 6/7: a threshold miss and a missing feature row are the same False at the gate
    and must never be the same number in the report."""
    record.note_conditions([{"market_condition_status": STATUS_VALID, "condition_result": False}])
    record.note_conditions([{"market_condition_status": STATUS_INSUFFICIENT_HISTORY,
                             "condition_result": False}])
    record.note_conditions([{"market_condition_status": "missing_session",
                             "condition_result": False}])
    stats = record.stats()
    assert stats["market_evaluated"] == 3
    assert stats["market_gate_rejected"] == 1
    assert stats["market_unknown_recommendations"] == 2
    assert stats["market_unknown_input_by_reason"] == {"insufficient_history": 1,
                                                       "missing_session": 1}
    assert stats["market_gate_passed"] == 0


def test_a_recommendation_every_gate_passed_counts_once_as_passed(record):
    record.note_conditions([{"market_condition_status": STATUS_VALID, "condition_result": True},
                            {"market_condition_status": STATUS_VALID, "condition_result": True}])
    stats = record.stats()
    assert stats["market_evaluated"] == 1
    assert stats["market_gate_passed"] == 1
    assert stats["market_leaf_evaluations"] == 2


def test_one_recommendation_rejected_AND_unknown_counts_in_both_not_twice_as_evaluated(record):
    record.note_conditions([{"market_condition_status": STATUS_VALID, "condition_result": False},
                            {"market_condition_status": "missing_session",
                             "condition_result": False}])
    stats = record.stats()
    assert stats["market_evaluated"] == 1
    assert stats["market_gate_rejected"] == 1
    assert stats["market_unknown_recommendations"] == 1


# --------------------------------------------------------------------------- entry state
def test_the_entry_state_is_the_row_at_the_PRIOR_session(record):
    record.note_entry(object(), "AAA", object())
    state = record.entry_states()[0]
    assert state["session"] == SESSION.isoformat()
    assert state["prior_session"] == PRIOR.isoformat()
    assert state["values"]["underlying_adx_14"] == {"value": 20.0, "status": STATUS_VALID}


def test_a_second_entry_in_the_same_session_does_not_re_read_or_duplicate(record):
    record.note_entry(object(), "AAA", object())
    record.note_entry(object(), "AAA", object())
    assert len(record.entry_states()) == 1
    assert record.stats()["entries_staged"] == 2


def test_a_symbol_with_no_row_records_an_EMPTY_state_rather_than_none_at_all(record):
    """A gate that could not measure is still a fact about the entry; dropping the record
    would make the trade look like one the profile never saw."""
    record.note_entry(object(), "ZZZ", object())
    assert record.entry_states()[0]["values"] == {}


def test_a_resolver_that_raises_is_recorded_as_a_failure_not_a_silent_gap(record, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("reader exploded")

    record.resolver = boom
    record.note_entry(object(), "AAA", object())
    assert record.stats()["entry_read_failures"] == 1
    assert record.entry_states() == []


def test_the_metadata_block_carries_no_entry_states(record):
    """They are attached to the trades they explain; a second copy in every trial's result
    dict is bytes over the wire for data the report reads off the trades anyway."""
    record.note_entry(object(), "AAA", object())
    block = record.as_dict()
    assert "entry_states" not in block
    assert block["profile"] == "ohlcv-v1"
    assert block["calc_version"] == SPEC.calc_version
    assert block["source_profile"] == "fmp-daily-split-adjusted-v1"


# --------------------------------------------------------------------------- attaching
def _states(*pairs):
    return [{"symbol": sym, "session": s, "prior_session": s, "values": {"x": {"value": i}}}
            for i, (sym, s) in enumerate(pairs)]


def test_a_fill_after_the_decision_bar_still_gets_the_decision_s_state():
    states = _states(("AAA", "2024-03-05"))
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-03-11T14:30:00"}]
    assert attach_entry_states(trades, states) == 1
    assert trades[0]["entry_state"]["session"] == "2024-03-05"


def test_each_entry_gets_ITS_OWN_state_not_the_first_or_the_last():
    states = _states(("AAA", "2024-01-05"), ("AAA", "2024-06-05"))
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-01-08T00:00:00"},
              {"underlying_symbol": "AAA", "entry_time": "2024-07-01T00:00:00"}]
    attach_entry_states(trades, states)
    assert trades[0]["entry_state"]["session"] == "2024-01-05"
    assert trades[1]["entry_state"]["session"] == "2024-06-05"


def test_a_trade_with_no_record_before_it_is_left_UNTOUCHED():
    """An assignment, or a position opened by something other than a gated entry rule. An
    empty dict would let the report bin it as though it had been measured."""
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-01-01T00:00:00"}]
    assert attach_entry_states(trades, _states(("AAA", "2024-03-05"))) == 0
    assert "entry_state" not in trades[0]


def test_an_equity_row_matches_on_its_own_symbol_and_an_option_leg_on_its_underlying():
    states = _states(("AAA", "2024-03-05"))
    trades = [{"symbol": "AAA", "entry_time": "2024-03-06T00:00:00"},
              {"symbol": "AAA240419C00100000", "underlying_symbol": "AAA",
               "entry_time": "2024-03-06T00:00:00"}]
    assert attach_entry_states(trades, states) == 2


def test_attaching_nothing_to_nothing_is_not_an_error():
    assert attach_entry_states([], []) == 0
    assert attach_entry_states(None, None) == 0
