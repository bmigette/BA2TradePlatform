"""BT/live market-condition SESSION parity (plan ``docs/plans/2026-09-22-bt-live-option-parity.md``
§0 and Task A4).

THE CLAIM. A backtest bar D decides with data through D's close and fills on the next bar, so it
IS the live decision made during the next regular session N(D). A live decision during session S
and the backtest bar ``prior_regular_session(S)`` must therefore build the same market-condition
context: the same ``session_label``, the same ``prior_session``, and -- through the same reader --
the very same row object. Anything else means one path gates on data the other never sees.

THE BUG THIS WOULD HAVE CAUGHT. Until 2026-09-22 the backtest used the bar date AS the decision
label and read ``prior_regular_session(bar)``: one session staler than the live decision it
stands for. Every assertion below on ``prior_session`` failed by exactly one session.

Both sides are the REAL objects: ``LiveMarketConditionResolver.begin_decision`` (clock patched,
no capture context, so the plain reader is served) and ``BacktestMarketConditionResolver`` over a
fake account clock -- sharing one ``DictMarketConditionReader`` whose rows are distinct objects per
session, so "the identical row" is an identity check, not an equality that two copies could pass.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ba2_common.core import market_condition_live as live  # noqa: E402
from ba2_common.core.market_calendar import (  # noqa: E402
    NY_TZ, prior_regular_session, regular_session_dates,
)
from ba2_common.core.market_condition_context import DictMarketConditionReader  # noqa: E402
from ba2_common.core.market_conditions import PROFILES  # noqa: E402

from app.services.backtest.market_condition_bt import BacktestMarketConditionResolver  # noqa: E402

PROFILE = "ohlcv-v1"
SYMBOL = "AAA"

#: The sessions the plan names, each with the calendar case it exercises.
NAMED_SESSIONS = {
    date(2024, 1, 2): "day after a holiday (New Year)",
    date(2024, 7, 5): "day after a half-day AND a holiday (Jul 3 half, Jul 4 closed)",
    date(2024, 12, 2): "day after a half-day (Black Friday 2024-11-29), a Monday",
    date(2025, 1, 21): "day after a holiday (MLK), a Tuesday",
    date(2025, 12, 26): "day after a half-day AND a holiday (Dec 24 half, Dec 25 closed)",
    date(2024, 3, 4): "a Monday",
    date(2025, 6, 3): "a Tuesday",
}

#: Every session S in 2024-2025 (the plan's range); the BT bar prior(S) reaches back into 2023.
ALL_SESSIONS = regular_session_dates(date(2024, 1, 1), date(2025, 12, 31))


class _Reader(DictMarketConditionReader):
    """The dict reader, carrying what a live resolver asks of any reader (profile, calc
    version, no mapped snapshot -> no coverage check)."""

    profile = PROFILE
    calc_version = PROFILES[PROFILE].calc_version
    mapped_reader = None


@pytest.fixture(scope="module")
def reader():
    days = regular_session_dates(date(2023, 12, 1), date(2025, 12, 31))
    # A distinct object per session: an identity check below cannot pass on a shared row.
    return _Reader({(SYMBOL, d): SimpleNamespace(session=d) for d in days})


class _Account:
    def __init__(self, day):
        self.day = day

    def _as_of_date(self):
        return self.day


def _live_context(reader, moment, monkeypatch):
    resolver = live.LiveMarketConditionResolver(PROFILE, reader=reader)
    monkeypatch.setattr(live, "_replay_now", lambda: moment)
    state = resolver.begin_decision()
    assert state.reader is reader, "a capture context leaked in: the live side must read plainly"
    return state.context()


def _bt_context(reader, bar):
    return BacktestMarketConditionResolver(reader)(_Account(bar), SYMBOL, None)


def _ny(day, hh, mm):
    return datetime.combine(day, time(hh, mm), tzinfo=NY_TZ)


def _assert_same(live_ctx, bt_ctx, reader, what):
    assert live_ctx.prior_session == bt_ctx.prior_session, what
    assert live_ctx.session_label == bt_ctx.session_label, what
    live_row = live_ctx.reader.observe(SYMBOL, live_ctx.prior_session)
    bt_row = bt_ctx.reader.observe(SYMBOL, bt_ctx.prior_session)
    assert live_row is not None, what
    assert live_row is bt_row, what


@pytest.mark.parametrize("session", sorted(NAMED_SESSIONS), ids=lambda d: d.isoformat())
def test_a_live_decision_at_0935_and_the_backtest_bar_before_it_read_the_same_row(
        session, reader, monkeypatch):
    what = f"{session} ({NAMED_SESSIONS[session]})"
    live_ctx = _live_context(reader, _ny(session, 9, 35), monkeypatch)
    bar = prior_regular_session(session)
    bt_ctx = _bt_context(reader, bar)
    _assert_same(live_ctx, bt_ctx, reader, what)
    # The row is the BAR's own session -- the completed session before the live decision.
    assert bt_ctx.prior_session == bar, what
    assert live_ctx.session_label == session, what


@pytest.mark.parametrize("session", sorted(NAMED_SESSIONS), ids=lambda d: d.isoformat())
def test_a_live_decision_after_the_close_still_reads_the_prior_session(
        session, reader, monkeypatch):
    """``prior_session_v1`` is a POLICY: a live decision at 16:30 ET on S -- after S closed, S's
    bar already complete -- still reads prior(S), exactly what the backtest bar prior(S) reads.
    Reading S itself there would be a live-only advantage the backtest never had."""
    what = f"{session} 16:30 ET ({NAMED_SESSIONS[session]})"
    live_ctx = _live_context(reader, _ny(session, 16, 30), monkeypatch)
    bt_ctx = _bt_context(reader, prior_regular_session(session))
    _assert_same(live_ctx, bt_ctx, reader, what)
    assert live_ctx.prior_session == prior_regular_session(session), what


def test_every_session_of_2024_and_2025_is_in_parity_at_the_open_and_after_the_close(
        reader, monkeypatch):
    """The sweep behind the named cases: no calendar corner of the two years escapes."""
    for session in ALL_SESSIONS:
        bt_ctx = _bt_context(reader, prior_regular_session(session))
        for hh, mm in ((9, 35), (16, 30)):
            live_ctx = _live_context(reader, _ny(session, hh, mm), monkeypatch)
            _assert_same(live_ctx, bt_ctx, reader, f"{session} {hh:02d}:{mm:02d} ET")


@pytest.mark.parametrize("moment", [
    datetime(2025, 6, 9, 9, 35, tzinfo=NY_TZ),                 # Monday open
    datetime(2025, 6, 10, 23, 59, tzinfo=NY_TZ),               # late evening ET
    datetime(2025, 6, 11, 2, 0, tzinfo=timezone.utc),          # UTC already the next day
    datetime(2025, 6, 14, 12, 0, tzinfo=NY_TZ),                # Saturday
    datetime(2025, 7, 4, 10, 0, tzinfo=NY_TZ),                 # a holiday
    datetime(2025, 11, 28, 15, 0, tzinfo=NY_TZ),               # after a half-day's close
], ids=lambda m: m.isoformat())
def test_the_live_state_s_label_and_data_session_are_unchanged_by_the_one_rule_rename(
        moment, reader, monkeypatch):
    """``DecisionState`` now builds its label with ``live_decision_label`` and its data session
    with ``decision_data_session`` (plan A3, one rule for both paths). That is a rename: the
    context equals what ``prior_regular_session(instant)`` and the NY date always gave."""
    ctx = _live_context(reader, moment, monkeypatch)
    assert ctx.session_label == moment.astimezone(NY_TZ).date()
    assert ctx.prior_session == prior_regular_session(moment)


def test_a_naive_live_decision_time_is_still_refused(reader):
    state = live.DecisionState(resolver=live.LiveMarketConditionResolver(PROFILE, reader=reader),
                               decision_time=datetime(2025, 6, 9, 9, 35), reader=reader)
    with pytest.raises(ValueError, match="timezone-aware"):
        state.context()
