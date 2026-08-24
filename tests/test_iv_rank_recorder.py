"""The daily ATM-IV recorder, the audit that decides what it records, and the
startup report that makes nine dormant live rules waking up a VISIBLE event.

Cadence rationale (pinned by test_running_the_hook_twice_in_one_day_writes_one_row):
once per weekday after the US close. The two hooks that already exist are both wrong
for this: the account refresh is a 5-minute interval (~288 samples/day, which turns a
252-day percentile into a last-few-days percentile), and rule evaluation is per
(expert, symbol, subtype) — daily for one use-case and weekly for another purely by
accident of ``_parse_schedule`` using ``times[0]``.
"""
from unittest.mock import patch

import pytest

from ba2_common.core import iv_rank_audit as audit
from ba2_trade_platform.core.db import get_all_instances
from ba2_trade_platform.core.models import OptionIVSnapshot
from ba2_trade_platform.core.types import ExpertEventRuleType
from tests.factories import (
    create_account_definition, create_expert_instance, create_event_action,
    create_ruleset, link_rule_to_ruleset,
)

IV_TRIGGERS = {"trigger_0": {"event_type": "has_buy_position"},
               "trigger_1": {"event_type": "iv_rank", "operator": ">=", "value": 50.0}}
PLAIN_TRIGGERS = {"trigger_0": {"event_type": "confidence", "operator": ">=", "value": 80.0}}


def _gated_expert(account_id, symbols, *, triggers=IV_TRIGGERS, enabled=True,
                  rule_name="Write Covered Call: Held Long, Rich IV"):
    """An enabled expert whose enter_market ruleset contains one rule with `triggers`."""
    rs = create_ruleset(name="rs")
    ea = create_event_action(name=rule_name, triggers=triggers)
    link_rule_to_ruleset(rs.id, ea.id, order_index=0)
    inst = create_expert_instance(account_id=account_id, expert="MockExpert",
                                  enabled=enabled, enter_market_ruleset_id=rs.id)
    _SYMBOLS[inst.id] = list(symbols)
    return inst


_SYMBOLS = {}


@pytest.fixture(autouse=True)
def _stub_expert_symbols(monkeypatch):
    """Resolve an expert's universe from the test map instead of the live registry.

    The audit reads it through ``get_enabled_instruments()`` on a resolved expert
    instance; MockExpert is not in the live expert registry, so the resolver call
    would raise. The seam is the symbol lookup, nothing else.
    """
    _SYMBOLS.clear()
    monkeypatch.setattr(audit, "_expert_symbols", lambda eid: _SYMBOLS.get(eid, []))
    yield
    _SYMBOLS.clear()


def _capture(monkeypatch, module, level):
    messages = []
    monkeypatch.setattr(module.logger, level, lambda msg, *a, **k: messages.append(str(msg)))
    return messages


# ---------------------------------------------------------------------------
# The audit: which experts are gated on iv_rank, and on which underlyings
# ---------------------------------------------------------------------------

def test_finds_the_gated_expert_its_rule_and_its_universe(mock_account_def):
    inst = _gated_expert(mock_account_def.id, ["AAPL", "MSFT"])

    gates = audit.find_iv_rank_gates()

    assert len(gates) == 1
    g = gates[0]
    assert g.expert_id == inst.id
    assert g.account_id == mock_account_def.id
    assert g.symbols == ("AAPL", "MSFT")
    assert g.rule_names == ("Write Covered Call: Held Long, Rich IV",)


def test_an_expert_with_no_iv_rank_rule_is_not_a_gate(mock_account_def):
    _gated_expert(mock_account_def.id, ["AAPL"], triggers=PLAIN_TRIGGERS)
    assert audit.find_iv_rank_gates() == []


def test_a_disabled_expert_is_not_a_gate(mock_account_def):
    """Recording IV for an expert that cannot trade is a pointless daily API bill."""
    _gated_expert(mock_account_def.id, ["AAPL"], enabled=False)
    assert audit.find_iv_rank_gates() == []


def test_the_open_positions_ruleset_is_scanned_too(mock_account_def):
    rs = create_ruleset(name="manage")
    ea = create_event_action(name="Roll on rich IV", triggers=IV_TRIGGERS)
    link_rule_to_ruleset(rs.id, ea.id)
    inst = create_expert_instance(account_id=mock_account_def.id, expert="MockExpert",
                                  open_positions_ruleset_id=rs.id)
    _SYMBOLS[inst.id] = ["NVDA"]

    assert [g.symbols for g in audit.find_iv_rank_gates()] == [("NVDA",)]


@pytest.mark.parametrize("placeholder", ["EXPERT", "DYNAMIC", "SCREENER", "OPEN_POSITIONS"])
def test_placeholder_symbols_are_not_underlyings(mock_account_def, placeholder):
    """``get_enabled_instruments`` returns sentinels for non-static selection methods.
    Recording an ATM IV for a ticker called "DYNAMIC" is a guaranteed chain-fetch error
    every single day."""
    _gated_expert(mock_account_def.id, [placeholder, "AAPL"])
    assert audit.recording_targets() == {mock_account_def.id: ["AAPL"]}


def test_recording_targets_are_the_deduped_union_per_account():
    a1 = create_account_definition(name="opt", provider="Mock")
    a2 = create_account_definition(name="other", provider="Mock")
    _gated_expert(a1.id, ["AAPL", "MSFT"])
    _gated_expert(a1.id, ["MSFT", "NVDA"])
    _gated_expert(a2.id, ["TSLA"])
    _gated_expert(a2.id, ["GOOG"], triggers=PLAIN_TRIGGERS)   # not gated -> excluded

    assert audit.recording_targets() == {a1.id: ["AAPL", "MSFT", "NVDA"], a2.id: ["TSLA"]}


# ---------------------------------------------------------------------------
# The recorder: cadence and honesty
# ---------------------------------------------------------------------------

def _patch_account(monkeypatch, account):
    import ba2_trade_platform.modules.accounts as accounts_mod
    monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: (lambda _id: account))


def test_running_the_hook_twice_in_one_day_writes_one_row(monkeypatch, mock_account, mock_account_def):
    """THE cadence test. The scheduler can fire a coalesced/missed run, an operator can
    trigger a manual refresh, and the app can restart — none of that may add a second
    sample for the day."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    _gated_expert(mock_account_def.id, ["AAPL", "MSFT"])
    _patch_account(monkeypatch, mock_account)

    TradeManager().record_daily_iv_snapshots()
    TradeManager().record_daily_iv_snapshots()

    rows = get_all_instances(OptionIVSnapshot)
    assert sorted(r.underlying for r in rows) == ["AAPL", "MSFT"]


def test_only_gated_symbols_are_fetched(monkeypatch, mock_account, mock_account_def):
    """Every recorded symbol costs one full option-chain request per day. Recording the
    whole account universe when only a subset is iv_rank-gated is pure waste."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    _gated_expert(mock_account_def.id, ["AAPL"])
    _gated_expert(mock_account_def.id, ["ZZZZ"], triggers=PLAIN_TRIGGERS)
    _patch_account(monkeypatch, mock_account)

    TradeManager().record_daily_iv_snapshots()

    assert [r.underlying for r in get_all_instances(OptionIVSnapshot)] == ["AAPL"]


def test_a_symbol_with_no_iv_is_skipped_and_reported(monkeypatch, mock_account, mock_account_def):
    """If the broker feed carries no IV, nothing is written and the operator is told —
    the alternative (a fabricated number) would arm live rules off invented data."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    _gated_expert(mock_account_def.id, ["AAPL", "NOIV"])
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: None if u == "NOIV" else 0.30, raising=True)
    _patch_account(monkeypatch, mock_account)
    warnings = _capture(monkeypatch, tm_mod, "warning")

    TradeManager().record_daily_iv_snapshots()

    assert [r.underlying for r in get_all_instances(OptionIVSnapshot)] == ["AAPL"]
    assert any("NOIV" in m for m in warnings), \
        "the recorder must name the symbols it could not sample"


def test_a_feed_that_carries_no_iv_at_all_is_an_error_not_a_shrug(
        monkeypatch, mock_account, mock_account_def):
    """The decisive live unknown is whether the broker's configured option feed
    populates implied_volatility. If it does not, the recorder writes nothing forever
    and every gated rule stays dead. That must be an ERROR naming the likely cause, not
    a quiet run of per-symbol warnings that looks like normal operation."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    _gated_expert(mock_account_def.id, ["AAPL", "MSFT"])
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: None, raising=True)
    _patch_account(monkeypatch, mock_account)
    errors = _capture(monkeypatch, tm_mod, "error")

    TradeManager().record_daily_iv_snapshots()

    assert get_all_instances(OptionIVSnapshot) == []
    assert any("feed" in m for m in errors), \
        "a total sampling failure must point at the feed, the likeliest cause"


def test_a_partial_failure_is_not_escalated_to_the_feed_error(
        monkeypatch, mock_account, mock_account_def):
    """One illiquid name with no chain is ordinary. Only a TOTAL blank warrants the
    "your feed is wrong" error, or the real signal drowns."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    _gated_expert(mock_account_def.id, ["AAPL", "NOIV"])
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: None if u == "NOIV" else 0.30, raising=True)
    _patch_account(monkeypatch, mock_account)
    errors = _capture(monkeypatch, tm_mod, "error")

    TradeManager().record_daily_iv_snapshots()
    assert errors == []


def test_a_non_options_account_is_skipped(monkeypatch, mock_account_def):
    """An equity-only account has no ATM IV to record and must not raise."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    class _EquityOnly:
        supports_options = False
        id = 1

    _gated_expert(mock_account_def.id, ["AAPL"])
    _patch_account(monkeypatch, _EquityOnly())

    TradeManager().record_daily_iv_snapshots()
    assert get_all_instances(OptionIVSnapshot) == []


def test_one_failing_account_does_not_abort_the_others(monkeypatch, mock_account):
    """Per-account isolation, same as refresh_accounts."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.modules.accounts as accounts_mod

    bad = create_account_definition(name="bad", provider="Bad")
    good = create_account_definition(name="good", provider="Good")
    _gated_expert(bad.id, ["AAPL"])
    _gated_expert(good.id, ["MSFT"])

    def _cls(provider):
        if provider == "Bad":
            raise RuntimeError("broker down")
        return lambda _id: mock_account

    monkeypatch.setattr(accounts_mod, "get_account_class", _cls)

    TradeManager().record_daily_iv_snapshots()
    assert [r.underlying for r in get_all_instances(OptionIVSnapshot)] == ["MSFT"]


# ---------------------------------------------------------------------------
# Startup visibility
# ---------------------------------------------------------------------------

def test_startup_report_names_the_rules_that_are_still_inert(monkeypatch, mock_account,
                                                             mock_account_def):
    """Nine live rules go from "can never fire" to "fires" once the series fills. That
    must be an announced state change, not something an operator discovers from an
    unexpected order."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    _gated_expert(mock_account_def.id, ["AAPL"])
    _patch_account(monkeypatch, mock_account)
    infos = _capture(monkeypatch, tm_mod, "info")

    TradeManager().report_iv_rank_readiness()

    blob = "\n".join(infos)
    assert "AAPL" in blob
    assert "0/5" in blob, "the report must show how far the series is from min_samples"
    assert "Write Covered Call: Held Long, Rich IV" in blob, "name the rule that is gated"


def test_startup_report_flips_to_armed_once_the_series_is_deep_enough(
        monkeypatch, mock_account, mock_account_def):
    from datetime import datetime, timedelta, timezone
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    _gated_expert(mock_account_def.id, ["AAPL"])
    _patch_account(monkeypatch, mock_account)
    now = datetime.now(timezone.utc)
    for d in range(1, 6):
        add_instance(OptionIVSnapshot(account_id=mock_account.id, underlying="AAPL",
                                      atm_iv=0.1 * d, recorded_at=now - timedelta(days=d)))

    infos = _capture(monkeypatch, tm_mod, "info")
    TradeManager().report_iv_rank_readiness()

    blob = "\n".join(infos)
    assert "5/5" in blob
    assert "ARMED" in blob, "an armed gate must be called out explicitly"


def test_report_output_stays_bounded_for_a_large_universe(monkeypatch, mock_account,
                                                          mock_account_def):
    """The live gated universe is 30 names per expert across 7 experts. One log line
    per symbol would bury the startup log in ~200 lines that nobody reads — which
    defeats the point of announcing the change at all."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    symbols = [f"SYM{i:02d}" for i in range(30)]
    _gated_expert(mock_account_def.id, symbols)
    _patch_account(monkeypatch, mock_account)
    infos = _capture(monkeypatch, tm_mod, "info")

    TradeManager().report_iv_rank_readiness()

    assert len(infos) <= 6, f"{len(infos)} lines for one expert is too many"
    blob = "\n".join(infos)
    assert "0/30 underlying(s) ARMED" in blob
    assert "(+18 more)" in blob, "the symbol list must be truncated, not dropped"


def test_report_is_silent_when_nothing_is_gated(monkeypatch, mock_account_def):
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    infos = _capture(monkeypatch, tm_mod, "info")
    TradeManager().report_iv_rank_readiness()
    assert not any("iv_rank" in m.lower() for m in infos)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def test_the_recorder_is_scheduled_once_per_weekday_after_the_close():
    """Cron in market time, Mon-Fri, after 16:00 ET. NOT an interval trigger: an
    interval would drift across the day boundary and (before the dedup guard existed)
    was exactly the shape that would have corrupted the series."""
    from ba2_trade_platform.core.JobManager import JobManager, IV_SNAPSHOT_JOB_ID

    jm = JobManager()
    with patch.object(jm, "_scheduler") as sched:
        jm._schedule_iv_snapshot_job()

    kwargs = sched.add_job.call_args.kwargs
    assert kwargs["id"] == IV_SNAPSHOT_JOB_ID
    trigger = kwargs["trigger"]
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert int(fields["hour"]) >= 16, "must run after the 16:00 ET close"
    assert str(trigger.timezone) == "America/New_York", "market time, not machine time"


def test_start_schedules_the_recorder(monkeypatch):
    """A job nobody schedules records nothing — the whole feature hinges on this line."""
    from ba2_trade_platform.core.JobManager import JobManager

    jm = JobManager()
    called = []
    for name in ("_start_control_thread", "_schedule_all_expert_jobs",
                 "_schedule_account_refresh_job", "_start_account_refresh_watchdog"):
        monkeypatch.setattr(jm, name, lambda *a, **k: None)
    monkeypatch.setattr(jm, "_schedule_iv_snapshot_job", lambda: called.append(True))
    monkeypatch.setattr(jm, "_report_iv_rank_readiness", lambda: None)

    jm.start()
    assert called == [True]
    jm._running = False


def test_executing_the_job_drives_the_recorder(monkeypatch):
    from ba2_trade_platform.core.JobManager import JobManager
    import ba2_trade_platform.core.TradeManager as tm_mod

    calls = []
    monkeypatch.setattr(tm_mod.TradeManager, "record_daily_iv_snapshots",
                        lambda self: calls.append(True), raising=True)

    JobManager()._execute_iv_snapshot()
    assert calls == [True]
