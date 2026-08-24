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

from datetime import datetime, timedelta, timezone

from ba2_common.core import iv_rank_audit as audit
from ba2_trade_platform.core.db import get_all_instances
from ba2_trade_platform.core.models import OptionIVSnapshot
from ba2_trade_platform.core.types import ExpertEventRuleType, MarketAnalysisStatus
from tests.factories import (
    create_account_definition, create_expert_instance, create_event_action,
    create_market_analysis, create_ruleset, link_rule_to_ruleset,
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


def _analysed(expert_id, symbol, *, days_ago=1, status=MarketAnalysisStatus.COMPLETED):
    """Record that `expert_id` ran an analysis on `symbol` `days_ago` days ago."""
    return create_market_analysis(
        symbol=symbol, expert_instance_id=expert_id, status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))


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
# Deferred universes: the expert picks its symbols at ANALYSIS time
# ---------------------------------------------------------------------------
#
# ``get_enabled_instruments()`` returns a SENTINEL — "SCREENER", "DYNAMIC", "EXPERT" —
# for every selection method that resolves at analysis time. Filtering those out and
# moving on (the original behaviour) meant a screener expert contributed ZERO recording
# targets, so its gates could never arm. That is not a corner case: on the live book
# FOUR of the seven iv_rank-gated experts (26, 29, 31, 33) select via screener and
# between them carry SIX of the nine gated rules. Deleting the sentinel silently turned
# the fix into a two-thirds fix.
#
# WHY RECENT ANALYSES, and not the alternatives:
#
#   * Re-running the screener at recorder time is the obvious idea and is wrong twice
#     over. The screener is a live, deliberately UNCACHED FMP call
#     (FMPScreenerProvider excludes itself from the uniform disk cache), so it doubles
#     the daily bill; and the 16:30 universe is not the universe the 08:00 analysis pass
#     used, so it would sample names the rules never ask about while missing names they
#     do. Sampling has to follow what the expert actually looked at.
#   * Declaring screener experts unsupported leaves six live rules permanently inert,
#     which is the defect being fixed.
#   * MarketAnalysis is ALREADY this codebase's answer to "is this symbol in a screener
#     expert's universe?" — SmartRiskManagerToolkit gates order opening on exactly this
#     query. A second, different definition is how two implementations of one statistic
#     start diverging.
#
# WINDOW: 30 days, not SmartRiskManager's 24 hours. The live screener experts run
# WEEKLY (verified: experts 26/29/31/33 each produce ~15 symbols every 7 days), so a
# 24h window would see nothing on six days in seven and the recorder would sample in
# bursts — the one cadence the whole feature exists to prevent. 30 days spans four
# runs of the slowest configured cadence. Ageing a name out is cheap because rows are
# never deleted: a symbol the screener re-selects still has its old series waiting.

def _screener_expert(account_id, **kw):
    """An iv_rank-gated expert whose universe is the SCREENER sentinel."""
    return _gated_expert(account_id, ["SCREENER"], **kw)


def test_a_screener_experts_universe_is_what_it_recently_analysed(mock_account_def):
    """THE critical gap: a sentinel universe used to yield zero recording targets."""
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "AAPL", days_ago=1)
    _analysed(inst.id, "MSFT", days_ago=8)

    assert audit.recording_targets() == {mock_account_def.id: ["AAPL", "MSFT"]}


@pytest.mark.parametrize("sentinel", ["SCREENER", "DYNAMIC", "EXPERT"])
def test_every_deferred_selection_mode_is_recovered_the_same_way(mock_account_def, sentinel):
    """All three sentinels mean "resolved at analysis time". Handling only SCREENER
    would leave the same hole one rename away."""
    inst = _gated_expert(mock_account_def.id, [sentinel])
    _analysed(inst.id, "AAPL")

    assert audit.recording_targets() == {mock_account_def.id: ["AAPL"]}


def test_a_deferred_universe_is_labelled_as_recovered_not_configured(mock_account_def):
    """The report must be able to say WHERE a symbol list came from — a recovered list
    is a best-effort trailing observation, a configured one is authoritative."""
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "AAPL")

    gate = audit.find_iv_rank_gates()[0]
    assert gate.universe_source == audit.UNIVERSE_RECENT_ANALYSES
    assert gate.universe_is_known is True
    assert gate.deferred_modes == ("SCREENER",)


def test_a_static_universe_is_labelled_configured(mock_account_def):
    _gated_expert(mock_account_def.id, ["AAPL"])
    gate = audit.find_iv_rank_gates()[0]
    assert gate.universe_source == audit.UNIVERSE_CONFIGURED
    assert gate.deferred_modes == ()


def test_analyses_older_than_the_window_are_not_the_universe(mock_account_def):
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "AAPL", days_ago=1)
    _analysed(inst.id, "STALE", days_ago=audit.DEFERRED_UNIVERSE_LOOKBACK_DAYS + 1)

    assert audit.recording_targets() == {mock_account_def.id: ["AAPL"]}


def test_the_window_has_an_upper_bound_too(mock_account_def):
    """Absolute dates, because the test above scales with the constant and so cannot
    catch it being INFLATED.

    An unbounded window would make the recording universe every symbol the expert has
    ever screened, growing forever — one full option-chain request per name per day, for
    names the screener stopped selecting months ago. The gate itself needs only
    ``min_samples`` (5) trailing days, so a name genuinely back in the universe re-arms
    within a week off its retained history.
    """
    assert audit.DEFERRED_UNIVERSE_LOOKBACK_DAYS <= 60, \
        "the recording universe must not accumulate everything ever screened"

    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "AAPL", days_ago=1)
    _analysed(inst.id, "LASTQUARTER", days_ago=90)
    _analysed(inst.id, "LASTYEAR", days_ago=300)

    assert audit.recording_targets() == {mock_account_def.id: ["AAPL"]}


def test_the_window_spans_a_weekly_screener_cadence(mock_account_def):
    """Live screener experts run every 7 days. A window that cannot hold several runs
    makes the recorder sample in bursts and starve in between."""
    assert audit.DEFERRED_UNIVERSE_LOOKBACK_DAYS >= 28

    inst = _screener_expert(mock_account_def.id)
    for week, sym in enumerate(["W1", "W2", "W3", "W4"], start=1):
        _analysed(inst.id, sym, days_ago=7 * week)

    assert audit.recording_targets() == {mock_account_def.id: ["W1", "W2", "W3", "W4"]}


def test_one_experts_analyses_are_not_another_experts_universe(mock_account_def):
    """Two screener experts on one account run different screens; borrowing symbols
    would sample names neither rule will ever evaluate."""
    mine = _screener_expert(mock_account_def.id)
    theirs = create_expert_instance(account_id=mock_account_def.id, expert="MockExpert")
    _analysed(mine.id, "MINE")
    _analysed(theirs.id, "THEIRS")

    assert audit.recording_targets() == {mock_account_def.id: ["MINE"]}


def test_a_skipped_analysis_still_proves_the_screener_selected_the_symbol(mock_account_def):
    """Deliberately wider than SmartRiskManager's COMPLETED-only filter. That check
    AUTHORISES an order and wants proof of finished work; this one PRE-WARMS a series
    and wants the widest honest superset — a symbol whose analysis was skipped today is
    one the screener will hand back tomorrow, and a missing series is what makes a rule
    permanently inert."""
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "SKIP", status=MarketAnalysisStatus.SKIPPED)

    assert audit.recording_targets() == {mock_account_def.id: ["SKIP"]}


def test_a_sentinel_is_never_recorded_as_a_ticker(mock_account_def):
    """Whatever else changes, asking a broker for the option chain of "SCREENER" must
    stay impossible — including if a sentinel somehow lands in the analysis history."""
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "SCREENER")
    _analysed(inst.id, "AAPL")

    assert audit.recording_targets() == {mock_account_def.id: ["AAPL"]}


def test_a_screener_expert_that_has_analysed_nothing_is_UNKNOWN_not_empty(mock_account_def):
    """"I cannot see this expert's universe" and "this expert has no symbols" are
    different facts and must not collapse into the same silent zero."""
    _screener_expert(mock_account_def.id)

    gate = audit.find_iv_rank_gates()[0]
    assert gate.symbols == ()
    assert gate.universe_source == audit.UNIVERSE_UNKNOWN
    assert gate.universe_is_known is False


def test_a_universe_that_could_not_be_resolved_is_UNKNOWN(monkeypatch, mock_account_def):
    """A raising resolver already logged an error, but the gate must carry the fact
    forward so the report cannot present the expert as fine."""
    _gated_expert(mock_account_def.id, ["AAPL"])
    monkeypatch.setattr(audit, "_expert_symbols",
                        lambda eid: (_ for _ in ()).throw(RuntimeError("registry down")))
    _capture(monkeypatch, audit, "error")

    gate = audit.find_iv_rank_gates()[0]
    assert gate.universe_is_known is False
    assert gate.universe_source == audit.UNIVERSE_UNKNOWN


def test_a_recovered_universe_is_recorded_end_to_end(monkeypatch, mock_account, mock_account_def):
    """The whole point: a screener expert's gated underlyings now get real samples."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "AAPL")
    _analysed(inst.id, "MSFT")
    _patch_account(monkeypatch, mock_account)

    TradeManager().record_daily_iv_snapshots()

    assert sorted(r.underlying for r in get_all_instances(OptionIVSnapshot)) == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# The report may never present an invisible universe as a healthy one
# ---------------------------------------------------------------------------
#
# "0/0 ARMED" is the worst possible line: it is literally true, reads as success, and
# is emitted exactly when the recorder has no idea what it is supposed to be recording.

def _report_lines(monkeypatch):
    import ba2_trade_platform.core.TradeManager as tm_mod
    from ba2_trade_platform.core.TradeManager import TradeManager
    infos = _capture(monkeypatch, tm_mod, "info")
    warnings = _capture(monkeypatch, tm_mod, "warning")
    TradeManager().report_iv_rank_readiness()
    return infos, warnings


def test_an_invisible_universe_is_a_WARNING_naming_the_dead_rules(
        monkeypatch, mock_account, mock_account_def):
    _screener_expert(mock_account_def.id, rule_name="Buy Call: Bullish Dip, Low IV")
    _patch_account(monkeypatch, mock_account)

    infos, warnings = _report_lines(monkeypatch)

    blob = "\n".join(warnings)
    assert "Buy Call: Bullish Dip, Low IV" in blob, "name the rule that stays dead"
    assert "SCREENER" in blob, "name WHY the universe is invisible"
    assert "0/0" not in "\n".join(infos + warnings), \
        "an unknown universe must never be rendered as a count"


@pytest.mark.parametrize("scenario", ["no-analyses", "resolver-raises", "empty-static-list"])
def test_the_report_can_never_print_0_of_0_ARMED(monkeypatch, mock_account, mock_account_def,
                                                 scenario):
    """Swept across every path that produces a gate with no symbols."""
    if scenario == "no-analyses":
        _screener_expert(mock_account_def.id)
    elif scenario == "resolver-raises":
        _gated_expert(mock_account_def.id, ["AAPL"])
        monkeypatch.setattr(audit, "_expert_symbols",
                            lambda eid: (_ for _ in ()).throw(RuntimeError("boom")))
        _capture(monkeypatch, audit, "error")
    else:
        _gated_expert(mock_account_def.id, [])
    _patch_account(monkeypatch, mock_account)

    infos, warnings = _report_lines(monkeypatch)

    blob = "\n".join(infos + warnings)
    assert "0/0" not in blob, f"{scenario} still renders an empty universe as a count"
    assert any("ARMED" not in line for line in warnings)
    assert warnings, f"{scenario} must produce a warning, not a clean info-only report"


def test_a_gated_expert_with_no_configured_instruments_is_reported(
        monkeypatch, mock_account, mock_account_def):
    """A static expert whose instrument list is empty is misconfigured, not healthy."""
    _gated_expert(mock_account_def.id, [])
    _patch_account(monkeypatch, mock_account)

    _, warnings = _report_lines(monkeypatch)
    assert any("no" in m.lower() and "instrument" in m.lower() for m in warnings)


def test_the_summary_line_counts_the_experts_it_cannot_see(
        monkeypatch, mock_account, mock_account_def):
    """The closing line is the one an operator actually reads. It must not be able to
    say "0 armed" in a tone that means "all good"."""
    _screener_expert(mock_account_def.id)
    _gated_expert(mock_account_def.id, ["AAPL"])
    _patch_account(monkeypatch, mock_account)

    infos, _ = _report_lines(monkeypatch)
    summary = [m for m in infos if m.startswith("IV-rank gate readiness:")][-1]
    assert "1 expert(s) with an UNKNOWN universe" in summary


def test_a_recovered_universe_is_reported_as_recovered(
        monkeypatch, mock_account, mock_account_def):
    """An operator must be able to tell a trailing observation from a configured list —
    a recovered universe shrinks the moment the expert stops running."""
    inst = _screener_expert(mock_account_def.id)
    _analysed(inst.id, "AAPL")
    _patch_account(monkeypatch, mock_account)

    infos, _ = _report_lines(monkeypatch)
    blob = "\n".join(infos)
    assert "AAPL" in blob
    assert audit.UNIVERSE_RECENT_ANALYSES in blob


def test_the_recorder_warns_when_it_cannot_see_any_gated_universe(
        monkeypatch, mock_account, mock_account_def):
    """Not just the report: a recorder pass with gates but no resolvable targets used to
    log "No iv_rank-gated rules configured" at DEBUG — the exact opposite of the truth."""
    import ba2_trade_platform.core.TradeManager as tm_mod
    from ba2_trade_platform.core.TradeManager import TradeManager
    _screener_expert(mock_account_def.id)
    _patch_account(monkeypatch, mock_account)
    warnings = _capture(monkeypatch, tm_mod, "warning")

    TradeManager().record_daily_iv_snapshots()

    assert get_all_instances(OptionIVSnapshot) == []
    assert any("universe" in m.lower() for m in warnings)


def test_a_genuinely_unconfigured_platform_stays_quiet(monkeypatch, mock_account_def):
    """No gated rules at all is not a problem and must not warn — a report that cries
    wolf on every start is a report nobody reads."""
    import ba2_trade_platform.core.TradeManager as tm_mod
    from ba2_trade_platform.core.TradeManager import TradeManager
    warnings = _capture(monkeypatch, tm_mod, "warning")

    TradeManager().record_daily_iv_snapshots()
    TradeManager().report_iv_rank_readiness()

    assert warnings == []


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
