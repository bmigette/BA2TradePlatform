"""The drawdown breaker measures the TRUE drawdown; the sizing rails keep the CAPPED one.

THE DEFECT (review finding, 2026-09-01)
---------------------------------------
``OptionRiskManagement.sleeve_equity`` reads ``get_account_snapshot().equity``, which on
``BacktestAccount`` resolves to ``deployed_equity() = min(equity_cap, cash + mark-to-market)``.
That clamp is correct and deliberate for a SIZER -- the cap is what may be spent -- but it is
ONE-SIDED: it compresses peaks and never troughs. A drawdown measured through it is therefore
not a drawdown. An account capped at 50k that falls 100k -> 64k, a true -36%, reports 50k on
both bars, a 0.0% drawdown and no stand-down, while the identical path live (where no cap
exists) stands the sleeve down at -20%. The rail whose entire job is to stop a loss was the
one rail the backtest could not measure.

``equity_cap.py`` is the codebase's own authority on this shape: it warns that feeding the
capped figure into scoring "would report zero P&L for every period spent above the cap", and
ships ``capped_drawdown_curve`` rather than differencing the capped series.

THE RULING, AND WHAT THIS FILE PINS
-----------------------------------
Two questions, two readers, one shared breaker:

  * *how many dollars may I deploy?*   -> ``sleeve_equity``      (CAPPED; the sizing rails)
  * *how much have I lost from peak?*  -> ``sleeve_true_equity`` (UNCAPPED; the breaker)

The runtime difference lives in the ACCOUNT's answer -- ``ReadOnlyAccountInterface.true_equity``
returns the same snapshot field for every real broker, and ``BacktestAccount`` overrides it
with its uncapped ``equity()`` -- never in forked breaker logic. Live, where there is no cap,
both readers return one number and nothing changes.

THE FIXTURE is the reviewer's arithmetic, driven through a REAL ``BacktestAccount`` over a
REAL ``AsOfPriceSource``: a 20k account under a 20k cap buys 200 shares at 100, the price runs
to 150 (a true 30k peak) and then falls. The equity the account computes from its own ledger:

    eval   1      2      3      4      5      6      7
    true   20k    25k    30k    23k    20k    18k    16k
    capped 20k    20k    20k    20k    20k    18k    16k

A 20% breaker on the TRUE series trips at eval 4 (-23.3% off the 30k peak). On the CAPPED
series the peak can never exceed 20k, so it trips only at eval 7 -- three evaluations and
7k of real losses later, with entries admitted the whole way down.

MUTATION KILLS (executed, not asserted):
  * point ``update_sleeve_breaker`` back at ``sleeve_equity``  ->
    ``test_the_breaker_trips_at_the_TRUE_drawdown_on_a_capped_account`` fails (trips at 7);
  * point the sizing rails at ``sleeve_true_equity``           ->
    ``test_the_sizing_rails_still_read_the_CAPPED_equity`` fails (the entry is admitted).

Run from the backend dir (with the worktree on PYTHONPATH):
    python -m pytest tests/backtest/test_option_breaker_sees_past_the_capped_equity.py -q
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.interfaces.ExtendableSettingsInterface import (
    ExtendableSettingsInterface,
)
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.option_book import BreakerState, update_breaker
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

BREAKER_PCT = 20.0
CAP = 20_000.0
SHARES = 200

#: (date, open, high, low, close). Bar 0 carries the clock when the market BUY is placed; it
#: fills at bar 1's open (the ``next_bar_open`` model), so bar 1 is evaluation 1.
BARS = [
    (datetime(2024, 1, 2), 100, 101, 99, 100),
    (datetime(2024, 1, 3), 100, 101, 99, 100),     # fill @100 -> equity 20,000
    (datetime(2024, 1, 4), 110, 126, 109, 125),    # 25,000
    (datetime(2024, 1, 5), 140, 151, 139, 150),    # 30,000  <- the TRUE peak
    (datetime(2024, 1, 8), 120, 121, 114, 115),    # 23,000  <- -23.3%: the TRUE breaker trips
    (datetime(2024, 1, 9), 105, 106, 99, 100),     # 20,000
    (datetime(2024, 1, 10), 95, 96, 89, 90),       # 18,000
    (datetime(2024, 1, 11), 85, 86, 79, 80),       # 16,000  <- where a CAPPED breaker trips
]

#: A complete sleeve configuration. Every one is REQUIRED and none has a default.
SLEEVE_SETTINGS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 50.0,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
    "circuit_breaker_pct": BREAKER_PCT,
}

CFG: Dict[str, Any] = {
    "starting_cash": CAP,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "equity_cap": CAP,
}


class _SleeveExpert:
    """The sleeve's settings read through the REAL accessor, borrowed rather than imitated.

    ``get_setting_with_interface_default`` over ``MarketExpertInterface``'s own declarations
    (the style commit 50ea80cc established), so an undeclared rail behaves here exactly as it
    does in production instead of being modelled by a double that raises where the real
    accessor returns.
    """

    get_setting_with_interface_default = (
        ExtendableSettingsInterface.get_setting_with_interface_default)
    get_merged_settings_definitions = MarketExpertInterface.get_merged_settings_definitions

    def __init__(self, instance_id: int, settings: Dict[str, Any]):
        self.id = instance_id
        self.settings = dict(settings)


def _bar_rows(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


def _account(cfg: Dict[str, Any], account_id: int):
    """A wired ``BacktestAccount`` over a fresh run DB and hand-built bars.

    The same ``_acct`` shape ``test_backtest_account_fills`` established. Returns
    ``(account, db_context, price_source)``; the caller MUST close the context.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition,
    )
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    wire_backtest_seams()
    ctx = backtest_trading_db(f"breaker-cap-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BARS))
    account = BacktestAccount(account_id, ps, cfg)
    wire_backtest_seams().register_account(account_id, account)
    return account, ctx, ps


def _buy_and_hold(account, ps) -> None:
    """Deploy the whole account into 200 shares at 100, through the REAL order path.

    ``submit_order`` + ``refresh_orders`` is the account's own fill engine, so the position,
    the cash and the marks are the simulator's, not the test's.
    """
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    ps.set_clock(BARS[0][0])
    order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=SHARES,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.NEW, comment="breaker-cap fixture")
    account.submit_order(order)
    # The clock stays on bar 0: ``next_bar_open`` fills at the open of the bar AFTER the
    # clock, so the fill price is bar 1's open (100) and the ledger is exactly 200 x 100.
    account.refresh_orders()
    filled = account.get_order(order.broker_order_id)
    assert filled.status == OrderStatus.FILLED, filled.status
    assert filled.filled_qty == SHARES, filled.filled_qty


def _walk(account, ps, expert, expert_id: int) -> List[Dict[str, Any]]:
    """One per-bar evaluation over the post-fill bars, recording what each reader saw.

    ``update_sleeve_breaker`` is the SHARED transition -- the same function
    ``daily_engine._update_option_breakers`` and ``run_option_lifecycle_pass`` call.
    """
    out: List[Dict[str, Any]] = []
    for bar in BARS[1:]:
        ps.set_clock(bar[0])
        out.append({
            "true": rm.sleeve_true_equity(account, expert_id),
            "capped": rm.sleeve_equity(account, expert_id),
            "state": rm.update_sleeve_breaker(expert=expert, account=account,
                                              expert_instance_id=expert_id),
        })
    return out


def _first_halted(walk: List[Dict[str, Any]]) -> Optional[int]:
    """1-based index of the evaluation on which the sleeve stood down, or ``None``."""
    for i, row in enumerate(walk, start=1):
        if row["state"].halted:
            return i
    return None


def _capped_control(walk: List[Dict[str, Any]]) -> Optional[int]:
    """What the SAME shared transition would have done on the capped series.

    The control is executed, not asserted: the identical ``option_book.update_breaker``, fed
    the numbers ``sleeve_equity`` actually returned on each of those bars. So "the capped
    reader trips three evaluations later" is a measurement of this run, not a claim about it.
    """
    state = BreakerState()
    for i, row in enumerate(walk, start=1):
        state = update_breaker(state, row["capped"],
                               {"circuit_breaker_pct": BREAKER_PCT})
        if state.halted:
            return i
    return None


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    """The latch is process state keyed by sleeve; a leak would decide a later test."""
    rm.reset_state()
    yield
    rm.reset_state()


# =========================================================================== #
# (a) the breaker measures the TRUE drawdown
# =========================================================================== #
def test_the_breaker_trips_at_the_TRUE_drawdown_on_a_capped_account():
    """THE regression: the cap must not be able to hide a loss from the breaker.

    MUTATION KILL: change ``update_sleeve_breaker`` back to ``sleeve_equity`` and the
    stand-down moves from evaluation 4 to evaluation 7 -- three bars and 7,000 dollars of
    real losses later, with every option entry admitted in between.
    """
    account, ctx, ps = _account(CFG, account_id=9301)
    try:
        _buy_and_hold(account, ps)
        expert = _SleeveExpert(9301, {"risk_manager_mode": "classic_options",
                                      **SLEEVE_SETTINGS})
        walk = _walk(account, ps, expert, 9301)

        # The fixture really is capped, and the cap really does hide the peak.
        assert [row["true"] for row in walk] == [20_000.0, 25_000.0, 30_000.0, 23_000.0,
                                                 20_000.0, 18_000.0, 16_000.0]
        assert [row["capped"] for row in walk] == [20_000.0, 20_000.0, 20_000.0, 20_000.0,
                                                   20_000.0, 18_000.0, 16_000.0]

        assert _first_halted(walk) == 4
        assert walk[3]["state"].tripped is True
        assert walk[2]["state"].peak_equity == 30_000.0      # the peak the cap concealed
        # ...and the control: the capped reader would have stood down three evaluations late.
        assert _capped_control(walk) == 7
    finally:
        ctx.__exit__(None, None, None)


def test_the_capped_run_stands_down_exactly_where_the_UNCAPPED_run_does():
    """Same price path, cap removed: the breaker must not be able to tell the difference.

    This is the property the ruling is actually about -- a backtest's breaker means what
    live's breaker means -- and it is stronger than the index assertion above, which a
    fixture rewrite could satisfy by accident.
    """
    capped, ctx_c, ps_c = _account(CFG, account_id=9302)
    try:
        _buy_and_hold(capped, ps_c)
        walk_capped = _walk(capped, ps_c, _SleeveExpert(
            9302, {"risk_manager_mode": "classic_options", **SLEEVE_SETTINGS}), 9302)
    finally:
        ctx_c.__exit__(None, None, None)

    uncapped_cfg = {k: v for k, v in CFG.items() if k != "equity_cap"}
    uncapped, ctx_u, ps_u = _account(uncapped_cfg, account_id=9303)
    try:
        _buy_and_hold(uncapped, ps_u)
        walk_uncapped = _walk(uncapped, ps_u, _SleeveExpert(
            9303, {"risk_manager_mode": "classic_options", **SLEEVE_SETTINGS}), 9303)
    finally:
        ctx_u.__exit__(None, None, None)

    # The two accounts are genuinely different accounts: the cap really binds on this path.
    assert [r["capped"] for r in walk_capped] != [r["capped"] for r in walk_uncapped]
    # ...and the breakers still transition identically, bar for bar.
    shape = lambda walk: [(r["state"].peak_equity, r["state"].halted, r["state"].tripped)
                          for r in walk]
    assert shape(walk_capped) == shape(walk_uncapped)
    assert _first_halted(walk_capped) == 4


# =========================================================================== #
# (c) the sizing rails are NOT moved onto the uncapped figure
# =========================================================================== #
def _long_call_legs(strike: float = 150.0) -> List[OptionLeg]:
    """A long call: a debit structure, so no short-side notional and no assignment cash.

    Deliberately the simplest candidate that engages ``max_deployment_pct`` and nothing else
    -- the rail under test is the DENOMINATOR, and a candidate that also tripped the leverage
    or assignment rails would pass this test for the wrong reason.
    """
    return [OptionLeg(contract_symbol=f"AAPL260116C{int(strike)}",
                      side=OrderDirection.BUY, ratio_qty=1,
                      strike=strike, option_type=OptionRight.CALL,
                      expiry=datetime(2026, 1, 16).date(), underlying="AAPL")]


def test_the_sizing_rails_still_read_the_CAPPED_equity():
    """A sizer must respect the cap. Only the LOSS measurement looks past it.

    On the 30k-peak bar the account may deploy 20,000 (the cap), not 30,000. A candidate
    risking 13,500 is 67.5% of the capped equity and 45% of the true equity, against a
    ``max_deployment_pct`` of 50: it must be REFUSED. Admitting it would mean the backtest
    deployed capital the capped account does not have.

    MUTATION KILL: point ``admit_option_entry``'s ``equity =`` at ``sleeve_true_equity`` and
    this entry is admitted.
    """
    from ba2_common.core.option_book import RAIL_MAX_DEPLOYMENT

    account, ctx, ps = _account(CFG, account_id=9304)
    try:
        _buy_and_hold(account, ps)
        ps.set_clock(BARS[3][0])                       # the 30,000 / capped-20,000 bar
        assert rm.sleeve_true_equity(account, 9304) == 30_000.0
        assert rm.sleeve_equity(account, 9304) == 20_000.0

        expert = _SleeveExpert(9304, {"risk_manager_mode": "classic_options",
                                      **SLEEVE_SETTINGS})
        verdict = rm.admit_option_entry(
            expert=expert, account=account, expert_instance_id=9304, underlying="AAPL",
            option_strategy="long_call", legs=_long_call_legs(), quantity=1,
            max_loss_per_contract=13_500.0)

        assert verdict.allowed is False
        assert verdict.reason == RAIL_MAX_DEPLOYMENT
        # 50% of the CAPPED 20,000 -- the number the refusal must have divided by.
        assert "20000.00" in verdict.detail or "20,000.00" in verdict.detail, verdict.detail
    finally:
        ctx.__exit__(None, None, None)


def test_the_same_candidate_fits_when_the_cap_is_lifted():
    """The control on the refusal above: 13,500 is 45% of the true 30,000, so with no cap the
    identical entry is ADMITTED. Without this, the refusal could be a rail failing for any
    reason at all and the test would still be green."""
    uncapped_cfg = {k: v for k, v in CFG.items() if k != "equity_cap"}
    account, ctx, ps = _account(uncapped_cfg, account_id=9305)
    try:
        _buy_and_hold(account, ps)
        ps.set_clock(BARS[3][0])
        assert rm.sleeve_equity(account, 9305) == 30_000.0

        expert = _SleeveExpert(9305, {"risk_manager_mode": "classic_options",
                                      **SLEEVE_SETTINGS})
        verdict = rm.admit_option_entry(
            expert=expert, account=account, expert_instance_id=9305, underlying="AAPL",
            option_strategy="long_call", legs=_long_call_legs(), quantity=1,
            max_loss_per_contract=13_500.0)

        assert verdict.allowed is True, verdict.detail
    finally:
        ctx.__exit__(None, None, None)


def test_the_backtest_account_answers_the_two_equity_questions_differently():
    """The seam itself, as a number: ``true_equity`` is uncapped, the snapshot is capped.

    ``BacktestAccount`` is the ONLY implementation that overrides ``true_equity``, because it
    is the only one that simulates a cap. This is what makes the breaker's uncapped reading a
    property of the account rather than a branch in shared code.
    """
    account, ctx, ps = _account(CFG, account_id=9306)
    try:
        _buy_and_hold(account, ps)
        ps.set_clock(BARS[3][0])
        assert account.equity() == 30_000.0
        assert account.true_equity() == 30_000.0
        assert account.deployed_equity() == 20_000.0
        assert account.get_account_snapshot().equity == 20_000.0
    finally:
        ctx.__exit__(None, None, None)
