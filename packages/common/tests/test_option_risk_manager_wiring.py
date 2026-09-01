"""The option risk manager, wired: ONE implementation reached by live and by the backtest.

Design: ``docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`` §4 (the binding
operator decision of 2026-08-30) and review finding **F5** — ``option_book.check_rails`` /
``admit`` and the breaker's halted gate had ZERO production callers, so every sleeve rail was
enforced nowhere and the circuit breaker gated nothing after the bar it flattened on.

Each test below pins one of the five things that decision requires:

* the mode is admitted, and an unknown mode refuses LOUDLY rather than defaulting;
* the default (absent) configuration reaches not one line of the risk manager;
* the rails REJECT and ADMIT at their boundaries, through a real production call;
* a halted sleeve opens nothing;
* live and backtest reach the SAME implementation — the whole content of the decision, so a
  future divergence has to fail a named test.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.option_book import (
    RAIL_BREAKER_HALTED, RAIL_MAX_CONCURRENT, RAIL_MAX_DEPLOYMENT,
    RAIL_MAX_NOTIONAL_LEVERAGE, RAIL_ONE_PER_UNDERLYING, RAIL_UNDEFINED_RISK,
    RAIL_UNMEASURABLE_CANDIDATE, BreakerState, update_breaker,
)
from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
from ba2_common.core.option_request import (
    OPTION_RAILS_UNCONFIGURED_REFUSAL, OPTION_RAIL_REFUSAL, REFUSAL_PHRASES,
)
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection


# --------------------------------------------------------------------------- doubles
RAILS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 40.0,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
}


class FakeExpert:
    """An expert that declares a mode and the sleeve rails, like the real accessor."""

    def __init__(self, mode: str = "classic_options", rails: Optional[Dict[str, Any]] = None):
        self.settings = {"risk_manager_mode": mode}
        self._rails = RAILS if rails is None else rails

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in self._rails:
            raise ValueError(f"{key} is not declared by this expert")
        return self._rails[key]


class FakeAccount:
    def __init__(self, balance: Optional[float] = 100_000.0,
                 cash: Optional[float] = 100_000.0):
        self._balance = balance
        self._cash = cash

    def get_balance(self):
        return self._balance

    def cash_available_for_delivery(self):
        return self._cash


def short_put_legs(strike: float = 100.0, symbol: str = "AAPL") -> List[OptionLeg]:
    return [OptionLeg(contract_symbol=f"{symbol}260116P{int(strike)}",
                      side=OrderDirection.SELL, ratio_qty=1,
                      option_type=OptionRight.PUT, strike=strike,
                      expiry=date(2026, 1, 16), underlying=symbol)]


def put_spread_legs(short_strike: float = 100.0, long_strike: float = 95.0,
                    symbol: str = "AAPL") -> List[OptionLeg]:
    """A DEFINED-RISK put vertical: short 100 / long 95.

    Used wherever a test is about the deployment or leverage rail rather than the naked
    sub-cap. A bare short put measures as undefined risk (``structure_metrics`` reports
    ``is_defined_risk=False`` for any uncovered short), so testing the deployment cap with
    one would be testing ``undefined_risk_max_pct`` under another name.
    """
    return [OptionLeg(contract_symbol=f"{symbol}260116P{int(short_strike)}",
                      side=OrderDirection.SELL, ratio_qty=1,
                      option_type=OptionRight.PUT, strike=short_strike,
                      expiry=date(2026, 1, 16), underlying=symbol),
            OptionLeg(contract_symbol=f"{symbol}260116P{int(long_strike)}",
                      side=OrderDirection.BUY, ratio_qty=1,
                      option_type=OptionRight.PUT, strike=long_strike,
                      expiry=date(2026, 1, 16), underlying=symbol)]


def long_call_legs(strike: float = 100.0, symbol: str = "AAPL") -> List[OptionLeg]:
    return [OptionLeg(contract_symbol=f"{symbol}260116C{int(strike)}",
                      side=OrderDirection.BUY, ratio_qty=1,
                      option_type=OptionRight.CALL, strike=strike,
                      expiry=date(2026, 1, 16), underlying=symbol)]


def held_short_put(txn_id: int, underlying: str, strike: float, qty: float = 1.0,
                   credit: float = 2.0) -> OptionStructure:
    """One OPEN cash-secured put in the sleeve, as the book sees it."""
    return OptionStructure(
        transaction_id=txn_id, underlying=underlying, strategy="cash_secured_put",
        legs=(LifecycleLeg(contract_symbol=f"{underlying}260116P{int(strike)}",
                           net_qty=-qty, strike=strike, option_type=OptionRight.PUT,
                           expiry=date(2026, 1, 16), underlying=underlying),),
        quantity=qty, multiplier=100, entry_net_premium=-credit)


@pytest.fixture(autouse=True)
def _clean_state():
    rm.reset_state()
    yield
    rm.reset_state()


@pytest.fixture
def empty_sleeve(monkeypatch):
    """No open option structures — the sleeve every boundary test starts from."""
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))


@pytest.fixture
def ledger_holds(monkeypatch):
    """Say which transactions the ledger still shows as WAITING or OPENED.

    In-flight charges are swept against this: a charge whose entry was rejected or
    cancelled never became exposure, and keeping it would block the sleeve for the life of
    the process. The tests that are about CHARGING therefore have to state that their
    transaction still exists, or they would be measuring the sweep instead.
    """
    def _hold(*transaction_ids):
        monkeypatch.setattr(rm, "_live_transaction_ids",
                            lambda eid: set(transaction_ids))
    return _hold


def gate(expert=None, account=None, legs=None, quantity=1,
         max_loss_per_contract=1_000.0, strategy="cash_secured_put",
         underlying="AAPL", expert_instance_id=7):
    return rm.admit_option_entry(
        expert=expert or FakeExpert(), account=account or FakeAccount(),
        expert_instance_id=expert_instance_id, underlying=underlying,
        option_strategy=strategy, legs=legs if legs is not None else short_put_legs(),
        quantity=quantity, max_loss_per_contract=max_loss_per_contract)


# ---------------------------------------------------------------------------
# 1. the mode
# ---------------------------------------------------------------------------
def test_classic_options_is_an_admitted_risk_manager_mode():
    """Before this, ``risk_manager_mode`` accepted only classic/smart, so NOTHING could
    select the option risk manager and no option run could ever be produced."""
    from ba2_common.core.utils import get_risk_manager_mode

    assert "classic_options" in rm.VALID_RISK_MANAGER_MODES
    assert get_risk_manager_mode({"risk_manager_mode": "classic_options"}) == "classic_options"
    assert rm.option_risk_manager_enabled({"risk_manager_mode": "classic_options"}) is True


def test_the_settings_definition_offers_the_option_mode_from_one_list():
    """The UI dropdown and the code must not carry two copies of the admitted set."""
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    MarketExpertInterface._ensure_builtin_settings()
    values = MarketExpertInterface._builtin_settings["risk_manager_mode"]["valid_values"]
    assert values == list(rm.VALID_RISK_MANAGER_MODES)


@pytest.fixture
def warnings_logged(monkeypatch):
    """The WARNING lines this module emits.

    Not ``caplog``: the project logger does not propagate to the root handler pytest
    captures, so a caplog assertion here passes whether or not anything was logged."""
    recorded: List[str] = []
    monkeypatch.setattr(rm.logger, "warning",
                        lambda msg, *a, **k: recorded.append(str(msg)))
    return recorded


def test_an_unadmitted_mode_does_not_engage_the_gate_and_does_not_raise(warnings_logged):
    """THE DISPATCH QUESTION FAILS OPEN (review finding H2, 2026-09-01), and only it.

    ``option_risk_manager_enabled`` is called from ``_option_risk_manager``, OUTSIDE the
    guarded option path. While it RAISED, one expert whose ``risk_manager_mode`` held an
    unadmitted string aborted the remaining actions of the whole Phase-1 entry pass under
    ``BA2_ERROR_MODE=enforce`` -- entries that had worked before the branch existed, on
    experts with no option in them. The gate engages on ``classic_options`` and nothing
    else; anything else keeps the legacy path and says so, loudly, in the log."""
    assert rm.option_risk_manager_enabled({"risk_manager_mode": "classic-options"},
                                          expert_instance_id=11) is False
    message = " | ".join(warnings_logged)
    assert "classic-options" in message           # names the offending value
    assert "classic_options" in message           # names what was admitted instead
    assert "11" in message                        # names the instance to fix


def test_the_string_None_reads_as_no_setting_at_all(warnings_logged):
    """THE REAL POPULATION. ``ExtendableSettingsInterface`` line 87 documents it: ``str(None)``
    was once written to the settings table, so live rows carry the literal ``"None"``. Under
    the raising gate that value killed the entry pass of a CLASSIC expert; it must be
    indistinguishable from having no setting at all, except for the warning."""
    assert rm.option_risk_manager_enabled({"risk_manager_mode": "None"},
                                          expert_instance_id=12) is False
    assert rm.option_risk_manager_enabled({}) is False          # the reference behaviour
    assert "'None'" in " | ".join(warnings_logged)


def test_the_unadmitted_mode_warning_is_once_per_instance_not_once_per_action(
        warnings_logged):
    """A Phase-1 pass consults this gate once per candidate STRUCTURE. One line per action
    is how a real warning becomes log noise nobody reads."""
    for _ in range(5):
        rm.option_risk_manager_enabled({"risk_manager_mode": "None"},
                                       expert_instance_id=13)
    rm.option_risk_manager_enabled({"risk_manager_mode": "None"},
                                   expert_instance_id=14)
    assert len(warnings_logged) == 2, warnings_logged


def test_the_admitted_set_is_still_a_closed_list():
    """The leniency is about DISPATCH, not about what counts as a mode. ``normalise_``
    still raises, and it is still the single reader of ``VALID_RISK_MANAGER_MODES``."""
    with pytest.raises(rm.OptionRiskManagerModeError):
        rm.normalise_risk_manager_mode({"risk_manager_mode": "classic-options"})


def test_an_absent_mode_is_classic_and_never_the_option_risk_manager():
    """The default configuration, pinned: absent / empty / whitespace is classic."""
    for settings in ({}, None, {"risk_manager_mode": ""}, {"risk_manager_mode": "   "}):
        assert rm.normalise_risk_manager_mode(settings) == "classic"
        assert rm.option_risk_manager_enabled(settings) is False


def test_smart_mode_does_not_run_the_option_risk_manager():
    assert rm.option_risk_manager_enabled({"risk_manager_mode": "smart"}) is False


# ---------------------------------------------------------------------------
# 2. the rails REJECT and ADMIT at their boundaries
# ---------------------------------------------------------------------------
def test_a_candidate_within_every_rail_is_admitted(empty_sleeve):
    verdict = gate()
    assert verdict.allowed is True
    assert verdict.phrase == ""
    assert verdict.verdict.evaluated  # the rails actually RAN, they were not skipped


def test_max_deployment_admits_exactly_at_the_cap(monkeypatch):
    """40% of 100k is 40,000 and a 40,000 structure fits. Equal ADMITS — the same boundary
    every other option cap uses, so one gate with a different boundary cannot creep in."""
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    verdict = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                   max_loss_per_contract=40_000.0)
    assert verdict.allowed is True


def test_max_deployment_declines_one_cent_over_the_cap(monkeypatch):
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    verdict = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                   max_loss_per_contract=40_000.01)
    assert verdict.allowed is False
    assert verdict.reason == RAIL_MAX_DEPLOYMENT
    assert verdict.phrase == OPTION_RAIL_REFUSAL
    assert "max_deployment_pct" in verdict.detail


def test_the_aggregate_cap_counts_what_the_sleeve_ALREADY_holds(monkeypatch):
    """F5's substantive gap: there was no aggregate max-loss cap anywhere. A structure that
    fits on its own must still be refused when the BOOK is already at the cap."""
    monkeypatch.setattr(rm, "sleeve_structures",
                        lambda eid: ([held_short_put(1, "MSFT", 390.0)], []))
    # 390 x 100 = 39,000 committed; 40,000 cap; a 2,000 structure no longer fits.
    verdict = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                   max_loss_per_contract=2_000.0, underlying="AAPL")
    assert verdict.allowed is False
    assert verdict.reason == RAIL_MAX_DEPLOYMENT
    # ... and a 1,000 one does, so the test is not passing because everything is refused.
    assert gate(legs=put_spread_legs(), strategy="bull_put_spread",
                max_loss_per_contract=1_000.0, underlying="AAPL").allowed is True


def test_one_structure_per_underlying(monkeypatch):
    monkeypatch.setattr(rm, "sleeve_structures",
                        lambda eid: ([held_short_put(1, "AAPL", 100.0)], []))
    verdict = gate(underlying="AAPL", max_loss_per_contract=100.0)
    assert verdict.allowed is False
    assert verdict.reason == RAIL_ONE_PER_UNDERLYING


def test_the_concurrent_structure_cap_binds(monkeypatch):
    book = [held_short_put(i, f"SYM{i}", 10.0) for i in range(1, 11)]
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: (book, []))
    verdict = gate(underlying="AAPL", max_loss_per_contract=1.0)
    assert verdict.allowed is False
    assert verdict.reason == RAIL_MAX_CONCURRENT


def test_notional_leverage_declines_a_book_too_levered(monkeypatch):
    """The leverage rail is SHORT-side notional, so it needs a short candidate to bind."""
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    account = FakeAccount(balance=10_000.0, cash=10_000_000.0)
    rails = dict(RAILS, max_deployment_pct=1_000_000.0, undefined_risk_max_pct=1_000_000.0)
    # 1 short put at strike 400 = 40,000 of short notional against 10,000 x 3 = 30,000.
    verdict = gate(expert=FakeExpert(rails=rails), account=account,
                   legs=short_put_legs(400.0), max_loss_per_contract=1.0)
    assert verdict.allowed is False
    assert verdict.reason == RAIL_MAX_NOTIONAL_LEVERAGE


def test_the_undefined_risk_subcap_binds_on_a_naked_short(monkeypatch):
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    rails = dict(RAILS, undefined_risk_max_pct=1.0)      # 1% of 100k = 1,000
    verdict = gate(expert=FakeExpert(rails=rails), strategy="short_put",
                   max_loss_per_contract=2_000.0)
    assert verdict.allowed is False
    assert verdict.reason == RAIL_UNDEFINED_RISK


def test_an_unmeasurable_max_loss_is_never_a_free_trade(empty_sleeve):
    """UNBOUNDED and UNMEASURABLE both arrive as ``None`` from the submit path (design
    SS8.3's default refusal for undefined risk). Neither may read as zero.

    The candidate deliberately CARRIES NOTIONAL (a put vertical, not a long call): a
    long-only structure with a zero max loss is caught by the rails' "risks nothing and
    controls nothing" branch anyway, so it would pass this test even if ``None`` were read
    as ``0.0``. Mutation target: ``float(max_loss_per_contract or 0.0)``."""
    verdict = gate(max_loss_per_contract=None, legs=put_spread_legs(),
                   strategy="bull_put_spread")
    assert verdict.allowed is False
    assert verdict.reason == RAIL_UNMEASURABLE_CANDIDATE


def test_an_unreadable_balance_declines(empty_sleeve):
    verdict = gate(account=FakeAccount(balance=None))
    assert verdict.allowed is False


def test_a_transaction_the_ledger_cannot_describe_makes_the_whole_book_decline(monkeypatch):
    """An OPENED option position with no executed leg is not a zero-cost one."""
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], [42]))
    verdict = gate()
    assert verdict.allowed is False
    assert "42" in verdict.detail


# ---------------------------------------------------------------------------
# 3. the breaker's halted gate — F5's other half
# ---------------------------------------------------------------------------
def test_a_halted_sleeve_opens_nothing(empty_sleeve):
    """The breaker flattened the book and then gated NOTHING: the next entry cycle
    re-opened it at the bottom of the drawdown. This is the gate that stops that."""
    state = update_breaker(BreakerState(), 10_000.0, {"circuit_breaker_pct": 20.0})
    state = update_breaker(state, 7_000.0, {"circuit_breaker_pct": 20.0})
    assert (state.tripped, state.halted) == (True, True)
    rm.set_breaker_state(7, state)

    verdict = gate()
    assert verdict.allowed is False
    assert verdict.reason == RAIL_BREAKER_HALTED


def test_a_recovered_sleeve_opens_again(empty_sleeve):
    """Recovery past the re-arm line lifts the stand-down — otherwise the gate is a trap
    with no exit, and 'halted forever' becomes a terminal state."""
    settings = {"circuit_breaker_pct": 20.0}
    state = update_breaker(BreakerState(), 10_000.0, settings)
    state = update_breaker(state, 7_000.0, settings)
    state = update_breaker(state, 9_500.0, settings)      # back inside -10%
    assert state.halted is False
    rm.set_breaker_state(7, state)
    assert gate().allowed is True


def test_the_entry_gate_never_consumes_the_breaker_EDGE(empty_sleeve):
    """LOAD BEARING. ``tripped`` is an edge the exit pass reads to flatten the book. If the
    entry gate updated the breaker it could consume that edge on a bar where the exit pass
    had not yet run, and the flatten would never be signalled at all. The entry gate READS."""
    rm.set_breaker_state(7, BreakerState(peak_equity=10_000.0))
    gate(account=FakeAccount(balance=1_000.0))
    assert rm.get_breaker_state(7) == BreakerState(peak_equity=10_000.0)


def test_rearm_is_the_operator_override_and_keeps_the_peak():
    state = update_breaker(BreakerState(), 10_000.0, {"circuit_breaker_pct": 20.0})
    state = update_breaker(state, 7_000.0, {"circuit_breaker_pct": 20.0})
    rm.set_breaker_state(7, state)
    cleared = rm.rearm_breaker(7)
    assert cleared.halted is False
    assert cleared.peak_equity == 10_000.0


# ---------------------------------------------------------------------------
# 4. rails that are not configured refuse rather than defaulting
# ---------------------------------------------------------------------------
def test_an_expert_with_no_declared_rails_refuses_the_entry(empty_sleeve):
    """NEVER a substituted default for a risk rail. The refusal names the missing knobs so
    'this sleeve stopped trading' is diagnosable instead of mysterious."""
    verdict = gate(expert=FakeExpert(rails={}))
    assert verdict.allowed is False
    assert verdict.phrase == OPTION_RAILS_UNCONFIGURED_REFUSAL
    for key in rm.REQUIRED_RAIL_SETTINGS:
        assert key in verdict.detail


def test_the_unconfigured_refusal_is_not_the_rail_refusal():
    """A rail that declined is the system working; a rail that does not exist is the
    operator's to fix. Collapsing the two reports a missing setting as a breached limit."""
    assert OPTION_RAILS_UNCONFIGURED_REFUSAL != OPTION_RAIL_REFUSAL
    assert OPTION_RAIL_REFUSAL in REFUSAL_PHRASES
    assert OPTION_RAILS_UNCONFIGURED_REFUSAL in REFUSAL_PHRASES


def test_the_base_interface_declares_every_required_rail():
    """Any expert can be switched to classic_options, so the rails must be configurable on
    the base class — before this they existed only on PremiumSeller."""
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    MarketExpertInterface._ensure_builtin_settings()
    for key in rm.REQUIRED_RAIL_SETTINGS + (rm.UNDEFINED_RISK_SETTING,):
        assert key in MarketExpertInterface._builtin_settings, key


# ---------------------------------------------------------------------------
# 5. what is on the wire is charged before the book can see it
# ---------------------------------------------------------------------------
def test_a_structure_submitted_this_cycle_is_charged_to_the_next_candidate(empty_sleeve,
                                                                            ledger_holds):
    """Its transaction is WAITING with no executed leg, so ``book_totals`` cannot see it.
    Without the pending charge, three 20k structures each measure an empty book and all
    three open under a 40k cap — the concentrated book the rails exist to stop."""
    ledger_holds(101)
    first = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                 underlying="AAPL", max_loss_per_contract=30_000.0)
    assert first.allowed is True
    rm.record_submitted(7, 101, first.candidate)

    second = gate(legs=put_spread_legs(symbol="MSFT"), strategy="bull_put_spread",
                  underlying="MSFT", max_loss_per_contract=20_000.0)
    assert second.allowed is False
    assert second.reason == RAIL_MAX_DEPLOYMENT


def test_a_pending_charge_is_dropped_once_the_book_can_see_it(monkeypatch, ledger_holds):
    """Otherwise the same structure is charged twice and the sleeve stops trading."""
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    ledger_holds(101)
    first = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                 underlying="AAPL", max_loss_per_contract=30_000.0)
    rm.record_submitted(7, 101, first.candidate)
    assert len(rm.pending_charges(7)) == 1

    # The transaction has filled and is now OPENED: the book counts it, the charge must not.
    monkeypatch.setattr(rm, "sleeve_structures",
                        lambda eid: ([held_short_put(101, "AAPL", 300.0)], []))
    gate(underlying="MSFT", legs=short_put_legs(symbol="MSFT"),
         max_loss_per_contract=100.0)
    assert rm.pending_charges(7) == ()


# ---------------------------------------------------------------------------
# 6. the candidate is priced from what was PERSISTED, never reconstructed
# ---------------------------------------------------------------------------
def test_the_candidates_max_loss_is_the_handed_in_per_contract_value():
    """Design SS8.2: read back what submit stamped. No leg reconstruction, no OCC parsing."""
    candidate = rm.candidate_from_entry(
        underlying="AAPL", option_strategy="cash_secured_put", legs=short_put_legs(100.0),
        quantity=3, max_loss_per_contract=9_800.0)
    assert candidate.max_loss == pytest.approx(29_400.0)          # 3 x the stamped value
    assert candidate.notional == pytest.approx(30_000.0)          # 100 x 100 x 3
    assert candidate.short_put_assignment == pytest.approx(30_000.0)


def test_a_long_only_candidate_owes_no_notional_and_no_delivery():
    candidate = rm.candidate_from_entry(
        underlying="AAPL", option_strategy="single", legs=long_call_legs(),
        quantity=2, max_loss_per_contract=500.0)
    assert candidate.notional == 0.0
    assert candidate.short_put_assignment == 0.0
    assert candidate.max_loss == pytest.approx(1_000.0)


def test_assignment_capacity_declines_a_book_that_could_not_take_delivery(monkeypatch):
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    account = FakeAccount(balance=100_000.0, cash=5_000.0)
    verdict = gate(account=account, legs=short_put_legs(100.0), quantity=1,
                   max_loss_per_contract=9_800.0)
    assert verdict.allowed is False
    assert "delivery" in verdict.detail


# ---------------------------------------------------------------------------
# 7. the run record — the shape the runs table already reads
# ---------------------------------------------------------------------------
def test_every_decision_is_journalled_admissions_and_refusals_alike(empty_sleeve):
    gate(underlying="AAPL", max_loss_per_contract=1_000.0)
    gate(underlying="MSFT", legs=short_put_legs(symbol="MSFT"),
         max_loss_per_contract=99_000_000.0)
    entries = rm.journal(7)
    assert [e["success"] for e in entries] == [True, False]
    assert entries[0]["arguments"]["symbol"] == "AAPL"
    assert entries[1]["result"]["rail"] == RAIL_MAX_DEPLOYMENT


def test_a_backtest_writes_no_run_record(empty_sleeve, monkeypatch):
    """A grid evaluates tens of thousands of entries and has no runs table to read them;
    writing them would put grid noise into the live database."""
    gate()
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    called = []
    monkeypatch.setattr("ba2_common.core.db.add_instance",
                        lambda obj: called.append(obj) or 1)
    assert rm.flush_option_rm_run(7, 3) is None
    assert called == []


def test_the_run_record_is_the_shape_the_runs_table_reads(empty_sleeve, monkeypatch):
    gate(underlying="AAPL", max_loss_per_contract=1_000.0)
    gate(underlying="MSFT", legs=short_put_legs(symbol="MSFT"),
         max_loss_per_contract=99_000_000.0)
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: False)
    written = []
    monkeypatch.setattr("ba2_common.core.db.add_instance",
                        lambda obj: (written.append(obj), 55)[1])

    assert rm.flush_option_rm_run(7, 3) == 55
    job = written[0]
    assert job.expert_instance_id == 7 and job.account_id == 3
    assert job.model_used == "classic_options"
    assert job.status == "COMPLETED"
    assert job.actions_taken_count == 1              # one admitted, one refused
    assert len(job.graph_state["actions_log"]) == 2
    # The renderer reads action_type / success / arguments.symbol out of each entry.
    assert job.graph_state["actions_log"][0]["arguments"]["symbol"] == "AAPL"
    # Drained: a second flush must not re-write the same decisions.
    assert rm.flush_option_rm_run(7, 3) is None


def test_the_journal_cannot_grow_without_bound(empty_sleeve):
    """A backtest runs one process for tens of thousands of entries."""
    for _ in range(rm._JOURNAL_LIMIT + 25):
        gate(underlying="AAPL", max_loss_per_contract=1_000.0)
    assert len(rm.journal(7)) == rm._JOURNAL_LIMIT


def test_a_charge_whose_entry_never_opened_stops_blocking_the_sleeve(empty_sleeve,
                                                                     monkeypatch):
    """A rejected or cancelled entry never became exposure. Charging the sleeve for it
    forever would take the book out of service for the life of the process — a risk
    manager that quietly stops trading is worse than one that refuses out loud."""
    first = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                 underlying="AAPL", max_loss_per_contract=30_000.0)
    rm.record_submitted(7, 101, first.candidate)
    monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: set())   # it was cancelled

    second = gate(legs=put_spread_legs(symbol="MSFT"), strategy="bull_put_spread",
                  underlying="MSFT", max_loss_per_contract=20_000.0)
    assert second.allowed is True
    assert rm.pending_charges(7) == ()


def test_an_unreadable_ledger_KEEPS_the_charge(empty_sleeve, monkeypatch):
    """``None`` is "I could not read the ledger", not "everything was cancelled". Reading
    it as the latter would forgive every in-flight charge exactly when the system is least
    able to tell what it holds."""
    first = gate(legs=put_spread_legs(), strategy="bull_put_spread",
                 underlying="AAPL", max_loss_per_contract=30_000.0)
    rm.record_submitted(7, 101, first.candidate)
    monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: None)

    second = gate(legs=put_spread_legs(symbol="MSFT"), strategy="bull_put_spread",
                  underlying="MSFT", max_loss_per_contract=20_000.0)
    assert second.allowed is False
    assert second.reason == RAIL_MAX_DEPLOYMENT


# ---------------------------------------------------------------------------
# 8. whose sleeve is it: one latch live, one per trial in a backtest
# ---------------------------------------------------------------------------
def test_the_live_sleeve_key_is_process_wide_not_per_thread(monkeypatch):
    """The exit pass runs on the JobManager thread and entries on WorkerQueue threads. A
    thread-local latch would mean the breaker that flattens the book gates no entry at all
    — F5, reintroduced by an isolation mechanism."""
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: False)
    keys = []
    t = __import__("threading").Thread(target=lambda: keys.append(rm._sleeve_key(7)))
    t.start()
    t.join()
    assert keys == [rm._sleeve_key(7)]


def test_two_concurrent_backtest_trials_do_not_share_a_sleeve(monkeypatch):
    """The GA runs trials concurrently in worker threads of ONE process, reusing the same
    expert instance ids. A shared latch would let trial B's drawdown stand trial A's sleeve
    down, and a GA result that depends on what another trial did is not reproducible."""
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    keys = []
    t = __import__("threading").Thread(target=lambda: keys.append(rm._sleeve_key(7)))
    t.start()
    t.join()
    assert keys != [rm._sleeve_key(7)]


def test_a_fresh_run_starts_from_a_clean_sleeve():
    """Sequential trials on one worker thread share a key, so the run boundary has to clear
    the state — ``backtest_trading_db`` calls this."""
    rm.set_breaker_state(7, BreakerState(peak_equity=10_000.0, halted=True))
    rm.reset_state()
    assert rm.get_breaker_state(7) == BreakerState()
    assert rm.pending_charges(7) == ()
    assert rm.journal(7) == ()


# ---------------------------------------------------------------------------
# 8. the verified stock cover: a covered call PASSES the rails (2026-08-31)
# ---------------------------------------------------------------------------
# Operator decision 2026-08-31: a covered call's cover is held stock OUTSIDE the
# order's legs, so the legs alone measured it UNBOUNDED (no stamp) and, worse, the
# candidate metrics read it as NAKED. The builder that VERIFIED the shares now
# supplies ``stock_cover_price`` (current spot -- the same value the max-loss stamp
# used), and the candidate becomes COVERED: its measured max loss charges the
# deployment cap, never the ``undefined_risk_max_pct`` sub-cap.
def short_call_legs(strike: float = 105.0, symbol: str = "AAPL") -> List[OptionLeg]:
    return [OptionLeg(contract_symbol=f"{symbol}260116C{int(strike)}",
                      side=OrderDirection.SELL, ratio_qty=1,
                      option_type=OptionRight.CALL, strike=strike,
                      expiry=date(2026, 1, 16), underlying=symbol)]


def test_a_covered_call_with_verified_cover_is_admitted_as_COVERED_risk(empty_sleeve):
    """The rails-passing covered call: measured max loss, deployment-cap charge, and a
    zero naked charge -- the overlay lane is reachable."""
    verdict = rm.admit_option_entry(
        expert=FakeExpert(), account=FakeAccount(), expert_instance_id=7,
        underlying="AAPL", option_strategy="covered_call", legs=short_call_legs(),
        quantity=1, max_loss_per_contract=9_700.0, stock_cover_price=100.0)
    assert verdict.allowed is True, verdict.detail
    assert verdict.candidate.is_defined_risk is True
    book = verdict.verdict.book_after
    assert book.committed == pytest.approx(9_700.0)
    assert book.naked_committed == pytest.approx(0.0)


def test_the_undefined_risk_subcap_never_binds_on_a_verified_covered_call(empty_sleeve):
    """Side by side under a 1% naked sub-cap: the covered call (cover supplied) is
    admitted while a naked short put of the SAME max loss declines on that very rail --
    covered risk is charged to the deployment cap, not the sub-cap."""
    rails = dict(RAILS, undefined_risk_max_pct=1.0)      # 1% of 100k = 1,000
    covered = rm.admit_option_entry(
        expert=FakeExpert(rails=rails), account=FakeAccount(), expert_instance_id=7,
        underlying="AAPL", option_strategy="covered_call", legs=short_call_legs(),
        quantity=1, max_loss_per_contract=9_700.0, stock_cover_price=100.0)
    naked = rm.admit_option_entry(
        expert=FakeExpert(rails=rails), account=FakeAccount(), expert_instance_id=7,
        underlying="MSFT", option_strategy="short_put",
        legs=short_put_legs(symbol="MSFT"), quantity=1,
        max_loss_per_contract=9_700.0)
    assert covered.allowed is True, covered.detail
    assert naked.allowed is False
    assert naked.reason == RAIL_UNDEFINED_RISK


def test_a_covered_call_with_NO_cover_still_declines(empty_sleeve):
    """The guard, at the RM: no cover supplied means the submit path measured the bare
    short call UNBOUNDED, handed over ``max_loss_per_contract=None``, and the candidate
    metrics still read NAKED -- the strategy NAME buys nothing."""
    verdict = rm.admit_option_entry(
        expert=FakeExpert(), account=FakeAccount(), expert_instance_id=7,
        underlying="AAPL", option_strategy="covered_call", legs=short_call_legs(),
        quantity=1, max_loss_per_contract=None)
    assert verdict.allowed is False
    assert verdict.candidate.is_defined_risk is False


def test_the_cover_declaration_never_overrides_an_UNMEASURABLE_metrics_answer():
    """``stock_cover_price`` flips only a MEASURED False to True. Legs the metrics cannot
    read (no strike) stay ``is_defined_risk=None`` -- unknown is not covered."""
    unreadable = [OptionLeg(contract_symbol="AAPL260116C105", side=OrderDirection.SELL,
                            ratio_qty=1, option_type=OptionRight.CALL, strike=None,
                            expiry=date(2026, 1, 16), underlying="AAPL")]
    candidate = rm.candidate_from_entry(
        underlying="AAPL", option_strategy="covered_call", legs=unreadable, quantity=1,
        max_loss_per_contract=9_700.0, stock_cover_price=100.0)
    assert candidate.is_defined_risk is None
