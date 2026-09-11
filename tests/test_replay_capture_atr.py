"""ATR sizing under capture: the read that sizes a live position is recorded.

``get_latest_atr`` is the last un-recorded input to a live trade: the risk
managers size on it, and until the second delivery it fetched with an inline
``datetime.now()`` against an un-tapped indicator provider -- so the number that
decided a quantity left no trace, and the request that produced it could never be
reproduced (a wall clock in a request identity is unmatchable by construction).

SCOPE. This records only while a capture scope is open around the sizing call.
Today the scopes are per-ANALYSIS, and sizing runs after one closes, so a live ATR
read is tapped but usually unrecorded; the decision-and-execution trace (spec step
6, Task D) is what opens a scope around the decision path. What is pinned here is
that the read goes through the seam and produces a reproducible request whenever a
scope IS open -- not that today's live platform records it.

Two properties, and one guard:

1. a live call (no ``end_date``) records ONE clock read and ONE indicator
   observation, and the observation's ``end_date`` IS that recorded read;
2. an as-of call (``end_date`` given) is unchanged -- the value is passed
   through, no clock is read, and the identity carries the caller's date;
3. with no capture context the wrapper is a passthrough: same value, one fetch.
"""
from datetime import datetime, timezone

from ba2_common.core.interfaces.MarketIndicatorsInterface import MarketIndicatorsInterface
from ba2_common.core.position_sizing import get_latest_atr
from ba2_common.core.replay import (
    CaptureContext,
    CaptureHealth,
    current_capture,
    use_capture_context,
)

NOW = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)


class _Indicators(MarketIndicatorsInterface):
    """A real MarketIndicatorsInterface implementation over a canned ATR series.

    Subclassing the interface is the point: that is where the tap is applied, so a
    fake that merely quacked like an indicator provider would test nothing.
    """

    def __init__(self):
        self.calls = []

    def get_indicator(self, symbol, indicator, start_date=None, end_date=None,
                      lookback_days=None, interval="1d", format_type="markdown",
                      period=None):
        self.calls.append({"symbol": symbol, "indicator": indicator,
                           "end_date": end_date, "period": period,
                           "interval": interval, "lookback_days": lookback_days})
        return {"symbol": symbol, "indicator": indicator,
                "values": [2.0, 2.5], "dates": ["2026-09-10", "2026-09-11"]}

    def get_supported_indicators(self):
        return ["atr"]

    def get_provider_name(self):
        return "test"

    def get_supported_features(self):
        return ["atr"]

    def validate_config(self):
        return True

    def _format_as_dict(self, data):
        return data

    def _format_as_markdown(self, data):
        return str(data)


def _context():
    return CaptureContext(
        analysis_meta={"analysis_id": "A1", "attempt_id": "T1", "session_id": "S1",
                       "expert_class": "TestExpert", "expert_instance_id": 7,
                       "symbol": "AAPL", "use_case": "enter_market",
                       "scheduled_at": None, "started_at": NOW},
        health=CaptureHealth())


def _observations(context):
    return [pending.observation for pending in context.observations]


def test_a_live_atr_read_records_one_clock_read_and_one_indicator_observation():
    provider = _Indicators()
    context = _context()
    with use_capture_context(context):
        atr = get_latest_atr("AAPL", provider, period=14)

    assert atr == 2.5, "the tap must not change the value the sizing uses"
    assert len(provider.calls) == 1, "capture added a second indicator fetch"

    reads = list(context.build_record().clock_reads)
    assert len(reads) == 1, (
        "the ATR window's 'now' must go through the evaluation-clock seam")

    observed = _observations(context)
    assert len(observed) == 1
    one = observed[0]
    assert (one.provider, one.method) == ("indicators", "get_indicator")
    assert one.request_identity == {
        "provider": "_Indicators", "symbol": "AAPL", "indicator": "atr",
        "period": 14, "interval": "1d", "start_date": None,
        "end_date": reads[0], "lookback_days": 60, "format_type": "dict",
    }, ("the recorded request must be reproducible: its end_date is the RECORDED "
        "clock read, not a wall clock nobody wrote down -- and every other argument "
        "that selects a different answer is in the identity with it")
    assert context.observations[0].payload["values"] == [2.0, 2.5]
    assert context.capture_failures == {}


def test_an_as_of_atr_read_is_unchanged_and_reads_no_clock():
    """The backtest caller passes its simulated date; nothing about that moves."""
    as_of = datetime(2024, 3, 15, tzinfo=timezone.utc)
    provider = _Indicators()
    context = _context()
    with use_capture_context(context):
        atr = get_latest_atr("AAPL", provider, period=20, interval="1d", end_date=as_of)

    assert atr == 2.5
    assert provider.calls[0]["end_date"] == as_of, (
        "the simulated as-of date must reach the provider untouched -- a wall clock "
        "here leaks today's ATR into a historical bar")
    assert list(context.build_record().clock_reads) == [], (
        "a call that was GIVEN its evaluation time must not read a clock")
    assert _observations(context)[0].request_identity["end_date"] == as_of.isoformat()


def test_the_atr_read_is_a_plain_call_with_no_capture_context():
    assert current_capture() is None
    provider = _Indicators()
    assert get_latest_atr("AAPL", provider, period=14) == 2.5
    assert len(provider.calls) == 1


def test_a_missing_indicator_provider_is_still_the_documented_none():
    """Capture must not turn the no-provider path into anything else."""
    context = _context()
    with use_capture_context(context):
        assert get_latest_atr("AAPL", None, period=14) is None
    assert _observations(context) == []
    assert list(context.build_record().clock_reads) == [], (
        "there is no request to date when there is no provider to ask")
