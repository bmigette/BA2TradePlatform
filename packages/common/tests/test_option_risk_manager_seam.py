"""The SEAM: one option risk manager, reached by live and by the backtest, once.

Design §4's operator decision (2026-08-30) is not "an option risk manager exists" — it is
that there is exactly **one** of it and that both runtimes go through it. Prose has claimed
that before and the program review (F5) found it false in both directions, so the claim is
pinned here as tests that a future divergence must break:

* ``_OptionEntryAction._submit_option_order`` is the ONE wiring point — the choke point all
  seventeen builders end at — and ``admit_option_entry`` is called from nowhere else in
  production code. A hook added to ``daily_engine`` (backtest-only) or ``JobManager``
  (live-only) fails ``test_the_option_risk_manager_has_exactly_one_production_wiring_point``.
* A live-shaped account and a backtest-shaped account driven through that seam reach the
  same function object and get the same verdict.
* An expert that has not opted in reaches not one line of it, so every existing live and
  backtest path is byte-for-byte what it was.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.TradeActions as ta
from ba2_common.core.instance_resolver import set_instance_resolver
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.option_request import OPTION_RAIL_REFUSAL
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

EXPIRY = date(2026, 3, 20)

RAILS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 40.0,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
    "circuit_breaker_pct": 20.0,      # a REQUIRED rail since 2026-09-01
}


class _Expert:
    def __init__(self, mode: str, rails: Optional[Dict[str, Any]] = None):
        self.settings = {"risk_manager_mode": mode}
        self._rails = RAILS if rails is None else rails

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in self._rails:
            raise ValueError(key)
        return self._rails[key]


class _Resolver:
    def __init__(self, expert):
        self._expert = expert

    def get_expert_instance(self, expert_id):
        return self._expert

    def get_account_instance(self, account_id):
        return None

    def get_account_instance_from_transaction(self, transaction):
        return None


class _LiveShapedAccount(OptionsAccountInterface):
    """A broker account: no simulated clock, so ``_today`` reads the wall clock."""

    def __init__(self, balance=100_000.0, cash=100_000.0):
        self.id = 1
        self.submitted: List[Dict[str, Any]] = []
        self._balance = balance
        self._cash = cash

    # -- the bits the submit seam touches
    def get_balance(self):
        return self._balance

    def get_account_info(self):
        """What ``ReadOnlyAccountInterface.get_account_snapshot`` probes for the sleeve's
        equity. This double holds no positions, so its cash and its equity coincide."""
        return {"equity": self._balance, "cash": self._balance, "balance": self._balance}

    #: THE real reader, borrowed as a function rather than imitated: ``OptionsAccountInterface``
    #: is a mixin and does not inherit it, but every real account does, so ``sleeve_equity``
    #: must reach equity down the same tolerant ``get_account_info()`` probe here.
    get_account_snapshot = ReadOnlyAccountInterface.get_account_snapshot
    #: ...and the reader the BREAKER uses, borrowed for the same reason.
    true_equity = ReadOnlyAccountInterface.true_equity

    def cash_available_for_delivery(self):
        return self._cash

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity, strategy=option_strategy))
        return SimpleNamespace(id=901, transaction_id=77, data={})

    # -- unused abstract surface
    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_chain(self, underlying, expiry_from=None, expiry_to=None,
                         option_type=None, strike_min=None, strike_max=None):
        return []

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None


class _BacktestShapedAccount(_LiveShapedAccount):
    """A simulator: it publishes ``_as_of_date``, which is how the shared action code tells
    a backtest clock from a wall clock. Nothing else about the seam differs."""

    def _as_of_date(self):
        return date(2026, 1, 5)


def _spread_legs(symbol="XYZ"):
    return [OptionLeg(contract_symbol=f"{symbol}260320P100", side=OrderDirection.SELL,
                      position_intent="sell_to_open", option_type=OptionRight.PUT,
                      strike=100.0, expiry=EXPIRY, underlying=symbol),
            OptionLeg(contract_symbol=f"{symbol}260320P95", side=OrderDirection.BUY,
                      position_intent="buy_to_open", option_type=OptionRight.PUT,
                      strike=95.0, expiry=EXPIRY, underlying=symbol)]


def _action(account, instance_id=7):
    rec = SimpleNamespace(id=1, instance_id=instance_id, data=None, price_at_date=100.0,
                          expected_profit_percent=None, recommended_action=None,
                          confidence=80.0)
    action = ta.create_action(ta.ExpertActionType.OPEN_BULL_PUT_SPREAD, "XYZ", account,
                              SimpleNamespace(), None, rec)
    action.submit_to_broker = True
    return action


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    yield
    rm.reset_state()


def _with_mode(mode: str):
    """Install a resolver for one test and RESTORE the previous one.

    Never reset-to-unconfigured: the resolver is a process-wide seam the host installs once,
    and clobbering it leaks into every later test in the session.
    """
    from ba2_common.core.instance_resolver import get_instance_resolver

    previous = get_instance_resolver()
    set_instance_resolver(_Resolver(_Expert(mode)))
    try:
        yield
    finally:
        set_instance_resolver(previous)


@pytest.fixture
def opted_in():
    yield from _with_mode("classic_options")


@pytest.fixture
def classic():
    yield from _with_mode("classic")


# ---------------------------------------------------------------------------
# the default is a provable no-op
# ---------------------------------------------------------------------------
def test_a_default_expert_never_reaches_the_option_risk_manager(classic, monkeypatch):
    """Design §11: nothing existing changes until an expert is switched over. A CLASSIC
    expert must not reach a single line of the option risk manager — not the rails, not the
    breaker, not the sleeve read. The order goes out exactly as it always did."""
    calls = []
    monkeypatch.setattr(ta, "admit_option_entry",
                        lambda **kw: calls.append(kw) or pytest.fail("reached"))
    account = _LiveShapedAccount()
    result = _action(account)._submit_option_order(_spread_legs(), 2, -1.5,
                                                   "bull_put_spread")
    assert result["success"] is True
    assert calls == []
    assert len(account.submitted) == 1


def test_a_NON_ADMITTED_mode_takes_the_LEGACY_path_byte_for_byte(monkeypatch):
    """Review finding H2 (blocker), 2026-09-01. THE REAL POPULATION: an expert whose
    ``risk_manager_mode`` holds the literal string ``"None"`` (see
    ``ExtendableSettingsInterface`` line 87 -- ``str(None)`` was written to the settings
    table). The mode read happened OUTSIDE the guarded path and RAISED, so under
    ``BA2_ERROR_MODE=enforce`` that one row aborted the rest of the Phase-1 entry pass --
    entries that, before this branch existed, read the same value as classic and went out.

    The entry gate engages on EXACTLY ``classic_options``; every other string is the legacy
    path, and this compares the two results field for field rather than asserting a weaker
    "it did not crash". MUTATION KILL: make the mode read raise (or fail closed) and the
    garbage run stops matching the classic run."""
    calls = []
    monkeypatch.setattr(ta, "admit_option_entry",
                        lambda **kw: calls.append(kw) or pytest.fail("reached"))

    def _run(mode: str):
        gen = _with_mode(mode)
        next(gen)
        try:
            account = _LiveShapedAccount()
            result = _action(account)._submit_option_order(_spread_legs(), 2, -1.5,
                                                           "bull_put_spread")
            return result, account.submitted
        finally:
            next(gen, None)

    reference, ref_submitted = _run("classic")
    garbage, garbage_submitted = _run("None")
    assert calls == []                                  # the RM was never consulted
    assert garbage["success"] is True
    assert garbage["data"] == reference["data"]
    assert garbage["message"] == reference["message"]
    assert len(garbage_submitted) == len(ref_submitted) == 1


def test_an_action_with_no_expert_instance_never_reaches_it(monkeypatch):
    """Rulesets fired from the UI test bench carry no instance id."""
    calls = []
    monkeypatch.setattr(ta, "admit_option_entry", lambda **kw: calls.append(kw))
    account = _LiveShapedAccount()
    _action(account, instance_id=None)._submit_option_order(_spread_legs(), 1, -1.0,
                                                            "bull_put_spread")
    assert calls == []
    assert len(account.submitted) == 1


# ---------------------------------------------------------------------------
# the two questions the cover answers, and the one it does not
# ---------------------------------------------------------------------------
def test_the_stock_cover_reaches_the_RAILS_and_not_the_ORDER_ROW(opted_in, monkeypatch):
    """Review finding M3, 2026-09-01, at the seam that decides both.

    ONE measurement function, TWO questions. The rails ask what the whole position
    commits, so a verified covered call is measurable there -- (spot - credit) x 100 --
    and stays admissible. The ORDER STAMP asks ``loss_pct_of_max_loss``'s denominator,
    whose numerator is the option legs' P&L alone, so it must be ABSENT and the stop
    self-disarms. MUTATION KILL: hand the RM the cover-free value and the rails go back to
    declining every covered call as unmeasurable; stamp the cover-inclusive one and an
    incoherent stop re-arms on every CC and every wheel CC leg."""
    seen = []
    monkeypatch.setattr(ta, "admit_option_entry",
                        lambda **kw: seen.append(kw) or rm.admit_option_entry(**kw))
    legs = [OptionLeg(contract_symbol="XYZ260320C105", side=OrderDirection.SELL,
                      position_intent="sell_to_open", option_type=OptionRight.CALL,
                      strike=105.0, expiry=EXPIRY, underlying="XYZ")]
    account = _LiveShapedAccount()
    result = _action(account)._submit_option_order(legs, 1, 3.0, "covered_call",
                                                   stock_cover_price=100.0)
    assert result["success"] is True, result["message"]
    assert "max_loss_per_contract" not in result["data"]
    assert seen and seen[0]["max_loss_per_contract"] == pytest.approx(9700.0)
    assert seen[0]["stock_cover_price"] == 100.0


def test_without_a_cover_the_two_questions_get_the_SAME_answer(opted_in, monkeypatch):
    """The split is not a second measurement path: on every structure but a verified
    covered call the RM is handed exactly what the row was stamped with."""
    seen = []
    monkeypatch.setattr(ta, "admit_option_entry",
                        lambda **kw: seen.append(kw) or rm.admit_option_entry(**kw))
    account = _LiveShapedAccount()
    result = _action(account)._submit_option_order(_spread_legs(), 1, -1.5,
                                                   "bull_put_spread")
    assert result["success"] is True, result["message"]
    assert seen[0]["max_loss_per_contract"] == pytest.approx(
        result["data"]["max_loss_per_contract"])


# ---------------------------------------------------------------------------
# the gate bites at the choke point
# ---------------------------------------------------------------------------
def test_the_rails_refuse_at_the_submit_choke_point(opted_in):
    """A refusal must stop the order reaching the broker AND produce the failed
    ``TradeActionResult`` the UI renders as the reason nothing fired."""
    # 1,000 of equity: the short 100 strike controls 10,000 of notional, which is 10x
    # leverage against a 3x rail — the first rail this candidate breaches.
    account = _LiveShapedAccount(balance=1_000.0)
    result = _action(account)._submit_option_order(_spread_legs(), 1, -1.5,
                                                   "bull_put_spread")
    assert result["success"] is False
    assert OPTION_RAIL_REFUSAL in result["message"]
    assert result["data"]["option_rm_rail"] == "max_notional_leverage"
    assert account.submitted == []                    # nothing reached the broker


def test_an_admitted_entry_still_reaches_the_broker(opted_in):
    account = _LiveShapedAccount()
    result = _action(account)._submit_option_order(_spread_legs(), 1, -1.5,
                                                   "bull_put_spread")
    assert result["success"] is True
    assert len(account.submitted) == 1


def test_a_submitted_structure_is_charged_to_the_sleeve_for_the_next_one(opted_in):
    """The pending charge is registered from the submit seam itself, keyed on the
    transaction the broker call returned — not reconstructed later from order rows."""
    account = _LiveShapedAccount()
    _action(account)._submit_option_order(_spread_legs(), 1, -1.5, "bull_put_spread")
    assert len(rm.pending_charges(7)) == 1


def test_a_PREVIEW_never_consults_the_rails_and_never_journals(opted_in, monkeypatch):
    """``submit_to_broker=False`` is a "manual review" preview: nothing is sent, so nothing
    may be charged to the sleeve and nothing may be written into the entry journal. The gate
    ran AHEAD of this branch until 2026-09-01, so a preview recorded an "admitted" decision
    for a structure the book never took, and consumed headroom against the next real entry.

    MUTATION KILL: move the gate back ahead of the submit_to_broker branch -- the journal
    stops being empty."""
    calls = []
    monkeypatch.setattr(ta, "admit_option_entry",
                        lambda **kw: calls.append(kw) or rm.admit_option_entry(**kw))
    account = _LiveShapedAccount()
    action = _action(account)
    action.submit_to_broker = False
    result = action._submit_option_order(_spread_legs(), 1, -1.5, "bull_put_spread")
    assert result["success"] is True
    assert "not submitted" in result["message"]
    assert calls == []
    assert rm.journal(7) == ()
    assert rm.pending_charges(7) == ()
    assert account.submitted == []


def test_a_refused_entry_is_never_charged(opted_in):
    account = _LiveShapedAccount(balance=1_000.0)
    _action(account)._submit_option_order(_spread_legs(), 1, -1.5, "bull_put_spread")
    assert rm.pending_charges(7) == ()


# ---------------------------------------------------------------------------
# ONE implementation, both runtimes
# ---------------------------------------------------------------------------
def test_live_and_backtest_reach_the_SAME_option_risk_manager(opted_in, monkeypatch):
    """THE decision, as a test. Two account shapes — a broker account and a simulator —
    driven through the one shared action class must land in the SAME function object and
    return the SAME verdict. If someone ever forks an option RM for the backtest, the two
    recorded callees stop being identical and this fails."""
    seen = []
    real = rm.admit_option_entry

    def spy(**kwargs):
        verdict = real(**kwargs)
        seen.append((real, kwargs["underlying"], verdict.allowed, verdict.reason))
        return verdict

    monkeypatch.setattr(ta, "admit_option_entry", spy)

    live = _LiveShapedAccount()
    backtest = _BacktestShapedAccount()
    live_result = _action(live)._submit_option_order(_spread_legs(), 1, -1.5,
                                                     "bull_put_spread")
    rm.reset_state()      # each runtime starts from the same, empty sleeve
    bt_result = _action(backtest)._submit_option_order(_spread_legs(), 1, -1.5,
                                                       "bull_put_spread")

    assert len(seen) == 2
    assert seen[0][0] is seen[1][0] is rm.admit_option_entry
    assert seen[0][1:] == seen[1][1:]                       # same decision, same rail
    assert live_result["success"] == bt_result["success"] is True


#: A CALL to the gate, in any shape production code could reach it.
#:
#: The first version of this guard was ``^\s*(verdict\s*=\s*)?admit_option_entry\(``, which
#: only ever matched the ONE call it was written against: a line-start anchor plus a single
#: hard-coded assignment prefix. ``rm.admit_option_entry(...)`` (the module-qualified form
#: every other file in the tree uses), a call nested in an expression or an argument, one
#: bound to a name and invoked later — every second wiring point anyone would actually write
#: passed the guard silently. Two patterns now: the CALL in any dotted / nested position, and
#: the ALIAS form (a bare reference stashed on a name), which is how a call site hides from a
#: call-shaped regex. ``def admit_option_entry(`` — the definition itself — is excluded.
_GATE_CALL = re.compile(r"(?<!def )\b(?:\w+\s*\.\s*)*admit_option_entry\s*\(")
_GATE_ALIAS = re.compile(r"=\s*(?:\w+\s*\.\s*)*admit_option_entry\b(?!\s*\()")


def test_the_option_risk_manager_has_exactly_one_production_wiring_point():
    """A second call site is a second implementation waiting to happen — a backtest-only
    hook in ``daily_engine`` or a live-only one in ``JobManager`` is exactly how the two
    sides diverged before. Production code may call ``admit_option_entry`` from ONE module.
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    callers = set()
    for path in root.rglob("*.py"):
        parts = path.parts
        if any(p in (".venv", "tests", "tests_scripts", "test_files", "__pycache__")
               for p in parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _GATE_CALL.search(text) or _GATE_ALIAS.search(text):
            callers.add(path.relative_to(root).as_posix())
    assert callers == {"packages/common/ba2_common/core/TradeActions.py"}, callers


def test_the_one_wiring_point_guard_would_actually_catch_a_second_one():
    """The guard is a regex, and a regex that matches nothing passes forever. These are the
    shapes a second wiring point would REALLY be written in — the module-qualified call the
    rest of the tree uses, a nested one, an aliased one — every one of which the original
    line-anchored pattern let through."""
    for smuggled in (
        "    rm.admit_option_entry(expert=e)\n",
        "    v = ba2_common.core.OptionRiskManagement.admit_option_entry(expert=e)\n",
        "    if not admit_option_entry(expert=e).allowed:\n",
        "    results.append(rm.admit_option_entry(expert=e))\n",
        "admit_option_entry(\n    expert=e)\n",
    ):
        assert _GATE_CALL.search(smuggled), smuggled
    assert _GATE_ALIAS.search("    _gate = rm.admit_option_entry\n")
    # ...and it must not flag the definition, or the module that owns it fails its own guard.
    assert not _GATE_CALL.search("def admit_option_entry(*, expert, account):\n")


def test_the_backtest_engine_carries_no_option_risk_manager_of_its_own():
    """``daily_engine`` bypassed the risk manager for options (``if self._entry_is_option``)
    and that is still how it stages an option entry — but the DECISION happens inside the
    shared code. The engine must not grow an implementation beside it.

    CALLING the shared risk manager is not carrying one, and since 2026-09-01 the engine
    does exactly that: one call per bar to ``OptionRiskManagement.update_sleeve_breaker``,
    the SAME transition the live exit pass makes, so the drawdown breaker means the same
    thing in both runtimes. What it must never do is reach past that into the pure
    primitives — ``option_book``'s rails, book totals, candidate model or the ``update_breaker``
    transition itself — because a second call site for those is a second implementation
    waiting to drift. The guard is on CALLS (and on the import), so prose naming the shared
    function it delegates to does not trip it.
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    engine = (root / "testplatform/backend/app/services/backtest/daily_engine.py"
              ).read_text(encoding="utf-8", errors="ignore")
    assert "option_book" not in engine, "the engine imported the pure rail primitives"
    for forbidden in ("check_rails(", "book_totals(", "update_breaker(",
                      "CandidateStructure("):
        assert forbidden not in engine, forbidden
    # ...and the ONE call it is allowed to make is there. Without this half the guard is
    # equally satisfied by an engine that simply dropped the breaker again, which is the
    # state this whole seam exists to leave behind.
    assert "update_sleeve_breaker(expert=" in engine
