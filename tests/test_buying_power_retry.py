"""The buying-power read behind the available-balance clamp (``_get_actual_available_balance``).

Review of 6a22ef8c, findings 2 / 5 / 6. The live adapters SWALLOW a failed broker read and hand back an
EMPTY answer (Alpaca: an all-None ``AccountSnapshot`` and ``get_account_info() -> None``; TastyTrade: an
all-None snapshot and ``{}``; IBKR: ``AccountSnapshot()`` and ``{}``), so a retry that only reacts to an
EXCEPTION never fired on the real adapters. These tests use fakes that RETURN empty answers.

``ba2_common``'s logger does not propagate: logs are captured by attaching a handler.
"""
import logging

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

M = MarketExpertInterface


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.errors, self.warnings = [], []

    def emit(self, record):
        (self.errors if record.levelno >= logging.ERROR else
         self.warnings if record.levelno == logging.WARNING else []).append(record.getMessage())


@pytest.fixture
def logs():
    from ba2_common.logger import logger as ba2_logger
    cap = _Cap()
    ba2_logger.addHandler(cap)
    try:
        yield cap
    finally:
        ba2_logger.removeHandler(cap)


@pytest.fixture
def slept(monkeypatch):
    seen = []
    monkeypatch.setattr(M, "_ACTUAL_BP_SLEEP", staticmethod(seen.append))
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    return seen


class _Broker:
    """An adapter that, like the real ones, never raises on an outage: it returns empty answers.

    ``outage_reads`` reads fail (empty snapshot / ``empty_info``), then the broker answers.
    """
    id = 42

    def __init__(self, outage_reads, empty_info, figure=12_345.0):
        self.outage_reads = outage_reads
        self.empty_info = empty_info
        self.figure = figure
        self.snapshot_calls = 0
        self.info_calls = 0

    def get_account_snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls <= self.outage_reads:
            return AccountSnapshot()
        return AccountSnapshot(equity=50_000.0, buying_power=self.figure)

    def get_account_info(self):
        self.info_calls += 1
        if self.info_calls <= self.outage_reads:
            return self.empty_info
        return {"buying_power": self.figure}


@pytest.mark.parametrize("empty_info", [None, {}], ids=["alpaca-None", "tasty-{}"])
def test_an_empty_answer_is_retried_and_a_recovered_broker_clamps(slept, logs, empty_info):
    broker = _Broker(outage_reads=2, empty_info=empty_info)
    assert M._get_actual_available_balance(broker, expert_id=7) == 12_345.0
    assert broker.snapshot_calls == 3                    # two empty answers, then the real one
    assert len(slept) == 2 and logs.errors == []


@pytest.mark.parametrize("empty_info", [None, {}], ids=["alpaca-None", "tasty-{}"])
def test_a_broker_that_never_answers_gives_one_error_naming_account_expert_and_attempts(slept, logs, empty_info):
    broker = _Broker(outage_reads=99, empty_info=empty_info)
    assert M._get_actual_available_balance(broker, expert_id=7) is None
    assert broker.snapshot_calls == 3
    assert len(logs.errors) == 1
    msg = logs.errors[0]
    assert "Account 42" in msg and "expert 7" in msg and "3 attempts" in msg
    assert "COULD NOT BE READ" in msg and "SKIPPED" in msg


def test_the_answered_but_no_figure_case_is_not_retried_and_says_so(slept, logs):
    class _NoBp:
        id = 9
        calls = 0

        def get_account_snapshot(self):
            type(self).calls += 1
            return AccountSnapshot(equity=1_000.0, cash=1_000.0)      # answered; no buying power

        def get_account_info(self):
            return {"account_number": "X1", "equity": 1_000.0}

    assert M._get_actual_available_balance(_NoBp(), expert_id=3) is None
    assert _NoBp.calls == 1 and slept == []
    assert len(logs.errors) == 1
    assert "ANSWERED" in logs.errors[0] and "publishes no buying power" in logs.errors[0]
    assert "COULD NOT BE READ" not in logs.errors[0]
    assert "Account 9" in logs.errors[0] and "expert 3" in logs.errors[0]


def test_total_delay_under_the_submit_lock_is_at_most_one_second():
    assert M._ACTUAL_BP_ATTEMPTS == 3
    assert len(M._ACTUAL_BP_BACKOFFS_S) == M._ACTUAL_BP_ATTEMPTS - 1
    assert 0 < sum(M._ACTUAL_BP_BACKOFFS_S) <= 1.0


def test_a_network_error_is_retried(slept, logs):
    class _Flaky:
        id = 5
        n = 0

        def get_account_snapshot(self):
            type(self).n += 1
            if type(self).n < 3:
                raise ConnectionError("reset by peer")
            return AccountSnapshot(buying_power=777.0)

        def get_account_info(self):
            return {}

    assert M._get_actual_available_balance(_Flaky()) == 777.0
    assert len(slept) == 2 and logs.errors == []


@pytest.mark.parametrize("exc", [AttributeError("x.y"), TypeError("bad arg")])
def test_a_programming_error_propagates_without_a_retry(slept, logs, exc):
    class _Buggy:
        id = 5

        def get_account_snapshot(self):
            raise exc

    with pytest.raises(type(exc)):
        M._get_actual_available_balance(_Buggy())
    assert slept == []


def test_a_mandatory_buying_power_account_retries_an_empty_answer_then_refuses(slept, logs):
    class _Ibkr(_Broker):
        buying_power_is_mandatory = True

    flaky = _Ibkr(outage_reads=1, empty_info={})
    assert M._get_actual_available_balance(flaky) == 12_345.0 and flaky.snapshot_calls == 2

    dead = _Ibkr(outage_reads=99, empty_info={})
    with pytest.raises(ValueError, match="mandatory"):
        M._get_actual_available_balance(dead)
    assert dead.snapshot_calls == 3
