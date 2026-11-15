"""Which session does a LIVE iv-v1 gate ask its chain reader for? Exactly the one the backtest uses
for that decision (review 2026-11-15, item 11): Monday morning (prior = Friday), the day after a
holiday, a half day as the prior session, a pass after the close, a pass after midnight UTC.

The live side runs through the REAL decision scope and a real iv condition; the reader is a chain
reader that records the session it is asked for. The backtest side is ``backtest_decision_label`` +
``decision_data_session`` for the bar whose decision label is the live label.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import backtest_decision_label, decision_data_session, regular_sessions_ending_at
from ba2_common.core.market_condition_iv_live import ChainLiveReader, IvEvidence
from ba2_common.core.market_conditions import IV_PROFILE, STATUS_NO_CONTEXT, FeatureRow, PROFILES
from ba2_common.core.types import ExpertEventType

NY = ZoneInfo("America/New_York")


class _Recording(ChainLiveReader):
    option_source = "alphavantage"

    def __init__(self):
        super().__init__(IV_PROFILE)
        self.asked = []

    def _evidence(self, symbol, session):
        self.asked.append(session)
        return IvEvidence(row=FeatureRow.uniform(PROFILES[IV_PROFILE], STATUS_NO_CONTEXT, "recording"),
                          failure="recording")


@pytest.fixture
def seam():
    saved = TC.get_market_condition_context_resolver()
    yield
    TC.set_market_condition_context_resolver(saved)


CASES = [
    ("monday morning -> friday", datetime(2025, 11, 17, 10, 0, tzinfo=NY), date(2025, 11, 14)),
    ("day after thanksgiving (half day) -> wednesday", datetime(2025, 11, 28, 10, 0, tzinfo=NY), date(2025, 11, 26)),
    ("monday after the half day -> the half day", datetime(2025, 12, 1, 10, 0, tzinfo=NY), date(2025, 11, 28)),
    ("day after independence day -> the half day before it", datetime(2025, 7, 7, 10, 0, tzinfo=NY), date(2025, 7, 3)),
    ("after the close -> still the prior session", datetime(2025, 11, 17, 16, 30, tzinfo=NY), date(2025, 11, 14)),
    ("after midnight UTC, still monday in NY", datetime(2025, 11, 18, 1, 30, tzinfo=timezone.utc), date(2025, 11, 14)),
]


@pytest.mark.parametrize("name,moment,want", CASES, ids=[c[0] for c in CASES])
def test_live_asks_for_the_session_the_backtest_reads(name, moment, want, seam, monkeypatch):
    reader = _Recording()
    resolver = live.LiveMarketConditionResolver(IV_PROFILE, reader=reader)
    TC.set_market_condition_context_resolver(resolver)
    monkeypatch.setattr(live, "_replay_now", lambda: moment)
    with live.market_condition_decision_scope():
        leaf = TC.create_condition(ExpertEventType.N_UNDERLYING_IV_RV_RATIO, object(), "AAA", None,
                                   operator_str=">", value=1.0)
        assert leaf.evaluate() is False
    assert reader.asked == [want]
    # the backtest: the bar whose decision label is this live decision's label reads the same session
    from ba2_common.core.market_calendar import live_decision_label
    label = live_decision_label(moment)
    bars = [d for d in regular_sessions_ending_at(label, 6) if backtest_decision_label(d) == label]
    assert bars and decision_data_session(backtest_decision_label(bars[-1])) == want
