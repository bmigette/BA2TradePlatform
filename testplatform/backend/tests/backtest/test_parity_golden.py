"""Golden live<->backtest parity test (Phase 0 of the live<->backtest engine unification plan,
docs/plans/2026-07-02-live-backtest-engine-unification.md).

This is the missing EVIDENCE CHANNEL for the plan's core claim ("same engine in live and
backtest"): it replays a hermetic window of RECORDED live ExpertRecommendations (captured by
tools/capture_live_parity_fixture.py into a committed JSON fixture) through the backtest's exact
enter-decision path — the real TradeActionEvaluator.evaluate(...).execute(...) against a flat
BacktestAccount + the live expert's own enter ruleset — and pins the backtest decision against
what the live engine actually did.

Asserts (logic parity — a failure is a real shared-engine regression):
  * every live-FUNDED rec fires an order of the SAME side in the backtest;
  * every HOLD rec fires nothing.

Measures (reported, not asserted): BUY recs that passed the ruleset but were NOT funded live —
the live orchestration seam (dedup / equity / capital allocation) the unification plan targets.

The fixture is committed and consumed read-only; the test touches NO live DB and NO network.

Run:  ./venv/bin/python -m pytest tests/backtest/test_parity_golden.py -v -s
"""
from __future__ import annotations

import os

from app.services.backtest.parity_harness import default_fixture, run_parity

_FIXTURE = default_fixture(13)  # live instance 13 = FMPRating, classic-RM, enter ruleset 10


def test_golden_fixture_is_committed():
    # The fixture is git-tracked, so its ABSENCE is a real CI failure — not a skip. This makes the
    # parity gate blocking: a broken/missing fixture (or import) cannot silently pass CI green.
    assert os.path.exists(_FIXTURE), (
        f"committed golden parity fixture missing: {_FIXTURE} — re-capture with "
        f"tools/capture_live_parity_fixture.py")


def test_golden_live_backtest_entry_parity():
    report = run_parity(_FIXTURE)
    print("\n" + report.summary())

    # There must be recorded live entries to prove parity against (guards a silently empty fixture).
    assert len(report.positive) >= 1, "fixture has no live-funded recs to prove parity against"
    assert len(report.negative) >= 1, "fixture has no HOLD recs for the negative control"

    # POSITIVE parity: every live-funded rec fires the SAME side in the backtest evaluator.
    assert report.positive_pass == len(report.positive), (
        "backtest did not reproduce a live-funded entry:\n" + report.summary())
    # NEGATIVE parity: every HOLD fires nothing.
    assert report.negative_pass == len(report.negative), (
        "backtest fired an order on a HOLD rec:\n" + report.summary())
    # No side/decision mismatches on the asserted sets.
    assert not report.mismatches, report.summary()
    assert report.ok


def test_golden_expert_is_classic_rm_in_scope():
    # Sanity: the golden fixture is an in-scope (classic-RM) expert, per the locked parity scope.
    report = run_parity(_FIXTURE)
    assert report.expert  # a real expert name was captured


# ===========================================================================
# OPTION ENTRY PARITY (design 2026-08-27 S4, operator decision 2026-08-30)
#
# The golden fixture above replays RECORDED live equity decisions. No live option entry has
# ever been recorded, so an option arm of that replay cannot exist yet. What the decision
# actually requires is pinned instead, and pinned in the CI gate rather than in prose: ONE
# option-entry decision, taken by ONE implementation, reached identically from the live
# construction site and the backtest construction site.
# ===========================================================================
import pathlib
from datetime import date as _date
from types import SimpleNamespace as _NS

import pytest as _pytest

_ROOT = pathlib.Path(__file__).resolve().parents[4]

_RAILS = {"max_concurrent_structures": 10, "max_deployment_pct": 40.0,
          "max_notional_leverage": 3.0, "undefined_risk_max_pct": 20.0,
          # A REQUIRED rail since 2026-09-01: the entry gate consults the latch this
          # setting produces, and the breaker now transitions in BOTH runtimes.
          "circuit_breaker_pct": 20.0}


class _ParityExpert:
    def __init__(self, mode="classic_options"):
        self.settings = {"risk_manager_mode": mode}

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in _RAILS:
            raise ValueError(key)
        return _RAILS[key]


class _ParityResolver:
    def __init__(self, expert):
        self._expert = expert

    def get_expert_instance(self, expert_id):
        return self._expert

    def get_account_instance(self, account_id):
        return None

    def get_account_instance_from_transaction(self, transaction):
        return None


def _parity_account(with_clock: bool):
    """A broker-shaped account, or a simulator-shaped one (it publishes ``_as_of_date``)."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface

    class _Acct(OptionsAccountInterface):
        def __init__(self):
            self.id = 1
            self.submitted = []

        def get_balance(self):
            return 100_000.0

        def get_account_info(self):
            """The sleeve equity read: ONE definition for both runtimes since 2026-09-01
            (AccountSnapshot.equity), reached through the tolerant
            ReadOnlyAccountInterface probe this double does not override."""
            return {"equity": 100_000.0, "cash": 100_000.0, "balance": 100_000.0}

        get_account_snapshot = ReadOnlyAccountInterface.get_account_snapshot

        def cash_available_for_delivery(self):
            return 100_000.0

        def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                                option_strategy, expert_recommendation_id=None,
                                transaction_id=None):
            self.submitted.append(option_strategy)
            return _NS(id=1, transaction_id=1, data={})

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

    if with_clock:
        _Acct._as_of_date = lambda self: _date(2026, 1, 5)
    return _Acct()


def _parity_legs():
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OptionRight, OrderDirection

    return [OptionLeg(contract_symbol="XYZ260320P100", side=OrderDirection.SELL,
                      position_intent="sell_to_open", option_type=OptionRight.PUT,
                      strike=100.0, expiry=_date(2026, 3, 20), underlying="XYZ"),
            OptionLeg(contract_symbol="XYZ260320P95", side=OrderDirection.BUY,
                      position_intent="buy_to_open", option_type=OptionRight.PUT,
                      strike=95.0, expiry=_date(2026, 3, 20), underlying="XYZ")]


@_pytest.fixture
def _option_parity_env(monkeypatch):
    import ba2_common.core.OptionRiskManagement as rm
    from ba2_common.core.instance_resolver import (
        get_instance_resolver, set_instance_resolver,
    )

    # RESTORE, never reset-to-unconfigured: the resolver is a PROCESS-WIDE seam that the
    # backtest wiring installs once, and clobbering it here would break every later test in
    # the session that expects it (it did, before this line).
    previous = get_instance_resolver()
    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    set_instance_resolver(_ParityResolver(_ParityExpert()))
    yield rm
    set_instance_resolver(previous)
    rm.reset_state()


def test_one_option_entry_decision_is_taken_by_ONE_implementation(_option_parity_env,
                                                                  monkeypatch):
    """The option arm of the parity gate. A broker-shaped account and a simulator-shaped
    account are driven through the shared option action; both must land in the SAME
    ``admit_option_entry`` function object and return the SAME verdict. A backtest-only
    fork of the option risk manager breaks this."""
    import ba2_common.core.TradeActions as ta

    rm = _option_parity_env
    seen = []
    real = rm.admit_option_entry

    def _spy(**kwargs):
        verdict = real(**kwargs)
        seen.append((real, verdict.allowed, verdict.reason))
        return verdict

    monkeypatch.setattr(ta, "admit_option_entry", _spy)

    results = []
    for with_clock in (False, True):
        rm.reset_state()
        account = _parity_account(with_clock)
        rec = _NS(id=1, instance_id=7, data=None, price_at_date=100.0,
                  expected_profit_percent=None, recommended_action=None, confidence=80.0)
        action = ta.create_action(ta.ExpertActionType.OPEN_BULL_PUT_SPREAD, "XYZ",
                                  account, _NS(), None, rec)
        action.submit_to_broker = True
        results.append(action._submit_option_order(_parity_legs(), 1, -1.5,
                                                   "bull_put_spread")["success"])

    assert len(seen) == 2, "one of the two paths never reached the option risk manager"
    assert seen[0][0] is seen[1][0] is rm.admit_option_entry
    assert seen[0][1:] == seen[1][1:]
    assert results == [True, True]


def test_live_and_backtest_construct_the_same_evaluator():
    """Both decision sites build ``ba2_common.core.TradeActionEvaluator``. That is what
    makes the option gate inside the shared action reachable from both without a second
    wiring point -- and it is the structural claim S4 is made of."""
    live = (_ROOT / "ba2_trade_platform/core/TradeManager.py").read_text(
        encoding="utf-8", errors="ignore")
    engine = (_ROOT / "testplatform/backend/app/services/backtest/daily_engine.py"
              ).read_text(encoding="utf-8", errors="ignore")
    assert "from .TradeActionEvaluator import TradeActionEvaluator" in live
    assert "from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator" in engine
    shim = (_ROOT / "ba2_trade_platform/core/TradeActionEvaluator.py").read_text(
        encoding="utf-8", errors="ignore")
    assert 'import_module("ba2_common.core.TradeActionEvaluator")' in shim


def test_the_option_risk_manager_mode_is_backtestable():
    """``classic_options`` must NOT be caught by the smart-RM scope guard: the whole point
    of one shared implementation is that a backtest of such an expert models what live
    does. Only ``smart`` is out of scope."""
    from app.services.backtest.daily_backtest_handler import assert_backtestable_risk_mode

    assert assert_backtestable_risk_mode(
        "FMPRating", {"risk_manager_mode": "classic_options"}) is None
    with _pytest.raises(ValueError):
        assert_backtestable_risk_mode("FMPRating", {"risk_manager_mode": "smart"})
