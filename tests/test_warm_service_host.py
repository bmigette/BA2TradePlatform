"""The live host's warm switch, settings, batch hook and scheduled job (spec step 4).

This is the layer that decides WHETHER this installation warms anything, with what
budget, and when. Four things must hold:

* the switch is OFF until someone turns it on, and every row exists so it CAN be
  turned on from the UI (nothing migrates AppSetting rows into existence);
* with it off, no thread starts and no job is scheduled -- "no deployment-triggered
  bulk download";
* the batch-end hook enqueues only what the roots do not already hold, resolved from
  the settings the analysis ACTUALLY RAN WITH (read from the capture store, not from
  a database row that may have been edited since);
* the post-close job is scheduled from the account's own exchange close, and refuses
  to schedule at all when no account can report one.
"""
import logging
import os
import threading
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay import (
    AnalysisRecord,
    ReplayStatus,
    ReplayStore,
    SessionRecord,
    set_replay_store,
)
from ba2_trade_platform.core import warm_service
from ba2_trade_platform.core.db import add_instance, get_setting
from ba2_trade_platform.core.models import AppSetting

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)



class _CapturedErrors(logging.Handler):
    """Collect the host logger's ERROR records.

    ``caplog`` cannot see them: ``ba2_trade_platform.logger`` sets
    ``propagate = False`` (logger.py:24), so records never reach the root handler
    pytest installs. Attaching to the logger itself is the only way to assert on
    what the operator will actually read.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def host_errors():
    # The logger object warm_service HOLDS, not a fresh import of the name: a test
    # earlier in the session that reloads ``ba2_trade_platform.logger`` leaves this
    # module bound to the old object, and a handler on the new one sees nothing.
    # Likewise a leaked ``logging.disable(...)`` or ``logger.disabled`` from another
    # file would silently empty this list; both are lifted for the test and restored.
    from ba2_trade_platform.core import warm_service
    host_logger = warm_service.logger
    assert isinstance(host_logger, logging.Logger), (
        f"warm_service.logger is {type(host_logger).__name__}, not a Logger: a test "
        f"earlier in the session left a mock in sys.modules['ba2_trade_platform.logger'] "
        f"(tests/test_penny_*.py once did) and this module bound it at first import")

    handler = _CapturedErrors()
    saved_disable = logging.root.manager.disable
    saved_disabled = host_logger.disabled
    saved_level = host_logger.level
    logging.disable(logging.NOTSET)
    host_logger.disabled = False
    if host_logger.level > logging.ERROR:
        host_logger.setLevel(logging.ERROR)
    host_logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        host_logger.removeHandler(handler)
        host_logger.setLevel(saved_level)
        host_logger.disabled = saved_disabled
        logging.disable(saved_disable)


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point the instance's cache folder at a temp dir and always tear the warm down."""
    import ba2_trade_platform.config as config

    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "db.sqlite"))
    try:
        yield tmp_path
    finally:
        warm_service.shutdown_warm_service(timeout=5.0)
        set_replay_store(None)


def _account_definition():
    from ba2_trade_platform.core.models import AccountDefinition

    return AccountDefinition(name="test", provider="AlpacaAccount")


def _enable():
    add_instance(AppSetting(key=warm_service.WARM_ENABLED_KEY, value_str="true"))


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, *, func, trigger, id, name, replace_existing, max_instances, coalesce):
        self.jobs[id] = {"func": func, "trigger": trigger, "name": name}
        return self.jobs[id]


class _FakeJobManager:
    def __init__(self):
        self._scheduler = _FakeScheduler()


# --------------------------------------------------------------------------- #
# The switch and the settings
# --------------------------------------------------------------------------- #
def test_the_settings_are_created_at_their_documented_pilot_values(cache_root):
    created = warm_service.ensure_settings()

    assert created == {
        "warm_enabled": "false",
        "warm_workers": "2",
        "warm_daily_allowance_mib": "100",
        "warm_settlement_offset_minutes": "90",
    }
    for key, value in created.items():
        assert get_setting(key) == value, "the row must exist so the UI can change it"


def test_nothing_starts_or_is_scheduled_while_the_switch_is_off(cache_root):
    jobs = _FakeJobManager()

    queue = warm_service.initialize_warm_service(job_manager=jobs)

    assert queue is None
    assert warm_service.get_warm_queue() is None
    assert jobs._scheduler.jobs == {}, "warming off must not register a background job"


def test_the_batch_hook_is_a_no_op_while_the_switch_is_off(cache_root):
    warm_service.ensure_settings()

    assert warm_service.on_analysis_batch_end("batch-1") == 0


@pytest.mark.parametrize("spelling", ["true", "True", "1", "yes"])
def test_any_true_spelling_turns_the_warm_on(cache_root, spelling, monkeypatch):
    add_instance(AppSetting(key=warm_service.WARM_ENABLED_KEY, value_str=spelling))
    monkeypatch.setattr(warm_service, "schedule_settlement_job", lambda *a, **k: None)

    queue = warm_service.initialize_warm_service()
    try:
        assert queue is not None and queue.is_running()
    finally:
        warm_service.shutdown_warm_service(timeout=5.0)


def test_the_allowance_is_read_in_mib(cache_root):
    add_instance(AppSetting(key=warm_service.WARM_ALLOWANCE_KEY, value_str="7"))

    assert warm_service.warm_daily_allowance_bytes() == 7 * 1024 * 1024


# --------------------------------------------------------------------------- #
# The batch-end hook
# --------------------------------------------------------------------------- #
def _open_capture_store(root, settings, *, expert="FMPRating", symbol="AAPL",
                        batch_id="batch-1", instance_id=None):
    """A capture store holding one recorded analysis of ``expert``/``symbol``."""
    store = ReplayStore(str(root), writer="sync")
    store.begin_session(SessionRecord(
        session_id="s1", instance_id="inst", started_at=NOW, exchange_tz="America/New_York",
        app_version="test", dirty=False))
    store.submit(
        AnalysisRecord(
            analysis_id="a1", attempt_id="t1", session_id="s1", expert_class=expert,
            expert_instance_id=instance_id, symbol=symbol,
            use_case=ReplayStatus.USE_CASE_ENTER_MARKET, started_at=NOW,
            outcome=ReplayStatus.OUTCOME_RECOMMENDATION,
            branch_flags={"batch_id": batch_id}),
        objects={"settings": settings})
    set_replay_store(store)
    return store


FMPRATING_SETTINGS = {
    "max_analyst_age_months": 0,
    "use_atr_stop": False,
    "sizing_mode": "notional",
    "atr_period": 14,
}


def _start_queue(monkeypatch, fetcher):
    """Start the warm with a fetcher that records instead of downloading."""
    monkeypatch.setattr(warm_service, "schedule_settlement_job", lambda *a, **k: None)
    _enable()
    queue = warm_service.initialize_warm_service()
    assert queue is not None
    # Swap the real fetcher out AFTER construction: the test is about which
    # requirements reach the queue, not about talking to FMP.
    queue._fetcher = fetcher
    return queue


def test_the_batch_hook_returns_immediately_without_touching_the_filesystem(cache_root,
                                                                            monkeypatch):
    """It runs in a TRADING worker's ``finally``; resolving a batch there delays the
    next analysis for work that has no deadline (spec section 6)."""
    import os

    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS)
    queue = _start_queue(monkeypatch, lambda req: None)
    caller = threading.current_thread().ident
    scans = []

    real_scandir = os.scandir

    def _watched_scandir(path="."):
        if threading.current_thread().ident == caller:
            scans.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _watched_scandir)

    assert warm_service.on_analysis_batch_end("batch-1") == 1
    caller_scans = list(scans)
    assert queue.join(timeout=10.0)

    assert caller_scans == [], (
        f"the hook listed cache directories on the trading thread: {caller_scans}")


def test_the_queued_job_enqueues_only_what_the_roots_do_not_hold(cache_root, monkeypatch):
    import ba2_trade_platform.config as config

    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS)
    history = os.path.join(config.CACHE_FOLDER, "fmp_history")
    os.makedirs(history, exist_ok=True)
    with open(os.path.join(history, "price_target__AAPL.json"), "w", encoding="utf-8") as fh:
        fh.write('[{"t": 1}]')
    queue = _start_queue(monkeypatch, lambda req: None)

    warm_service.on_analysis_batch_end("batch-1")
    assert queue.join(timeout=10.0)

    keys = set(queue.warmed_keys())
    assert keys, "the job must have run and enqueued the gaps"
    assert not any("price_target" in k for k in keys), (
        "a payload already on the root is not work")
    assert any("grades_historical" in k for k in keys), (
        "a payload the root lacks must be enqueued")


def test_the_job_ignores_analyses_from_another_batch(cache_root, monkeypatch):
    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS, batch_id="other")
    queue = _start_queue(monkeypatch, lambda req: None)

    assert warm_service.plan_and_enqueue_batch("batch-1") == 0


def test_an_expert_without_an_adapter_enqueues_nothing_but_is_not_an_error(cache_root,
                                                                          monkeypatch):
    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS, expert="FactorRanker")
    _start_queue(monkeypatch, lambda req: None)

    assert warm_service.plan_and_enqueue_batch("batch-1") == 0, (
        "an unsupported expert is reported by the plan, never downloaded for")


def test_a_settings_dict_missing_a_key_the_resolver_needs_warms_nothing_and_says_so(
        cache_root, monkeypatch, host_errors):
    _open_capture_store(cache_root / "replay", {"max_analyst_age_months": 0})
    _start_queue(monkeypatch, lambda req: None)

    assert warm_service.plan_and_enqueue_batch("batch-1") == 0
    assert any("use_atr_stop" in m for m in host_errors), (
        "the missing key must be named; warming a configuration the analysis did not "
        "run with is worse than warming nothing")


def test_the_batch_hook_is_a_no_op_when_the_batch_is_already_queued(cache_root, monkeypatch):
    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS)
    queue = _start_queue(monkeypatch, lambda req: None)
    queue.stop(timeout=2.0)          # nothing drains, so the first job stays queued

    assert warm_service.on_analysis_batch_end("batch-1") == 1
    assert warm_service.on_analysis_batch_end("batch-1") == 0


# --------------------------------------------------------------------------- #
# The rules half of a batch's requirements
# --------------------------------------------------------------------------- #
def _instance_with_an_earnings_rule():
    """An ExpertInstance whose enter-market ruleset holds one days_to_earnings rule.

    ``days_to_earnings`` is the rule condition that adds data requirements: it reads
    the quarterly earnings calendar (with the annual estimates as the documented
    fallback) for every symbol in the universe. Nothing else about a ruleset warms
    anything -- the ATR requirement comes from the expert's own ``use_atr_stop``
    setting, not from a rule.
    """
    from ba2_trade_platform.core.db import add_instance as _add
    from ba2_trade_platform.core.models import (
        AccountDefinition, EventAction, ExpertInstance, Ruleset, RulesetEventActionLink,
    )
    from ba2_trade_platform.core.types import AnalysisUseCase, ExpertEventRuleType

    account_id = _add(AccountDefinition(name="warm-rules", provider="AlpacaAccount"))
    ruleset_id = _add(Ruleset(name="enter", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                              subtype=AnalysisUseCase.ENTER_MARKET))
    action_id = _add(EventAction(
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET,
        name="skip around earnings",
        triggers={"t1": {"event_type": "days_to_earnings", "operator": "<", "value": 5}},
        actions={"a1": {"action_type": "reject_recommendation"}}))
    # The link row has a COMPOSITE primary key and no ``id``, so it goes in through a
    # session rather than through ``add_instance`` (which returns the new id).
    from ba2_common.core.db import get_db

    with get_db() as session:
        session.add(RulesetEventActionLink(ruleset_id=ruleset_id, eventaction_id=action_id,
                                           order_index=0))
        session.commit()
    return _add(ExpertInstance(account_id=account_id, expert="FMPRating", enabled=True,
                               enter_market_ruleset_id=ruleset_id))


def test_the_rules_of_an_instance_are_read_outside_the_session_that_loaded_it(cache_root):
    """``get_instance`` hands back a DETACHED row, so ``ruleset.event_actions`` -- a lazy
    relationship -- raises DetachedInstanceError. The broad handler turned that into a
    warning and ``None``: every rule-derived requirement silently stopped being warmed
    while the log said the resolver had run."""
    instance_id = _instance_with_an_earnings_rule()

    actions = warm_service._rules_for_instance(instance_id)

    assert actions, "the instance's rules came back empty (the detached-relationship trap)"
    assert [a.triggers["t1"]["event_type"] for a in actions] == ["days_to_earnings"]


def test_an_instance_that_no_longer_exists_is_not_an_error(cache_root, host_errors):
    """An expert deleted between the analysis and this hook: its own declarations still
    resolve, the rule extras cannot, and that is an absence rather than a wrong answer."""
    assert warm_service._rules_for_instance(999_999) is None
    assert host_errors == []


def test_a_rule_condition_adds_its_requirement_to_the_batch(cache_root, monkeypatch):
    """End to end: the earnings calendar the rule reads is enqueued for the symbol."""
    instance_id = _instance_with_an_earnings_rule()
    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS, instance_id=instance_id)
    queue = _start_queue(monkeypatch, lambda req: None)

    # Resolved on THIS thread, not through the batch hook: the test database is an
    # in-memory SQLite, which SQLAlchemy serves per-thread, so a warm worker resolving
    # the instance would open an empty one. The hook's own contract (submit a job and
    # return) is pinned by its own test.
    warm_service.plan_and_enqueue_batch("batch-1")
    assert queue.join(timeout=10.0)

    keys = set(queue.warmed_keys())
    assert any("past_earnings_quarterly" in k for k in keys), (
        f"the days_to_earnings rule's calendar was never warmed; enqueued {sorted(keys)}")


# --------------------------------------------------------------------------- #
# The unknown-size reservation
# --------------------------------------------------------------------------- #
def test_the_unknown_reserve_is_measured_on_the_real_root(cache_root):
    """It was derived from a plan over an EMPTY requirement list.

    ``measured_sizes()`` reads the plan's ENTRIES, and a plan over no requirements has
    none -- so the measurement was empty whatever the root held, ``unknown_reserve_for``
    always raised, and the startup log claimed there was no artifact to size from while
    the reserve silently became the whole daily allowance (the second unmeasurable item
    then pauses the warm).
    """
    import ba2_trade_platform.config as config

    history = os.path.join(config.CACHE_FOLDER, "fmp_history")
    os.makedirs(history, exist_ok=True)
    for name, size in (("price_target__AAPL.json", 400), ("grades_historical__AAPL.json", 900)):
        with open(os.path.join(history, name), "w", encoding="utf-8") as fh:
            fh.write("x" * size)
    warm_service.ensure_settings()

    reserve = warm_service._unknown_reserve_bytes()

    assert 400 <= reserve <= 900, (
        f"the reserve must come off the files on the root, got {reserve}")
    assert reserve < warm_service.warm_daily_allowance_bytes()


def test_an_empty_root_still_reserves_the_whole_allowance_and_says_so(cache_root):
    """No basis for any reservation is stated, never guessed."""
    warm_service.ensure_settings()

    assert warm_service._unknown_reserve_bytes() == warm_service.warm_daily_allowance_bytes()


# --------------------------------------------------------------------------- #
# The post-close job
# --------------------------------------------------------------------------- #
def test_the_post_close_job_is_scheduled_at_the_close_plus_the_settlement_offset(cache_root):
    warm_service.ensure_settings()
    jobs = _FakeJobManager()

    job_id = warm_service.schedule_settlement_job(
        jobs, close_time_provider=lambda: (16, 0, "America/New_York"))

    assert job_id == warm_service.WARM_SETTLEMENT_JOB_ID
    trigger = jobs._scheduler.jobs[job_id]["trigger"]
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "17" and fields["minute"] == "30", (
        "16:00 close + the 90-minute settlement offset")
    assert str(trigger.timezone) == "America/New_York"


def test_the_job_is_re_resolved_daily(cache_root):
    """An exchange that changes its hours must not wait for a process restart."""
    warm_service.ensure_settings()
    jobs = _FakeJobManager()

    warm_service.schedule_settlement_job(
        jobs, close_time_provider=lambda: (16, 0, "America/New_York"))

    assert warm_service.WARM_RERESOLVE_JOB_ID in jobs._scheduler.jobs


@pytest.mark.parametrize("close_utc,expected_local", [
    # Alpaca normalises its clock to UTC. Winter: 16:00 New York is 21:00 UTC.
    (datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc), 16),
    # Summer (EDT): the SAME 16:00 local close is 20:00 UTC. Reading the hour off the
    # instant's own tzinfo gives 21 and 20 -- an hour of silent DST drift.
    (datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc), 16),
])
def test_the_close_is_read_in_the_exchange_zone_not_the_brokers(cache_root, monkeypatch,
                                                               close_utc, expected_local):
    from ba2_common.core.account_types import MarketHours

    class _Account:
        def get_market_hours(self):
            return MarketHours(is_open=False, next_close=close_utc,
                               close_at=close_utc, source="broker", as_of=close_utc)

    monkeypatch.setattr(warm_service, "get_account_instance_from_id", None, raising=False)
    monkeypatch.setattr("ba2_trade_platform.core.utils.get_account_instance_from_id",
                        lambda account_id: _Account())
    add_instance(_account_definition())

    hour, minute, tz_name = warm_service.resolve_market_close()

    assert (hour, minute) == (expected_local, 0)
    assert tz_name == "America/New_York", (
        "the CRON needs the exchange zone, or APScheduler cannot follow the DST change")


def test_no_job_is_scheduled_when_no_account_can_report_its_close(cache_root, host_errors):
    warm_service.ensure_settings()
    jobs = _FakeJobManager()

    assert warm_service.schedule_settlement_job(jobs, close_time_provider=lambda: None) is None
    assert jobs._scheduler.jobs == {}
    assert any("close" in m for m in host_errors)


def test_the_pinned_root_is_dated_under_the_instances_cache_folder(cache_root):
    import ba2_trade_platform.config as config

    assert warm_service.pinned_root_for("2026-09-11") == os.path.join(
        config.CACHE_FOLDER, "replay", "pinned", "2026-09-11")


def test_the_settlement_run_is_a_no_op_with_the_warm_off(cache_root):
    warm_service.ensure_settings()

    assert warm_service.run_settlement_warm() is None


def test_the_settlement_run_pins_the_sessions_artifacts_under_a_dated_root(cache_root,
                                                                          monkeypatch):
    """Lifecycle step 5: extend the tails, then PIN, so a comparison reads a fixed set."""
    import json

    import ba2_trade_platform.config as config

    _open_capture_store(cache_root / "replay", FMPRATING_SETTINGS)
    history = os.path.join(config.CACHE_FOLDER, "fmp_history")
    os.makedirs(history, exist_ok=True)
    with open(os.path.join(history, "price_target__AAPL.json"), "w", encoding="utf-8") as fh:
        fh.write('[{"t": 1}]')
    queue = _start_queue(monkeypatch, lambda req: None)

    destination = warm_service.run_settlement_warm()

    assert destination == warm_service.pinned_root_for(
        datetime.now(timezone.utc).date().isoformat())
    manifest = json.load(open(os.path.join(destination, "manifest.json"), encoding="utf-8"))
    assert "fmp_history/price_target__AAPL.json" in manifest["files"]
    assert manifest["files"]["fmp_history/price_target__AAPL.json"]["provenance"] == \
        "legacy_history_unknown_revision", (
        "a file that was already there is NOT evidence of the revision live consumed")
    assert manifest["unpinned"], "requirements with no artifact must be named, not omitted"
    assert queue.is_running()
