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


# ---- a measured ZERO is a figure, never "empty" (money-critical) ----------------------------------------
def test_a_zero_buying_power_snapshot_is_a_figure_not_an_empty_answer(slept, logs):
    class _Spent:
        id = 3

        def get_account_snapshot(self):
            return AccountSnapshot(buying_power=0.0)

        def get_account_info(self):
            raise AssertionError("a zero in the snapshot must not fall through to the info probe")

    assert M._get_actual_available_balance(_Spent()) == 0.0
    assert slept == [] and logs.errors == []
    assert M._read_actual_buying_power(_Spent())[0] == "figure"


def test_a_zero_buying_power_in_the_info_dict_is_a_figure_and_does_not_fall_through_to_cash(slept, logs):
    class _Spent:
        id = 3

        def get_account_snapshot(self):
            return AccountSnapshot()                      # the snapshot could not be read

        def get_account_info(self):
            return {"buying_power": 0.0, "cash": 2_500.0}

    assert M._get_actual_available_balance(_Spent()) == 0.0
    assert slept == [] and logs.errors == []


# ---- policy: only an OSError-family error or an empty answer is "could not be read" -------------------
@pytest.mark.parametrize("where", ["snapshot", "info"])
def test_a_non_oserror_from_the_account_propagates_without_a_retry(slept, logs, where):
    """An SDK error class escaping a third-party adapter (alpaca APIError, TastytradeError, httpx errors
    and IBKRError are NOT OSError) is not absorbed: enforce mode propagates what is not named."""
    class _SdkError(Exception):
        pass

    class _Acct:
        id = 5

        def get_account_snapshot(self):
            if where == "snapshot":
                raise _SdkError("sdk says no")
            return AccountSnapshot()

        def get_account_info(self):
            raise _SdkError("sdk says no")

    with pytest.raises(_SdkError):
        M._get_actual_available_balance(_Acct())
    assert slept == [] and logs.errors == []


def test_a_runtime_error_is_no_longer_absorbed(slept):
    """Counterpart of the old pins "a RuntimeError is absorbed, retried, then skipped" (6a22ef8c)."""
    class _Acct:
        id = 5

        def get_account_snapshot(self):
            raise RuntimeError("transient broker error")

    with pytest.raises(RuntimeError):
        M._get_actual_available_balance(_Acct())


@pytest.mark.usefixtures("reset_test_db")
def test_through_get_available_balance_a_propagated_error_is_cannot_size(slept, logs):
    """Expert path: the broad handler of ``_available_balance_breakdown`` turns the propagated error into
    the loud "cannot size" (None) with its own ERROR -- the sizing call is refused, not guessed."""
    from tests import factories
    from tests.test_available_balance_clamp import _BalanceExpert, _resolver_for
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    class _Acct:
        def __init__(self, id_):
            self.id = id_

        def get_balance(self):
            return 100_000.0

        def get_tradable_balance(self):
            return 100_000.0

        def get_instrument_current_price(self, s, price_type="bid"):
            return {} if isinstance(s, (list, tuple, set)) else None

        def get_account_snapshot(self):
            raise RuntimeError("sdk error")

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(account_id=acct_def.id, expert="_BalanceExpert",
                                            virtual_equity_pct=100.0)
    prev = get_instance_resolver()
    try:
        set_instance_resolver(_resolver_for(_Acct(acct_def.id)))
        assert _BalanceExpert(inst.id).get_available_balance() is None
    finally:
        set_instance_resolver(prev)
    assert any("Error calculating available balance" in m and "sdk error" in m for m in logs.errors)


@pytest.mark.usefixtures("reset_test_db")
def test_the_expertless_option_path_refuses_that_one_entry_loudly(slept, logs):
    """``account_available_equity_detail`` (option entry with no expert): a non-defect error from the
    broker read becomes ``(None, (reason,))`` -- the contract every caller already treats as "unknown,
    refuse" -- with ONE ERROR, instead of an uncaught exception aborting the whole entry pass. A
    programming error still propagates."""
    from ba2_common.core.interfaces.MarketExpertInterface import account_available_equity_detail

    class _Acct:
        id = 11

        def __init__(self, exc):
            self._exc = exc

        def option_capital_equity(self):
            return 50_000.0

        def get_account_snapshot(self):
            raise self._exc

    available, names = account_available_equity_detail(_Acct(RuntimeError("sdk error")))
    assert available is None and len(names) == 1
    assert "account 11" in names[0] and "sdk error" in names[0] and "buying power" in names[0]
    assert len(logs.errors) == 1 and "sdk error" in logs.errors[0]
    with pytest.raises(AttributeError):
        account_available_equity_detail(_Acct(AttributeError("x.y")))


# ---- an outage costs a BOUNDED number of broker reads, however many sizing calls it spans ---------------
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(M, "_ACTUAL_BP_CLOCK", staticmethod(c))
    M._BP_FAILED_READS.clear()
    yield c
    M._BP_FAILED_READS.clear()


class _DerivedBroker(_Broker):
    """Alpaca / TastyTrade shape: the snapshot IS a read of ``get_account_info``'s source."""
    snapshot_is_derived_from_account_info = True


def test_an_empty_snapshot_from_a_derived_adapter_does_not_probe_get_account_info(slept, logs, clock):
    broker = _DerivedBroker(outage_reads=99, empty_info=None)
    assert M._get_actual_available_balance(broker, expert_id=1) is None
    assert broker.snapshot_calls == 3 and broker.info_calls == 0           # was 3 + 3
    assert len(logs.errors) == 1


def test_a_snapshot_that_answered_without_a_figure_still_falls_back_to_info(slept, logs, clock):
    class _Partial(_DerivedBroker):
        def get_account_snapshot(self):
            self.snapshot_calls += 1
            return AccountSnapshot(equity=1.0)                 # answered, no buying power

        def get_account_info(self):
            self.info_calls += 1
            return {"buying_power": 77.0}

    broker = _Partial(outage_reads=0, empty_info=None)
    assert M._get_actual_available_balance(broker) == 77.0
    assert broker.snapshot_calls == 1 and broker.info_calls == 1


def test_many_sizing_calls_during_an_outage_make_a_bounded_number_of_reads_and_sleeps(slept, logs, clock):
    """40 sizing decisions (candidates x experts) in one outage: ONE real read cycle (3 snapshot reads,
    0.75 s of sleep in total); the other 39 take the same outcome from the failure memory with no broker
    call and no sleep. Each decision is still LOUD: one ERROR apiece (no silent failure)."""
    broker = _DerivedBroker(outage_reads=10**6, empty_info=None)
    for i in range(40):
        assert M._get_actual_available_balance(broker, expert_id=i) is None
    assert broker.snapshot_calls == 3 and broker.info_calls == 0
    assert slept == list(M._ACTUAL_BP_BACKOFFS_S) and sum(slept) <= 1.0
    assert len(logs.errors) == 40
    assert all("COULD NOT BE READ" in m and "SKIPPED" in m for m in logs.errors)
    assert "remembered" in logs.errors[1] and "remembered" not in logs.errors[0]
    assert "expert 39" in logs.errors[39] and "Account 42" in logs.errors[39]


def test_the_memory_expires_and_a_recovered_broker_clears_it(slept, logs, clock):
    broker = _DerivedBroker(outage_reads=3, empty_info=None)                 # fails exactly one cycle
    assert M._get_actual_available_balance(broker) is None
    clock.t += M._ACTUAL_BP_FAILURE_MEMORY_S - 0.1
    assert M._get_actual_available_balance(broker) is None and broker.snapshot_calls == 3   # still remembered
    clock.t += 0.2                                                           # window over: read again
    assert M._get_actual_available_balance(broker) == 12_345.0
    assert broker.snapshot_calls == 4 and 42 not in M._BP_FAILED_READS       # success cleared the memory
    assert M._get_actual_available_balance(broker) == 12_345.0               # and stays a normal read


def test_a_healthy_account_never_touches_the_failure_memory(slept, logs, clock):
    ok = _Broker(outage_reads=0, empty_info=None)
    for _ in range(5):
        assert M._get_actual_available_balance(ok) == 12_345.0
    assert M._BP_FAILED_READS == {} and slept == [] and logs.errors == []


def test_the_memory_applies_to_a_mandatory_buying_power_account_too(slept, logs, clock):
    class _Ibkr(_Broker):
        buying_power_is_mandatory = True

    dead = _Ibkr(outage_reads=10**6, empty_info={})
    for _ in range(5):
        with pytest.raises(ValueError, match="mandatory"):
            M._get_actual_available_balance(dead)
    assert dead.snapshot_calls == 3


# ---- messages are true in the mandatory (snapshot-only) mode ------------------------------------------
def test_a_non_finite_snapshot_buying_power_on_a_mandatory_account_logs_no_fallback_and_says_unusable(slept, logs):
    class _Ibkr:
        id = 8
        buying_power_is_mandatory = True

        def get_account_snapshot(self):
            return AccountSnapshot(equity=1_000.0, buying_power=float("nan"))

        def get_account_info(self):
            raise AssertionError("snapshot-only mode never reads get_account_info")

    with pytest.raises(ValueError) as e:
        M._get_actual_available_balance(_Ibkr())
    assert not [w for w in logs.warnings if "falling back" in w]
    assert "unusable" in str(e.value) and "answered without one" not in str(e.value)
    assert "mandatory" in str(e.value)


def test_a_mandatory_account_that_answers_without_a_figure_says_so(slept, logs):
    class _Ibkr:
        id = 8
        buying_power_is_mandatory = True

        def get_account_snapshot(self):
            return AccountSnapshot(equity=1_000.0)

    with pytest.raises(ValueError, match="answered without one"):
        M._get_actual_available_balance(_Ibkr())


def test_a_mandatory_buying_power_account_retries_an_empty_answer_then_refuses(slept, logs):
    class _Ibkr(_Broker):
        buying_power_is_mandatory = True

    flaky = _Ibkr(outage_reads=1, empty_info={})
    assert M._get_actual_available_balance(flaky) == 12_345.0 and flaky.snapshot_calls == 2

    dead = _Ibkr(outage_reads=99, empty_info={})
    with pytest.raises(ValueError, match="mandatory"):
        M._get_actual_available_balance(dead)
    assert dead.snapshot_calls == 3
