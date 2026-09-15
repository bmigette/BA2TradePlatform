"""The live host's capture switch, store root, session and batch linkage (spec step 2).

This is the layer that decides WHETHER to record, WHERE, and FOR WHICH INSTANCE.
Four things must hold, because each of them silently breaks something else when
it does not:

* the switch is OFF until someone turns it on, and the setting row exists so it
  CAN be turned on from the UI (there is no migration for AppSetting rows);
* the store lands under this instance's cache folder, not a shared default;
* a session is bounded by the UTC date, so an export names a day rather than
  "everything since the process started";
* the batch id reaches the RECORD and nothing else -- never a trading row.
"""
import os
from datetime import datetime, timezone

import pytest

from ba2_common.core.db import get_setting
from ba2_common.core.replay import get_replay_store
from ba2_trade_platform.core import replay_capture
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AppSetting


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point this instance's cache folder at a temp dir and always tear capture down."""
    import ba2_trade_platform.config as config

    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "db.sqlite"))
    try:
        yield tmp_path
    finally:
        replay_capture.shutdown_replay_capture(timeout=5.0)
        replay_capture.clear_current_batch()


def _enable():
    add_instance(AppSetting(key=replay_capture.CAPTURE_SETTING_KEY, value_str="true"))


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #
def test_missing_setting_is_created_as_false_and_capture_stays_off(cache_root, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    assert get_setting(replay_capture.CAPTURE_SETTING_KEY) is None
    caplog.clear()

    store = replay_capture.initialize_replay_capture()

    assert store is None, "capture must be OFF until someone turns it on"
    assert get_replay_store() is None, "no store means every tap is a passthrough"
    assert get_setting(replay_capture.CAPTURE_SETTING_KEY) == "false", (
        "the row must exist so the switch is reachable from the UI (no migration "
        "creates AppSetting rows)")
    assert not os.path.exists(replay_capture.store_root()), (
        "capture off must not create a store directory")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "the FIRST run legitimately has no setting row; warning about it teaches "
        f"readers to ignore warnings: {[r.message for r in caplog.records]}")


@pytest.mark.parametrize("spelling", ["true", "True", "1", "yes"])
def test_any_true_spelling_turns_capture_on(cache_root, spelling):
    """coerce_bool, not ``== 'true'``: a setting written as 1 means ON."""
    add_instance(AppSetting(key=replay_capture.CAPTURE_SETTING_KEY, value_str=spelling))
    try:
        assert replay_capture.initialize_replay_capture() is not None
    finally:
        replay_capture.shutdown_replay_capture(timeout=5.0)


# --------------------------------------------------------------------------- #
# Where, and describing what was running
# --------------------------------------------------------------------------- #
def test_enabled_opens_the_store_under_this_instances_cache_folder(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()

    assert store is not None
    assert get_replay_store() is store
    expected = os.path.join(str(cache_root / "cache"), "replay", "v1")
    assert str(store.root) == expected
    assert os.path.isdir(expected)
    assert store.writer == "thread", "recording must not wait on disk in a worker"


def test_the_session_describes_the_running_build(cache_root):
    from ba2_trade_platform.version import APP_VERSION

    _enable()
    store = replay_capture.initialize_replay_capture()
    session = store.index.get_session(store.session_id)

    assert session.app_version == APP_VERSION
    assert set(session.package_versions) == {"ba2_common", "ba2_providers", "ba2_experts"}
    assert session.exchange_tz == "America/New_York"
    assert session.instance_id and len(session.instance_id) == 16
    assert str(cache_root) not in session.instance_id, (
        "the instance id must be opaque -- an export must not carry a home directory")
    assert list(session.capabilities["recorded_experts"]) == list(
        replay_capture.RECORDED_EXPERTS)
    # source_revision is either a real revision or an honest None -- never invented.
    assert session.source_revision is None or len(session.source_revision) == 40


def test_two_instances_get_different_ids(cache_root, monkeypatch):
    import ba2_trade_platform.config as config

    first = replay_capture._instance_id()
    monkeypatch.setattr(config, "DB_FILE", str(cache_root / "other.sqlite"))
    assert replay_capture._instance_id() != first


# --------------------------------------------------------------------------- #
# Sessions roll at the UTC date change
# --------------------------------------------------------------------------- #
def test_session_rolls_over_at_the_utc_date_change(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()
    first = store.session_id

    # The rollover check compares the OPEN session's date to today's: pretend the
    # open one belongs to yesterday, exactly as it does after midnight.
    replay_capture._SESSION_DATE = "2020-01-01"
    store.current_session_id()
    second = store.session_id

    assert second != first
    assert store.index.get_session(first).status == "finalized", (
        "the session we rolled away from must be closed, not left open forever")
    assert store.index.get_session(second).status == "open"


def test_the_rollover_check_is_free_on_the_same_day(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()
    first = store.session_id

    for _ in range(5):
        assert store.current_session_id() == first


# --------------------------------------------------------------------------- #
# Every account class's quote read is tapped
# --------------------------------------------------------------------------- #
def _account_classes():
    """Every concrete broker account class the live platform can instantiate."""
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    return [AlpacaAccount, IBKRAccount, TastyTradeAccount]


@pytest.mark.parametrize("account_class", _account_classes(),
                         ids=lambda c: c.__name__)
def test_every_account_classes_quote_read_is_tapped(account_class):
    """An OVERRIDE of get_instrument_current_price silently loses the base tap.

    Inheriting the base method inherits its tap; overriding it replaces both, and
    nothing about that is visible at the call site -- the quote simply stops being
    recorded for that broker. IBKRAccount already did this once. The guard is on
    the resolved attribute, so it holds however the class gets the method.
    """
    method = account_class.get_instrument_current_price
    assert hasattr(method, "__wrapped__"), (
        f"{account_class.__name__}.get_instrument_current_price is not tapped: an "
        f"override must carry @observe_provider, or it must not override at all")


def test_the_ibkr_override_records_the_quote_it_returned(tmp_path):
    """The override's own tap: identity, provenance, and the value untouched."""
    from ba2_common.core.replay import (
        CaptureContext,
        CaptureHealth,
        ReplayStatus,
        use_capture_context,
    )
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount

    # IBKRAccount is itself abstract (it leaves several AccountInterface methods
    # unimplemented), so drive its override through a minimal concrete subclass --
    # which is also what the live registry would have to provide.
    class _ConcreteIBKR(IBKRAccount):
        def _get_instrument_current_price_impl(self, *a, **k):
            raise AssertionError("the override must not fall through to the base impl")

        def _submit_order_impl(self, *a, **k): ...
        def adjust_sl(self, *a, **k): ...
        def adjust_tp(self, *a, **k): ...
        def adjust_tp_sl(self, *a, **k): ...
        def get_balance(self, *a, **k): ...
        def get_order(self, *a, **k): ...
        def get_orders(self, *a, **k): ...
        def refresh_positions(self, *a, **k): ...
        def symbols_exist(self, *a, **k): ...

    account = _ConcreteIBKR.__new__(_ConcreteIBKR)
    account.id = 77
    account._connected = False          # IBKRAccount.__del__ reads it
    account._ensure_connected = lambda: None
    account._create_contract = lambda symbol: object()

    class _Ticker:
        last = 123.5
        close = 120.0

    class _IB:
        def reqMktData(self, contract):
            return _Ticker()

        def sleep(self, seconds):
            pass

    account.ib = _IB()

    context = CaptureContext(
        analysis_meta={
            "analysis_id": "A1", "attempt_id": "T1", "session_id": "S1",
            "expert_class": "X", "expert_instance_id": 1, "symbol": "AAPL",
            "use_case": "enter_market", "scheduled_at": None,
            "started_at": datetime.now(timezone.utc),
        },
        health=CaptureHealth(),
    )
    with use_capture_context(context):
        price = account.get_instrument_current_price("AAPL")

    assert price == 123.5
    observed = [pending.observation for pending in context.observations]
    assert len(observed) == 1
    assert (observed[0].provider, observed[0].method) == (
        "broker", "get_instrument_current_price")
    assert observed[0].request_identity["symbols"] == ["AAPL"]
    assert observed[0].request_identity["account_class"] == "_ConcreteIBKR"
    assert IBKRAccount.get_instrument_current_price.__wrapped__ is not None, (
        "the method under test is IBKR's own tapped override, not the base method")
    assert "price_type" not in observed[0].request_identity, (
        "this override takes no price_type; the record must not invent one")
    assert observed[0].provenance == ReplayStatus.PROVENANCE_NETWORK, (
        "the override keeps no memo -- every call is a broker read")
    assert context.observations[0].payload == 123.5


# --------------------------------------------------------------------------- #
# The rollover must not stall concurrent analyses
# --------------------------------------------------------------------------- #
def test_the_rollover_does_not_hold_the_lock_while_finalizing(cache_root):
    """Finalizing drains the writer. Draining under the lock stalls every worker.

    At the first analysis after midnight UTC one thread rolls the session. If it
    finalizes the OLD session while holding the process-wide lock, every other
    analysis blocks behind it for as long as the writer takes (up to the drain
    timeout) -- a recording detail delaying trading decisions. The new session
    must be visible, and the lock free, before any draining starts.
    """
    import threading

    _enable()
    store = replay_capture.initialize_replay_capture()
    first = store.session_id

    finalize_entered = threading.Event()
    release_finalize = threading.Event()
    finalized = []

    def slow_finalize(session_id, timeout=None):
        finalize_entered.set()
        release_finalize.wait(10.0)
        finalized.append(session_id)
        return 0

    store.finalize_session = slow_finalize
    replay_capture._SESSION_DATE = "2020-01-01"

    roller = threading.Thread(target=replay_capture._roll_session_if_needed)
    roller.start()
    try:
        assert finalize_entered.wait(5.0), "the rollover never reached finalize"

        # While the old session is still being finalized, a SECOND analysis's
        # rollover check must return immediately with the new session.
        done = threading.Event()
        seen = {}

        def second_analysis():
            seen["session"] = store.current_session_id()
            done.set()

        threading.Thread(target=second_analysis).start()
        assert done.wait(3.0), (
            "a concurrent analysis blocked behind the finalize -- the lock is "
            "still held while draining")
        assert seen["session"] != first
        assert seen["session"] == store.session_id
    finally:
        release_finalize.set()
        roller.join(10.0)

    assert finalized == [first], "the previous session must still be finalized"


def test_the_rollover_finalizes_with_a_short_timeout(cache_root):
    """A trading thread waits SECONDS on the writer at most, not the full drain."""
    _enable()
    store = replay_capture.initialize_replay_capture()
    seen = {}

    def record_timeout(session_id, timeout=None):
        seen["timeout"] = timeout
        return 0

    store.finalize_session = record_timeout
    replay_capture._SESSION_DATE = "2020-01-01"
    replay_capture._roll_session_if_needed()

    assert seen["timeout"] == replay_capture.ROLLOVER_DRAIN_TIMEOUT
    assert replay_capture.ROLLOVER_DRAIN_TIMEOUT <= 5.0


# --------------------------------------------------------------------------- #
# Batch linkage
# --------------------------------------------------------------------------- #
def test_batch_id_is_thread_local_and_reaches_the_record(cache_root):
    """The record names the batch; nothing is written to a trading row to get it."""
    import threading

    from ba2_common.core.interfaces.MarketExpertInterface import _current_capture_batch

    _enable()
    store = replay_capture.initialize_replay_capture()

    replay_capture.set_current_batch("7_0930_20260910")
    assert _current_capture_batch() == "7_0930_20260910"

    seen = {}

    def worker():
        seen["other_thread"] = _current_capture_batch()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen["other_thread"] is None, (
        "a process-wide batch id would attribute one expert's analyses to another's batch")

    replay_capture.clear_current_batch()
    assert _current_capture_batch() is None


def test_the_batch_id_lands_on_the_analysis_record(cache_root):
    from ba2_common.core.replay import capture_scope

    _enable()
    store = replay_capture.initialize_replay_capture()
    replay_capture.set_current_batch("batch-42")

    from ba2_trade_platform.core.interfaces.MarketExpertInterface import (
        MarketExpertInterface,
    )

    class _Expert(MarketExpertInterface):
        def __init__(self):
            self.id = 3

        @classmethod
        def description(cls):
            return "test"

        def render_market_analysis(self, market_analysis):
            return ""

        def run_analysis(self, symbol, market_analysis):
            return None

    class _MA:
        id = 99
        symbol = "AAPL"
        subtype = None
        created_at = None

    expert = _Expert()
    with expert._analysis_capture(_MA(), {"a": 1}, "enter_market") as context:
        assert context is not None
        context.set_outcome(skip_reason="nothing to do")

    store.drain(timeout=10.0)
    records = store.index.analyses(store.session_id)
    assert len(records) == 1
    assert records[0].branch_flags["batch_id"] == "batch-42"
    assert records[0].expert_instance_id == 3
    assert records[0].symbol == "AAPL"
    assert records[0].outcome == "skip"
    assert records[0].skip_reason == "nothing to do"


# --------------------------------------------------------------------------- #
# Failure behaviour
# --------------------------------------------------------------------------- #
def test_initialize_is_idempotent(cache_root):
    _enable()
    first = replay_capture.initialize_replay_capture()
    assert replay_capture.initialize_replay_capture() is first


def test_capture_health_is_empty_when_capture_is_off(cache_root):
    assert replay_capture.get_capture_health() == {}


def test_shutdown_finalizes_the_session_and_uninstalls_the_store(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()
    session_id = store.session_id

    replay_capture.shutdown_replay_capture(timeout=5.0)

    assert get_replay_store() is None
    from ba2_common.core.replay import ReplayStore

    reopened = ReplayStore(store.root, writer="sync")
    try:
        assert reopened.index.get_session(session_id).status == "finalized"
    finally:
        reopened.close()
