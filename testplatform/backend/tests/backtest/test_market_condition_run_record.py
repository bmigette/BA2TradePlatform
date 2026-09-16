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
    MarketConditionRunRecord, apply_market_condition_block, attach_entry_states,
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
    return MarketConditionRunRecord(_Resolver(rows, SESSION, PRIOR), profiles=["ohlcv-v1"],
                                    manifests={"ohlcv-v1": "sha256:" + "b" * 64})


# --------------------------------------------------------------------------- counters
def test_the_binding_counters_are_ABSENT_until_the_binding_has_run(record):
    """Not zeros. "No structure was bound across a gap" and "the binding has not happened yet"
    are different facts, and a blob assembled without it -- an engine-level test, a caller that
    never reaches the handler -- must not read as a run with a perfect same-session binding."""
    assert record.binding is None
    stats = record.stats()
    for key in ("bound_same_session", "bound_with_gap", "ambiguous"):
        assert key not in stats, key
    record.binding = attach_entry_states(
        [{"underlying_symbol": "AAA", "entry_time": f"{SESSION.isoformat()}T00:00:00"}],
        _states(("AAA", SESSION.isoformat())))
    assert record.stats()["bound_same_session"] == 1


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


def test_a_symbol_with_no_row_records_the_STATUS_the_gate_would_have_reported(record):
    """A gate that could not measure is still a fact about the entry, and WHICH failure it was
    is the fact that matters: an uncovered symbol produces exactly this for a whole run. An
    empty dict would be indistinguishable in the report from a run that predates the capture."""
    record.note_entry(object(), "ZZZ", object())
    values = record.entry_states()[0]["values"]
    assert set(values) == set(FIELDS)
    assert all(v == {"value": None, "status": "missing_session"} for v in values.values())


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
    # PLURAL since Task 10: a run can pin more than one profile, every one is its own warmed
    # snapshot, and there is no single "the manifest"/"the calc version" true of two of them.
    assert block["profiles"] == ["ohlcv-v1"]
    assert block["manifests"] == {"ohlcv-v1": "sha256:" + "b" * 64}
    assert block["calc_versions"] == {"ohlcv-v1": SPEC.calc_version}
    assert block["source_profile"] == "fmp-daily-split-adjusted-v1"


# --------------------------------------------------------------------------- attaching
def _states(*pairs):
    return [{"symbol": sym, "session": s, "prior_session": s, "values": {"x": {"value": i}}}
            for i, (sym, s) in enumerate(pairs)]


def _leg(symbol, entry, *, txn=None, contract=None):
    return {"underlying_symbol": symbol, "entry_time": f"{entry}T00:00:00",
            "transaction_id": txn, "contract_symbol": contract}


def test_a_fill_after_the_decision_bar_still_gets_the_decision_s_state():
    states = _states(("AAA", "2024-03-05"))
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-03-08T14:30:00"}]
    out = attach_entry_states(trades, states)
    assert out["attached"] == 1 and out["with_gap"] == 1 and out["same_session"] == 0
    assert trades[0]["entry_state"]["session"] == "2024-03-05"
    # THE GAP IS IN THE DATA. A reader looking at a surprising bin can see which rows were
    # bound on the decision's own session and which were inferred across days.
    assert trades[0]["entry_state"]["gap_days"] == 3
    assert "ambiguous" not in trades[0]["entry_state"]


def test_a_same_session_fill_records_a_zero_gap_and_no_ambiguity():
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-03-05T14:30:00"}]
    out = attach_entry_states(trades, _states(("AAA", "2024-03-05")))
    assert out == {"attached": 1, "same_session": 1, "with_gap": 0, "ambiguous": 0}
    assert trades[0]["entry_state"]["gap_days"] == 0


def test_two_decisions_in_one_week_make_the_binding_AMBIGUOUS_and_say_so():
    """THE CASE THE BOUND ALONE CANNOT SETTLE. ``note_entry`` records a state whenever an entry
    RULE fires -- including Monday's decision, which the dup-position or equity gate then
    stopped, so it produced no order. Wednesday's decision produced Thursday's fill. Both sit
    inside Thursday's 7-day window, so choosing the later one is a judgement, and the row says
    so instead of presenting it as the measurement behind that trade."""
    states = _states(("AAA", "2024-03-04"), ("AAA", "2024-03-06"))   # Monday, Wednesday
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-03-07T14:30:00"}]  # Thursday
    out = attach_entry_states(trades, states)
    assert out["attached"] == 1 and out["ambiguous"] == 1 and out["with_gap"] == 1
    state = trades[0]["entry_state"]
    assert state["session"] == "2024-03-06"      # the latest at-or-before is still the answer
    assert state["gap_days"] == 1
    assert state["ambiguous"] is True
    # ...and with only ONE decision in the window the flag is absent, not False: an absent key
    # is what a reader scanning for problems can grep for.
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-03-07T14:30:00"}]
    attach_entry_states(trades, _states(("AAA", "2024-03-06")))
    assert "ambiguous" not in trades[0]["entry_state"]


def test_the_ambiguity_counts_reach_the_run_stats(record):
    record.resolver.session = record.resolver.prior = None       # not used by this path
    record.binding = attach_entry_states(
        [{"underlying_symbol": "AAA", "entry_time": "2024-03-07T00:00:00"}],
        _states(("AAA", "2024-03-04"), ("AAA", "2024-03-06")))
    stats = record.stats()
    assert stats["ambiguous"] == 1
    assert stats["bound_with_gap"] == 1
    assert stats["bound_same_session"] == 0


def test_each_entry_gets_ITS_OWN_state_not_the_first_or_the_last():
    states = _states(("AAA", "2024-01-05"), ("AAA", "2024-06-05"))
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-01-08T00:00:00"},
              {"underlying_symbol": "AAA", "entry_time": "2024-06-07T00:00:00"}]
    assert attach_entry_states(trades, states)["ambiguous"] == 0
    assert trades[0]["entry_state"]["session"] == "2024-01-05"
    assert trades[1]["entry_state"]["session"] == "2024-06-05"


def test_a_position_opened_LONG_after_the_last_decision_inherits_NOTHING():
    """THE FABRICATION THIS PREVENTS. The match is by date, not identity, so without a bound
    an assignment or a lifecycle roll months later would inherit the last rule-fired state and
    the attribution table would report a measured regime for a trade no measurement produced."""
    states = _states(("AAA", "2024-01-05"))
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-06-01T00:00:00"}]
    assert attach_entry_states(trades, states)["attached"] == 0
    assert "entry_state" not in trades[0]


@pytest.mark.parametrize("entry,attached", [
    ("2024-01-05", 1),      # the decision bar itself
    ("2024-01-12", 1),      # exactly the bound
    ("2024-01-13", 0),      # one day past it
])
def test_the_gap_bound_is_inclusive_and_is_the_documented_constant(entry, attached):
    from app.services.backtest.market_condition_bt import ENTRY_STATE_MAX_GAP_DAYS

    assert ENTRY_STATE_MAX_GAP_DAYS == 7
    trades = [{"underlying_symbol": "AAA", "entry_time": f"{entry}T00:00:00"}]
    assert attach_entry_states(trades, _states(("AAA", "2024-01-05")))["attached"] == attached


def test_an_unparseable_entry_date_is_not_a_match():
    trades = [{"underlying_symbol": "AAA", "entry_time": "not-a-date"}]
    assert attach_entry_states(trades, _states(("AAA", "2024-01-05")))["attached"] == 0
    assert "entry_state" not in trades[0]


def test_a_multi_leg_structure_stores_the_state_ONCE(record):
    """One decision, one measurement. Four copies of the same ~200-byte dict in the persisted
    trades blob is three copies of nothing, on every individual of every generation."""
    legs = [_leg("AAA", "2024-03-05", txn=77, contract=f"AAA240419C0011000{i}")
            for i in range(4)]
    out = attach_entry_states(legs, _states(("AAA", "2024-03-05")))
    assert out["attached"] == 1                  # the STRUCTURE, not its four rows
    assert "entry_state" in legs[0]
    assert all("entry_state" not in leg for leg in legs[1:])


def test_legs_of_DIFFERENT_structures_each_get_their_own():
    legs = [_leg("AAA", "2024-03-05", txn=1, contract="AAA240419C00110000"),
            _leg("AAA", "2024-03-05", txn=2, contract="AAA240419C00120000")]
    assert attach_entry_states(legs, _states(("AAA", "2024-03-05")))["attached"] == 2


def test_a_trade_with_no_record_before_it_is_left_UNTOUCHED():
    """An assignment, or a position opened by something other than a gated entry rule. An
    empty dict would let the report bin it as though it had been measured."""
    trades = [{"underlying_symbol": "AAA", "entry_time": "2024-01-01T00:00:00"}]
    assert attach_entry_states(trades, _states(("AAA", "2024-03-05")))["attached"] == 0
    assert "entry_state" not in trades[0]


def test_an_equity_row_matches_on_its_own_symbol_and_an_option_leg_on_its_underlying():
    states = _states(("AAA", "2024-03-05"))
    trades = [{"symbol": "AAA", "entry_time": "2024-03-06T00:00:00"},
              {"symbol": "AAA240419C00100000", "underlying_symbol": "AAA",
               "entry_time": "2024-03-06T00:00:00"}]
    assert attach_entry_states(trades, states)["attached"] == 2


def test_attaching_nothing_to_nothing_is_not_an_error():
    assert attach_entry_states([], [])["attached"] == 0
    assert attach_entry_states(None, None)["attached"] == 0


# --------------------------------------------------------------------------- the blob
def test_a_profile_less_run_gets_NO_key_at_all(record):
    """Not an empty block, not a null: the key must be ABSENT, or the byte-identical
    comparison in the all-off gate fails on the payload SHAPE rather than on any number."""
    results = {"trades": [], "total_return": 1.0}
    assert apply_market_condition_block(results, None) is results
    assert "market_condition" not in results
    assert results == {"trades": [], "total_return": 1.0}


def test_the_block_lands_on_the_results_and_the_states_land_on_the_trades(record):
    record.note_eligible()
    record.note_entry(object(), "AAA", object())
    results = {"trades": [{"symbol": "AAA", "entry_time": f"{SESSION.isoformat()}T15:00:00"},
                          {"symbol": "ZZZ", "entry_time": f"{SESSION.isoformat()}T15:00:00"}]}
    apply_market_condition_block(results, record)
    block = results["market_condition"]
    assert block["stats"]["structures_with_entry_state"] == 1
    assert block["stats"]["bound_same_session"] == 1
    assert results["trades"][0]["entry_state"]["values"]["underlying_adx_14"]["value"] == 20.0
    assert "entry_state" not in results["trades"][1]
    # The metadata is added AFTER the metrics; it must not have grown a metric of its own.
    assert set(block) == {"profiles", "manifests", "calc_versions", "source_profile",
                          "timing_policy", "stats"}
