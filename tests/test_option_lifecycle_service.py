"""The LIVE option lifecycle pass: ``core/option_lifecycle_service.py`` + its JobManager wiring.

This is the task that finally wires ``option_lifecycle`` (Task 6) and ``option_book``
(Task 7) into the live path. Until it landed, both were dead code: ~220 tests and nothing
outside their own test files called either of them.

What the pass is for, and what these tests pin:

* **It runs BEFORE the analyses and invokes no expert.** Roll at 21 DTE, capture at 50% of
  credit, defend a tested short, trip a drawdown breaker — none of those needs an opinion
  about the underlying. Paying for an FMP call plus an LLM analysis to discover a spread is
  at 21 DTE is exactly the cost behind the "options as fast as stocks" requirement.
* **Every close is guarded by ``has_pending_closing_order``.** The pass is scheduled and may
  overlap a manual action; without the guard it re-submits a close on every cycle the first
  one takes to fill (the 2026-07-21 options-grid equity runaway).
* **A broker that cannot answer stops the pass for that account.** ``get_positions()``
  returning ``None`` is *fetch failed*, not *flat* — the conflation that has caused five
  incidents here.
* **``LIFECYCLE_UNKNOWN`` is never swallowed.** "We cannot measure this" and "this is fine"
  are different facts; collapsing them hid a dead roll-DTE gene for a whole GA campaign.

Time is frozen to 2026-05-14 and never to the wall clock — a mutation once survived on this
project because the frozen date happened to equal today.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import pytest

import ba2_trade_platform.core.option_lifecycle_service as svc
from ba2_trade_platform.core.models import MarketAnalysis, TradingOrder
from ba2_trade_platform.core.option_types import OptionContract
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_common.core.option_lifecycle import (
    LIFECYCLE_BREAKER, LIFECYCLE_CREDIT_STOP, LIFECYCLE_HOLD, LIFECYCLE_PROFIT_CAPTURE,
    LIFECYCLE_ROLL_DTE, LIFECYCLE_TESTED, LIFECYCLE_UNKNOWN,
)

from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance, create_trading_order,
    create_transaction,
)


# --------------------------------------------------------------------------- clock
#: Frozen. Deliberately NOT today: a mutation once survived here because the two matched.
FROZEN_NOW = datetime(2026, 5, 14, 15, 0, tzinfo=timezone.utc)
#: 64 DTE from FROZEN_NOW — comfortably outside a roll_dte of 21.
EXPIRY_FAR = date(2026, 7, 17)
#: 15 DTE from FROZEN_NOW — inside a roll_dte of 21.
EXPIRY_NEAR = date(2026, 5, 29)


# ------------------------------------------------------------------------ settings
#: A complete, ordinary short-premium configuration. Individual tests override one key.
BASE_SETTINGS = {
    "profit_capture_pct": 50.0,
    "strangle_capture_pct": 25.0,
    "tested_delta_enabled": False,
    "tested_delta": 0.30,
    "roll_dte": 21,
    "dr_stop_enabled": False,
    "dr_stop_credit_mult": 2.0,
    "ur_stop_enabled": True,
    "ur_stop_credit_mult": 2.0,
    "max_deployment_pct": 40.0,
    "undefined_risk_max_pct": 20.0,
    "max_notional_leverage": 3.0,
    "max_concurrent_structures": 10,
    "circuit_breaker_pct": 20.0,
}


def occ(underlying: str, expiry: date, right: str, strike: float) -> str:
    return f"{underlying}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


# --------------------------------------------------------------------------- doubles
class FakeExpert:
    """Just enough ``ExtendableSettingsInterface`` for the pass to read its thresholds.

    A key the expert does not declare raises ``ValueError``, exactly as
    ``get_setting_with_interface_default`` does — that is the shape the service has to
    survive when an expert has no option settings at all.
    """

    def __init__(self, settings: Dict, expert_id: int = 1):
        self._settings = dict(settings)
        self.id = expert_id
        self.run_analysis_calls = 0

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in self._settings:
            raise ValueError(f"Setting {key!r} not found in FakeExpert interface definitions")
        return self._settings[key]

    def run_analysis(self, *a, **k):          # must NEVER be called by the pass
        self.run_analysis_calls += 1
        raise AssertionError("the lifecycle pass invoked an expert analysis")


class FakeAccount(MockAccount):
    """A broker double built on the real ``OptionsAccountInterface`` machinery.

    ``submit_option_order`` and ``has_pending_closing_order`` are the REAL inherited
    implementations, so a close genuinely writes ``TradingOrder`` rows and the pending-close
    guard genuinely reads them back. Only the wire call (``_submit_option_order_impl``) and
    the market data are faked — faking above that seam is what let the assignment gap ship.
    """

    def __init__(self, account_id: int):
        super().__init__(account_id)
        self.chain: Dict[str, OptionContract] = {}
        self.chain_calls: List = []
        self.chain_error: Optional[Exception] = None
        self.positions_result: object = []       # [] = flat, None = FETCH FAILED
        self.positions_error: Optional[Exception] = None
        self.balance_error: Optional[Exception] = None
        self.submitted: List = []
        self.submit_error_on_txn: Optional[int] = None    # the BROKER call fails
        self.submit_raises_on_txn: Optional[int] = None   # submit_option_order itself throws
        self.submit_returns_none = False

    def submit_option_order(self, *args, **kwargs):
        if (self.submit_raises_on_txn is not None
                and kwargs.get("transaction_id") == self.submit_raises_on_txn):
            raise RuntimeError("the broker connection dropped mid-submit")
        return super().submit_option_order(*args, **kwargs)

    # -- book visibility ---------------------------------------------------
    def get_positions(self):
        if self.positions_error is not None:
            raise self.positions_error
        return self.positions_result

    def get_balance(self):
        if self.balance_error is not None:
            raise self.balance_error
        return self._balance

    def get_account_info(self):
        """The read the sleeve's equity actually comes down since 2026-09-01.

        ``sleeve_equity`` reads ``get_account_snapshot().equity`` — one definition for both
        runtimes — and ``MockAccount`` does not override the snapshot, so the tolerant
        ``ReadOnlyAccountInterface`` probe lands HERE. ``balance_error`` therefore has to
        fail this call to model "the balance endpoint is down"; failing ``get_balance``
        alone would leave the breaker reading a perfectly healthy equity and the test
        asserting nothing.
        """
        if self.balance_error is not None:
            raise self.balance_error
        return super().get_account_info()

    # -- market data -------------------------------------------------------
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        self.chain_calls.append((underlying, expiry_min, expiry_max))
        if self.chain_error is not None:
            raise self.chain_error
        return [c for c in self.chain.values()
                if c.underlying == underlying and expiry_min <= c.expiry <= expiry_max]

    # -- order submission --------------------------------------------------
    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        from ba2_trade_platform.core.db import update_instance
        if self.submit_error_on_txn is not None and trading_order.transaction_id == self.submit_error_on_txn:
            raise RuntimeError("broker rejected the close")
        if self.submit_returns_none:
            return None
        # Left WORKING (not FILLED) on purpose: a submitted-but-unfilled close is exactly
        # the state has_pending_closing_order exists to see.
        trading_order.status = OrderStatus.PENDING
        trading_order.broker_order_id = f"fake-{trading_order.id}"
        update_instance(trading_order)
        for i, lo in enumerate(leg_orders or []):
            lo.status = OrderStatus.PENDING
            lo.broker_order_id = f"fake-{trading_order.id}-{i}"
            update_instance(lo)
        self.submitted.append(trading_order)
        return trading_order


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` text emitted by the service module.

    NOT caplog: ``ba2_trade_platform/logger.py`` sets ``propagate = False`` and
    ``tests/test_penny_gainers_fix.py`` swaps the logger module for a MagicMock under a full
    collection, so caplog's root handler sees nothing. Patching the module-under-test's own
    ``logger`` is immune to both.
    """
    import sys
    module = sys.modules[svc.__name__]
    messages: List[str] = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _capture_rm_errors(monkeypatch):
    """Collect ``logger.error`` text emitted by the SHARED option risk manager.

    The sleeve's equity read moved there (one reader, both runtimes), so the error a failed
    balance endpoint produces is now logged by ``ba2_common``, not by this service. Same
    reasoning as ``_capture_errors`` for why this is not caplog.
    """
    import ba2_common.core.OptionRiskManagement as _rm

    messages: List[str] = []
    monkeypatch.setattr(_rm.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _capture_warnings(monkeypatch):
    import sys
    module = sys.modules[svc.__name__]
    messages: List[str] = []
    monkeypatch.setattr(module.logger, "warning", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


# ------------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def _clean_breaker_state():
    """The breaker peak is process state; a leaked peak would silently trip a later test."""
    svc.reset_breaker_states()
    yield
    svc.reset_breaker_states()


@pytest.fixture
def sleeve():
    """An account + an option expert instance, both persisted."""
    acct_def = create_account_definition()
    expert_row = create_expert_instance(account_id=acct_def.id)
    account = FakeAccount(acct_def.id)
    expert = FakeExpert(BASE_SETTINGS, expert_id=expert_row.id)
    return account, expert, expert_row


@pytest.fixture
def wired(monkeypatch, sleeve):
    """Point the service's instance factories at the doubles."""
    account, expert, expert_row = sleeve
    monkeypatch.setattr(svc, "_resolve_expert", lambda eid: expert)
    monkeypatch.setattr(svc, "_resolve_account", lambda aid: account)
    return account, expert, expert_row


# ------------------------------------------------------------------------ builders
def _contract(underlying, expiry, right, strike, *, bid, ask, last=None, delta=None):
    return OptionContract(
        symbol=occ(underlying, expiry, "C" if right is OptionRight.CALL else "P", strike),
        underlying=underlying, option_type=right, strike=strike, expiry=expiry,
        bid=bid, ask=ask, last=last, delta=delta)


def open_credit_spread(account, expert_row, *, underlying="ACN", expiry=EXPIRY_FAR,
                       short_strike=100.0, long_strike=95.0, credit=2.00, qty=1,
                       short_fill=3.00, long_fill=1.00, strategy="put_credit_spread",
                       right=OptionRight.PUT, txn_expiry=..., leg_expiry=...):
    """One OPENED short put vertical: parent + two filled legs + the intent transaction."""
    txn_expiry = expiry if txn_expiry is ... else txn_expiry
    leg_expiry = expiry if leg_expiry is ... else leg_expiry
    letter = "C" if right is OptionRight.CALL else "P"
    txn = create_transaction(
        symbol=underlying, quantity=qty, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=-abs(credit),
        expert_id=expert_row.id, multiplier=100, asset_class=AssetClass.OPTION,
        option_strategy=strategy, expiry=txn_expiry)
    parent = create_trading_order(
        account.id, symbol=underlying, quantity=qty, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, multiplier=100, option_strategy=strategy,
        underlying_symbol=underlying, expiry=txn_expiry, limit_price=-abs(credit),
        open_price=-abs(credit), filled_qty=qty)
    short = create_trading_order(
        account.id, symbol=occ(underlying, expiry, letter, short_strike), quantity=qty,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, transaction_id=txn.id, asset_class=AssetClass.OPTION,
        multiplier=100, contract_symbol=occ(underlying, expiry, letter, short_strike),
        option_type=right, strike=short_strike, expiry=leg_expiry,
        underlying_symbol=underlying, parent_order_id=parent.id,
        open_price=short_fill, filled_qty=qty)
    long = create_trading_order(
        account.id, symbol=occ(underlying, expiry, letter, long_strike), quantity=qty,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.FILLED, transaction_id=txn.id, asset_class=AssetClass.OPTION,
        multiplier=100, contract_symbol=occ(underlying, expiry, letter, long_strike),
        option_type=right, strike=long_strike, expiry=leg_expiry,
        underlying_symbol=underlying, parent_order_id=parent.id,
        open_price=long_fill, filled_qty=qty)
    return txn, parent, short, long


def quote_spread(account, *, underlying="ACN", expiry=EXPIRY_FAR, short_strike=100.0,
                 long_strike=95.0, short_px=2.00, long_px=0.60, right=OptionRight.PUT,
                 short_delta=0.20, long_delta=0.10):
    """Publish a chain for both legs. ``*_px`` is the mid; the pass exits at ask/bid.

    The DEFAULTS price a healthy structure: against the builders' 2.00 entry credit they
    cost 1.50 to flatten, i.e. +25% — under ``profit_capture_pct`` 50 and nowhere near the
    stop. A test that wants an exit says so explicitly, so no test can pass by accident.
    """
    for strike, px, dlt in ((short_strike, short_px, short_delta),
                            (long_strike, long_px, long_delta)):
        c = _contract(underlying, expiry, right, strike,
                      bid=round(px - 0.05, 4), ask=round(px + 0.05, 4), last=px, delta=dlt)
        account.chain[c.symbol] = c


def open_long_call(account, expert_row, *, underlying="ACN", expiry=EXPIRY_FAR,
                   strike=100.0, debit=3.00, qty=1):
    """One OPENED long call: the DEBIT arm, whose whole exposure is the premium paid."""
    contract = occ(underlying, expiry, "C", strike)
    txn = create_transaction(
        symbol=underlying, quantity=qty, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=debit, expert_id=expert_row.id,
        multiplier=100, asset_class=AssetClass.OPTION, option_strategy="long_call",
        expiry=expiry)
    create_trading_order(
        account.id, symbol=contract, quantity=qty, side=OrderDirection.BUY,
        order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, multiplier=100, contract_symbol=contract,
        option_type=OptionRight.CALL, strike=strike, expiry=expiry,
        underlying_symbol=underlying, open_price=debit, filled_qty=qty)
    return txn


def run(expert_row, **kw):
    return svc.run_option_lifecycle_pass(expert_row.id, as_of=FROZEN_NOW, **kw)


def close_orders(txn_id) -> List[TradingOrder]:
    from ba2_common.core.trade_store import orders_where
    return [o for o in orders_where(transaction_id=txn_id)
            if o.option_strategy == "close" and o.parent_order_id is None]


# ===========================================================================
# The four prescribed tests
# ===========================================================================
def test_the_lifecycle_pass_runs_before_any_analysis_is_submitted(monkeypatch, sleeve):
    """Order matters: manage first, then let the expert opine on what remains.

    An analysis submitted first would be an LLM opinion about a spread the pass is about to
    flatten — money spent to be overruled, and a rule then evaluating an entry thesis against
    a position that no longer exists.
    """
    account, expert, expert_row = sleeve
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)

    from ba2_trade_platform.core.JobManager import JobManager
    calls: List[str] = []
    monkeypatch.setattr(svc, "run_option_lifecycle_pass",
                        lambda *a, **k: calls.append("lifecycle"))
    monkeypatch.setattr(JobManager, "submit_market_analysis",
                        lambda self, **k: calls.append(f"analysis:{k['symbol']}"))
    monkeypatch.setattr(JobManager, "__init__", lambda self: None)

    jm = JobManager()
    jm._execute_open_positions_analysis(expert_row.id, "open_positions")

    assert calls, "the job did nothing at all"
    assert calls[0] == "lifecycle", f"the pass must run first, got {calls}"
    assert "analysis:ACN" in calls, "the expert analyses must still run afterwards"


def test_the_lifecycle_pass_submits_no_market_analysis(monkeypatch, wired):
    """The whole point. Maintenance is calendar and state driven — no expert, no LLM."""
    from ba2_trade_platform.core.JobManager import JobManager
    from unittest.mock import MagicMock

    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)

    submit_market_analysis = MagicMock()
    monkeypatch.setattr(JobManager, "submit_market_analysis", submit_market_analysis)

    result = run(expert_row)

    assert submit_market_analysis.call_count == 0
    assert expert.run_analysis_calls == 0
    from ba2_common.core.db import get_all_instances
    assert get_all_instances(MarketAnalysis) == [], "the pass wrote a MarketAnalysis row"
    # ...and it still did its job.
    assert [d.reason for d in result.decisions] == [LIFECYCLE_ROLL_DTE]
    assert len(result.submitted) == 1


def test_a_close_is_guarded_against_a_pending_close(wired):
    """The pass is scheduled and may overlap a manual action.

    Without the guard the pass submits ANOTHER close on every cycle the first one takes to
    fill, each crediting cash for contracts that may already be gone — the 2026-07-21
    options-grid trillion-scale equity runaway.
    """
    account, expert, expert_row = wired
    txn, parent, _, _ = open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    # A close submitted on an earlier cycle, still working.
    create_trading_order(
        account.id, symbol="ACN", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.PENDING, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, option_strategy="close", multiplier=100)

    result = run(expert_row)

    assert account.submitted == [], "a second close was submitted over a working one"
    assert result.submitted == []
    assert txn.id in result.skipped_pending_close
    # The decision itself is still made and still visible — we skipped the ACTION, not the
    # measurement. A silent skip would hide a close that never happens.
    assert [d.reason for d in result.decisions] == [LIFECYCLE_ROLL_DTE]


def test_a_broker_that_cannot_answer_stops_the_pass_for_that_account(monkeypatch, wired):
    """Never act against a book you cannot see.

    ``get_positions()`` returning None is 'fetch failed', not 'flat'. That conflation has
    force-closed real positions and duplicated others on this project five times.
    """
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    account.positions_result = None                    # FETCH FAILED, not flat
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert account.submitted == [], "the pass acted against an unverified book"
    assert result.aborted is True
    assert result.decisions == []
    assert any("position" in m.lower() for m in errors), errors


# ===========================================================================
# Step 5 — idempotence
# ===========================================================================
def test_running_the_pass_twice_with_no_state_change_submits_nothing_the_second_time(wired):
    """It runs on a schedule. Two runs over one unchanged book close one position, once."""
    account, expert, expert_row = wired
    txn, *_ = open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)

    first = run(expert_row)
    assert len(first.submitted) == 1
    assert len(close_orders(txn.id)) == 1

    second = run(expert_row)
    assert second.submitted == [], "the second run re-submitted the same close"
    assert txn.id in second.skipped_pending_close
    assert len(close_orders(txn.id)) == 1, "a duplicate closing order reached the ledger"


# ===========================================================================
# Unknown is never a hold
# ===========================================================================
def test_a_structure_with_no_chain_row_is_unknown_and_is_reported_not_held(monkeypatch, wired):
    """LIFECYCLE_UNKNOWN means the decision could not be made. It is NOT a hold.

    Folding it into hold is what hid the dead roll-DTE gene for an entire GA campaign.
    """
    account, expert, expert_row = wired
    txn, *_ = open_credit_spread(account, expert_row, expiry=EXPIRY_FAR)  # no quotes
    warnings = _capture_warnings(monkeypatch)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_UNKNOWN]
    assert len(result.unknown) == 1, "an unmeasurable structure was swallowed"
    assert result.unknown[0].pnl_pct is None, "unmeasurable P&L was coerced to a number"
    # The DECISION itself must be reported, naming the position — not merely the missing
    # chain row that caused it. An operator needs to know which structure went unmanaged.
    assert any(f"transaction {txn.id} is UNKNOWN" in m for m in warnings), warnings
    assert account.submitted == [], "an unmeasurable structure must not be traded on"


def test_an_unknown_and_a_hold_are_different_facts(wired):
    """Same book, one measurable and one not. The reasons must differ."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, underlying="ACN")
    open_credit_spread(account, expert_row, underlying="MSFT")
    quote_spread(account, underlying="ACN")              # MSFT deliberately unquoted

    result = run(expert_row)

    reasons = {d.reason for d in result.decisions}
    assert reasons == {LIFECYCLE_HOLD, LIFECYCLE_UNKNOWN}
    assert len(result.unknown) == 1


# ===========================================================================
# Building the structures: netting, expiry, the premium basis
# ===========================================================================
def test_the_percent_basis_is_the_transactions_signed_net_premium(wired):
    """A 2.00 credit now costing 1.00 to close is +50% — the profit-capture trigger."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, credit=2.00)
    # Flatten cost = buy back the short at its ask (1.05) - sell the long at its bid (0.05)
    # = 1.00 per share, i.e. half the credit banked.
    quote_spread(account, short_px=1.00, long_px=0.10)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_PROFIT_CAPTURE]
    assert result.decisions[0].pnl_pct == pytest.approx(50.0)
    assert len(result.submitted) == 1


def test_a_leg_already_bought_back_is_netted_out_and_not_reversed_again(wired):
    """Netting is per contract over the executed orders. A flat leg must not be re-opened."""
    account, expert, expert_row = wired
    txn, parent, short, long = open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    # The short leg was bought back individually mid-life: it nets to zero.
    create_trading_order(
        account.id, symbol=short.contract_symbol, quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol=short.contract_symbol, option_type=OptionRight.PUT, strike=100.0,
        expiry=EXPIRY_NEAR, underlying_symbol="ACN", open_price=0.50, filled_qty=1)
    quote_spread(account, expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert len(result.submitted) == 1
    legs = result.submitted[0].legs
    assert [l.contract_symbol for l in legs] == [long.contract_symbol]
    assert legs[0].side == OrderDirection.SELL, "the surviving long is SOLD to close"
    assert legs[0].position_intent == "sell_to_close"
    # The buy-back is REALISED cash, not zero: 2.00 credit - 0.50 paid + 0.55 to sell the
    # remaining long = 2.05 on a 2.00 basis.
    assert result.decisions[0].pnl_pct == pytest.approx(102.5)
    # And the long-only remnant of a CREDIT structure owes no premium outlay — it was
    # never paid for. Reading its entry premium as a debit would charge the sleeve 200.
    assert result.book.committed == pytest.approx(0.0)


def test_a_two_lot_structure_is_priced_and_closed_at_its_real_size(wired):
    """Quantity is the percent basis AND the closing size. Pinning it to 1 flatters both."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, qty=2, credit=2.00)
    quote_spread(account, short_px=1.00, long_px=0.10)

    result = run(expert_row)

    assert result.decisions[0].pnl_pct == pytest.approx(50.0), \
        "the 2-lot basis was not 2 x credit x multiplier"
    assert {l.ratio_qty for l in result.submitted[0].legs} == {2}
    # submit_option_order sizes each child as quantity x ratio_qty, so the parent must
    # carry 1: anything else multiplies the netted leg sizes a second time.
    from ba2_common.core.trade_store import orders_where
    children = [o for o in orders_where(parent_order_id=result.submitted[0].order.id)]
    assert {o.quantity for o in children} == {2}, \
        f"the close was sized {sorted(o.quantity for o in children)}, not 2 contracts"


def test_the_close_is_a_market_order_tagged_close_on_the_same_transaction(wired):
    """Three properties of the closing order, each load-bearing.

    MARKET: a resting limit that does not fill leaves the pending-close guard blocking this
    structure from being managed at all until the next pass, and the pass is daily.
    ``option_strategy="close"``: it is in ``NON_INTENT_STRATEGIES``, so it does not relabel
    the transaction's opening intent, and it is what every holdings filter excludes.
    ``transaction_id``: without it ``has_pending_closing_order`` can never see the close,
    and the pass re-submits one every cycle forever.
    """
    account, expert, expert_row = wired
    txn, *_ = open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)

    order = run(expert_row).submitted[0].order

    assert order.order_type == OrderType.MARKET
    assert order.option_strategy == "close"
    assert order.transaction_id == txn.id


def test_an_executed_fill_with_no_price_makes_the_pnl_unknown_not_optimistic(wired):
    """A fill we cannot price makes the realised cash unknowable — not 0.0.

    Counting it as zero cash produces a confident, wrong percentage: here a -125% that
    reads as a healthy hold, on a structure nobody can actually value.
    """
    account, expert, expert_row = wired
    _, _, short, _ = open_credit_spread(account, expert_row)
    quote_spread(account)
    from ba2_common.core.db import update_instance
    short.open_price = None
    update_instance(short)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_UNKNOWN]
    assert result.decisions[0].pnl_pct is None
    assert account.submitted == []


def test_an_opened_option_transaction_with_no_executed_legs_is_reported(monkeypatch, wired):
    """A position the ledger cannot describe is a fact, not a silence."""
    account, expert, expert_row = wired
    txn = create_transaction(
        symbol="ACN", quantity=1, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=-2.0, expert_id=expert_row.id,
        multiplier=100, asset_class=AssetClass.OPTION,
        option_strategy="put_credit_spread", expiry=EXPIRY_NEAR)
    warnings = _capture_warnings(monkeypatch)

    result = run(expert_row)

    assert result.unbuildable == [txn.id]
    assert result.decisions == []
    assert any(str(txn.id) in m for m in warnings), warnings


def test_a_fractional_contract_count_refuses_the_whole_close(monkeypatch, wired):
    """Truncating half a contract to zero would submit a PARTIAL flatten and call it done."""
    account, expert, expert_row = wired
    txn, _, short, _ = open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    from ba2_common.core.db import update_instance
    short.filled_qty = 0.5
    update_instance(short)
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert account.submitted == [], "a partial flatten was submitted"
    assert result.failed == [txn.id]
    assert any("whole number of contracts" in m for m in errors), errors


def test_the_structure_expiry_comes_from_the_transaction_when_the_legs_carry_none(wired):
    """The parent expiry is the fix for the dead roll gene; legs may predate it."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR, leg_expiry=None)
    quote_spread(account, expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_ROLL_DTE]
    assert "15 DTE" in result.decisions[0].detail
    # ...and the chain was still located for the legs, so the exit is priced. A close
    # outranks an unmeasurable input, so an unpriced roll would look identical here.
    assert result.decisions[0].pnl_pct is not None


def test_the_structure_expiry_comes_from_the_legs_when_the_transaction_carries_none(wired):
    """And the other way round, for rows written before the intent column existed."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR, txn_expiry=None)
    quote_spread(account, expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_ROLL_DTE]
    # The chain window came off the LEG, so the exit is priced. Asking only the parent
    # (NULL here) would find no chain at all and the roll would fire unpriced.
    assert result.decisions[0].pnl_pct is not None


def test_conflicting_expiries_are_unknown_never_a_guess(wired):
    """A parent saying one date and a leg saying another is a contradiction, not a max()."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR, txn_expiry=EXPIRY_FAR)
    quote_spread(account, expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_UNKNOWN]
    assert "conflicting expiries" in result.decisions[0].detail
    assert account.submitted == []


def test_a_tested_short_is_measured_on_the_short_leg_only(wired):
    """A deep long wing's delta would otherwise close every healthy spread."""
    account, expert, expert_row = wired
    expert._settings["tested_delta_enabled"] = True
    open_credit_spread(account, expert_row)
    quote_spread(account, short_delta=0.42, long_delta=0.90)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_TESTED]
    assert occ("ACN", EXPIRY_FAR, "P", 100.0) in result.decisions[0].detail


def test_an_equity_transaction_is_not_an_option_structure(wired):
    """The pass manages options. An equity position is somebody else's job."""
    account, expert, expert_row = wired
    create_transaction(symbol="AAPL", quantity=100, side=OrderDirection.BUY,
                       status=TransactionStatus.OPENED, open_price=150.0,
                       expert_id=expert_row.id)

    result = run(expert_row)

    assert result.decisions == []
    assert result.unbuildable == [], "an equity position was pulled into the option book"
    assert account.submitted == []


def test_an_entry_that_has_not_finished_filling_is_not_managed(monkeypatch, wired):
    """WAITING is an entry still working, not a position.

    Managing one would net its part-filled legs and submit an offsetting close — cancelling
    an entry before it ever opens (the reason the backtest engine's ``_held_transactions``
    is OPENED-only too), and reporting the untouched ones as unmanageable every pass.
    """
    account, expert, expert_row = wired
    txn = create_transaction(
        symbol="ACN", quantity=2, side=OrderDirection.SELL,
        status=TransactionStatus.WAITING, open_price=-2.0, expert_id=expert_row.id,
        multiplier=100, asset_class=AssetClass.OPTION,
        option_strategy="put_credit_spread", expiry=EXPIRY_NEAR)
    create_trading_order(
        account.id, symbol=occ("ACN", EXPIRY_NEAR, "P", 100.0), quantity=2,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.PARTIALLY_FILLED, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol=occ("ACN", EXPIRY_NEAR, "P", 100.0), option_type=OptionRight.PUT,
        strike=100.0, expiry=EXPIRY_NEAR, underlying_symbol="ACN",
        open_price=3.0, filled_qty=1)
    quote_spread(account, expiry=EXPIRY_NEAR)
    warnings = _capture_warnings(monkeypatch)

    result = run(expert_row)

    assert result.decisions == []
    assert result.unbuildable == []
    assert account.submitted == [], "a half-filled entry was closed out from under itself"
    assert warnings == []


def test_the_pass_never_acts_on_another_experts_positions(wired):
    """Rails are per-expert sleeves. Managing a neighbour's book is not maintenance."""
    account, expert, expert_row = wired
    other = create_expert_instance(account_id=expert_row.account_id)
    other_txn, *_ = open_credit_spread(account, other, underlying="MSFT",
                                       expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="MSFT", expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert result.decisions == []
    assert account.submitted == []
    assert close_orders(other_txn.id) == []


def test_the_close_targets_the_deciding_transaction(wired):
    """Two structures, one exit. The close must carry the right transaction and contracts."""
    account, expert, expert_row = wired
    healthy, *_ = open_credit_spread(account, expert_row, underlying="ACN")
    rolling, *_ = open_credit_spread(account, expert_row, underlying="MSFT",
                                     expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="ACN")
    quote_spread(account, underlying="MSFT", expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert len(result.submitted) == 1
    order = result.submitted[0].order
    assert order.transaction_id == rolling.id
    assert close_orders(healthy.id) == [], "the healthy structure was closed"
    assert {l.contract_symbol for l in result.submitted[0].legs} == {
        occ("MSFT", EXPIRY_NEAR, "P", 100.0), occ("MSFT", EXPIRY_NEAR, "P", 95.0)}


def test_a_hold_submits_nothing(wired):
    """A healthy spread is left alone — and says so, with its measured P&L."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row)
    quote_spread(account, short_px=2.00, long_px=0.60)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_HOLD]
    assert result.submitted == []
    assert result.unknown == []


def test_the_credit_stop_fires_on_an_undefined_risk_strategy(wired):
    """ur_stop is on by default; a short put at -2x credit must be stopped out."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, strategy="short_strangle", credit=2.00)
    # Flatten cost 6.00 per share against a 2.00 credit = -200%.
    quote_spread(account, short_px=6.00, long_px=0.05, long_delta=0.02)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_CREDIT_STOP]
    assert len(result.submitted) == 1


# ===========================================================================
# The circuit breaker: fed from option_book, never re-implemented here
# ===========================================================================
def test_the_breaker_signal_reaches_the_lifecycle_as_a_flatten(wired):
    """option_book owns the STATE; option_lifecycle owns the flatten. Task 8 connects them."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, underlying="ACN")
    open_credit_spread(account, expert_row, underlying="MSFT")
    quote_spread(account, underlying="ACN")
    quote_spread(account, underlying="MSFT")

    account._balance = 100_000.0
    run(expert_row)                                # ratchets the peak
    account._balance = 79_000.0                    # -21%: past circuit_breaker_pct 20
    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_BREAKER, LIFECYCLE_BREAKER]
    assert len(result.submitted) == 2
    assert result.breaker.tripped is True
    assert result.breaker.halted is True


def test_the_peak_ratchets_even_while_the_sleeve_is_flat(wired):
    """``manage_open`` returned before ratcheting on an empty book, so a just-flattened
    sleeve stopped tracking its peak and would re-arm against a stale one."""
    account, expert, expert_row = wired
    account._balance = 100_000.0
    flat = run(expert_row)                         # no holdings at all
    assert flat.decisions == []
    assert flat.breaker.peak_equity == 100_000.0

    open_credit_spread(account, expert_row)
    quote_spread(account)
    account._balance = 75_000.0
    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_BREAKER]


def test_a_stand_down_suppresses_entries_not_exits(wired):
    """THE semantic decision. ``manage_open`` returned [] while halted, so a structure the
    flatten failed to close was never managed again — no capture, no stop, no roll, and the
    breaker (which signals the EDGE, not the latch) could never re-fire on it.

    Here the stand-down gates ENTRY (``check_rails`` declines while ``halted``) and the exit
    rules keep running every pass.
    """
    account, expert, expert_row = wired
    txn, *_ = open_credit_spread(account, expert_row)
    quote_spread(account)                                    # healthy: a hold
    account._balance = 100_000.0
    run(expert_row)

    # A manual close is working when the breaker trips, so the flatten is (correctly)
    # withheld — the structure survives the one bar the breaker signals on.
    manual = create_trading_order(
        account.id, symbol="ACN", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.PENDING, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, option_strategy="close", multiplier=100)
    account._balance = 79_000.0
    tripped = run(expert_row)
    assert tripped.breaker.tripped is True
    assert tripped.breaker.halted is True
    assert tripped.submitted == [], "the guard should have withheld the flatten"
    assert txn.id in tripped.skipped_pending_close

    # The manual close is then canceled: the structure is open, unflattened, and the
    # breaker's EDGE has passed. Next pass it is at +50%.
    from ba2_common.core.db import update_instance
    manual.status = OrderStatus.CANCELED
    update_instance(manual)
    quote_spread(account, short_px=1.00, long_px=0.10)
    after = run(expert_row)

    assert after.breaker.halted is True, "the sleeve should still be standing down"
    assert after.breaker.tripped is False, "the breaker signals the edge, not the latch"
    assert [d.reason for d in after.decisions] == [LIFECYCLE_PROFIT_CAPTURE], \
        "a stand-down suppressed an EXIT — the structure would never be managed again"
    assert len(after.submitted) == 1


def test_an_unreadable_balance_leaves_the_breaker_blind_and_trips_nothing(wired):
    """A balance we could not read is not a 100% drawdown.

    The peak is already 100k when the read fails, so substituting 0.0 would measure a
    complete wipeout and flatten the whole book on a broker hiccup.
    """
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row)
    quote_spread(account)
    account._balance = 100_000.0
    run(expert_row)                                  # peak = 100k
    account._balance = None

    result = run(expert_row)

    assert result.breaker.blind is True
    assert result.breaker.tripped is False
    assert [d.reason for d in result.decisions] == [LIFECYCLE_HOLD]
    assert account.submitted == []


def test_an_equity_read_that_raises_leaves_the_breaker_blind_too(monkeypatch, wired):
    """An exception reading the equity is unknown equity, not zero equity.

    Substituting 0.0 against a ratcheted peak is a measured 100% drawdown, and the breaker
    would flatten the entire sleeve on a broker hiccup.

    (Named for the BALANCE call until 2026-09-01, when the sleeve's equity became
    ``get_account_snapshot().equity`` — one definition for live and backtest. The fact under
    test is unchanged; the endpoint that fails is the one the breaker now reads.)
    """
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row)
    quote_spread(account)
    account._balance = 100_000.0
    run(expert_row)                                  # peak = 100k
    account.balance_error = RuntimeError("balance endpoint 503")
    errors = _capture_rm_errors(monkeypatch)

    result = run(expert_row)

    assert result.breaker.blind is True
    assert result.breaker.tripped is False
    assert [d.reason for d in result.decisions] == [LIFECYCLE_HOLD]
    assert account.submitted == []
    assert errors


# ===========================================================================
# The book
# ===========================================================================
def test_the_book_totals_are_computed_from_the_same_structures(wired):
    """One 5-wide put vertical commits 500 and is defined risk."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, short_strike=100.0, long_strike=95.0)
    quote_spread(account, short_px=2.00, long_px=0.60)

    result = run(expert_row)

    assert result.book.structure_count == 1
    assert result.book.underlyings == frozenset({"ACN"})
    assert result.book.committed == pytest.approx(500.0)
    assert result.book.naked_committed == pytest.approx(0.0)


def test_a_structure_we_cannot_total_makes_the_book_unmeasurable_not_smaller(monkeypatch,
                                                                             wired):
    """A sum with a missing addend is an unknown sum — and it must be said out loud.

    An unmeasurable sleeve makes every entry rail decline (``check_rails``), which looks
    exactly like "no candidates passed" unless the pass reports why.
    """
    account, expert, expert_row = wired
    _, _, short, _ = open_credit_spread(account, expert_row)
    quote_spread(account)
    from ba2_common.core.db import update_instance
    short.strike = None
    update_instance(short)
    warnings = _capture_warnings(monkeypatch)

    result = run(expert_row)

    assert result.book.committed is None
    assert result.book.unmeasurable, "the book did not name what it could not measure"
    assert any("UNMEASURABLE" in m for m in warnings), warnings


def test_a_long_only_debit_structure_commits_the_premium_it_paid(wired):
    """The debit arm. ``_txn_metrics`` bucketed by order side and returned zero for a
    structure with no SELL leg, so a pure-debit book deployed nothing and the rails never
    engaged. Its exposure is premium x contracts x MULTIPLIER — dropping the multiplier
    understates it a hundredfold.
    """
    account, expert, expert_row = wired
    open_long_call(account, expert_row, debit=3.00, qty=2)
    c = _contract("ACN", EXPIRY_FAR, OptionRight.CALL, 100.0, bid=3.0, ask=3.2, delta=0.5)
    account.chain[c.symbol] = c

    result = run(expert_row)

    assert result.book.committed == pytest.approx(600.0)
    assert result.book.premium_outlay == pytest.approx(600.0)
    assert result.book.notional == pytest.approx(0.0), "a long option is not short notional"
    assert [d.reason for d in result.decisions] == [LIFECYCLE_HOLD]


def test_the_underlying_is_the_transactions_ticker_not_a_legs_blank_field(wired):
    """``Transaction.symbol`` IS the underlying. A leg row that never recorded one must not
    turn the sleeve's one-per-underlying rail into a phantom empty bucket."""
    account, expert, expert_row = wired
    txn, _, short, long = open_credit_spread(account, expert_row)
    quote_spread(account)
    from ba2_common.core.db import update_instance
    for leg in (short, long):
        leg.underlying_symbol = None
        update_instance(leg)

    result = run(expert_row)

    assert result.book.underlyings == frozenset({"ACN"})
    # ...and the chain was still located, because the structure knows its own underlying.
    assert [d.reason for d in result.decisions] == [LIFECYCLE_HOLD]
    assert result.decisions[0].pnl_pct is not None


def test_a_structure_whose_legs_have_all_netted_flat_submits_nothing(monkeypatch, wired):
    """Its transaction is still OPENED until the netting resolves it, so it still occupies
    a slot — but there is nothing left to offset, and "nothing to close" is not a close."""
    account, expert, expert_row = wired
    txn, _, short, long = open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    for leg, side, intent in ((short, OrderDirection.BUY, "buy_to_close"),
                              (long, OrderDirection.SELL, "sell_to_close")):
        create_trading_order(
            account.id, symbol=leg.contract_symbol, quantity=1, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, multiplier=100,
            contract_symbol=leg.contract_symbol, option_type=leg.option_type,
            strike=leg.strike, expiry=EXPIRY_NEAR, underlying_symbol="ACN",
            position_intent=intent, open_price=0.50, filled_qty=1)
    quote_spread(account, expiry=EXPIRY_NEAR)

    result = run(expert_row)

    assert [d.reason for d in result.decisions] == [LIFECYCLE_ROLL_DTE]
    assert result.submitted == [], "a close was recorded for a structure with no legs left"
    assert close_orders(txn.id) == []
    assert result.book.structure_count == 1, "it still occupies its slot"


def test_a_working_close_does_not_net_the_position_flat(wired):
    """Only EXECUTED fills net. A submitted-but-unfilled close has moved no contracts.

    Counting its legs would make the structure read as flat while the broker still holds
    every one of them — and then, if that close is canceled, nothing would ever close it.
    """
    account, expert, expert_row = wired
    txn, *_ = open_credit_spread(account, expert_row)
    quote_spread(account)
    first = run(expert_row)                      # healthy: nothing submitted yet
    assert first.book.committed == pytest.approx(500.0)

    # A close is now working: parent + two PENDING legs on the same transaction.
    parent = create_trading_order(
        account.id, symbol="ACN", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.PENDING, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, option_strategy="close", multiplier=100)
    for strike, side in ((100.0, OrderDirection.BUY), (95.0, OrderDirection.SELL)):
        create_trading_order(
            account.id, symbol=occ("ACN", EXPIRY_FAR, "P", strike), quantity=1, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.PENDING, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, multiplier=100,
            contract_symbol=occ("ACN", EXPIRY_FAR, "P", strike),
            option_type=OptionRight.PUT, strike=strike, expiry=EXPIRY_FAR,
            underlying_symbol="ACN", parent_order_id=parent.id)

    second = run(expert_row)

    assert second.book.committed == pytest.approx(500.0), \
        "an unfilled close made the sleeve look flat"
    assert [d.reason for d in second.decisions] == [LIFECYCLE_HOLD]


# ===========================================================================
# Isolation: one failure must not take the pass, or the job, down
# ===========================================================================
def test_a_broker_rejection_on_one_structure_does_not_stop_the_others(monkeypatch, wired):
    """One account's outage must not silence an entire book — nor one structure's."""
    account, expert, expert_row = wired
    bad, *_ = open_credit_spread(account, expert_row, underlying="ACN",
                                 expiry=EXPIRY_NEAR)
    good, *_ = open_credit_spread(account, expert_row, underlying="MSFT",
                                  expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="ACN", expiry=EXPIRY_NEAR)     # both roll at 15 DTE
    quote_spread(account, underlying="MSFT", expiry=EXPIRY_NEAR)
    account.submit_error_on_txn = bad.id
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert [s.transaction_id for s in result.submitted] == [good.id]
    assert bad.id in result.failed
    assert any(str(bad.id) in m for m in errors), errors


def test_a_chain_outage_makes_a_structure_unknown_and_leaves_the_rest_managed(monkeypatch,
                                                                              wired):
    """A market-data failure is per-symbol blindness, not a reason to stop the sleeve.

    The book-wide stop is ``get_positions()``: that is a statement about the LEDGER. A chain
    we could not fetch makes one structure unmeasurable, which ``option_lifecycle`` already
    reports as UNKNOWN by name.
    """
    account, expert, expert_row = wired
    blind, *_ = open_credit_spread(account, expert_row, underlying="ACN")
    rolling, *_ = open_credit_spread(account, expert_row, underlying="MSFT",
                                     expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="MSFT", expiry=EXPIRY_NEAR)
    real_chain = account.get_option_chain

    def flaky(underlying, expiry_min, expiry_max, **kw):
        if underlying == "ACN":
            raise RuntimeError("chain endpoint 503")
        return real_chain(underlying, expiry_min, expiry_max, **kw)

    monkeypatch.setattr(account, "get_option_chain", flaky)
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is False
    assert [d.transaction_id for d in result.unknown] == [blind.id]
    assert [s.transaction_id for s in result.submitted] == [rolling.id]
    assert any("ACN" in m for m in errors), errors


def test_an_exception_closing_one_structure_does_not_abort_the_others(monkeypatch, wired):
    """A thrown exception, not a refused order: the loop must survive both."""
    account, expert, expert_row = wired
    bad, *_ = open_credit_spread(account, expert_row, underlying="ACN", expiry=EXPIRY_NEAR)
    good, *_ = open_credit_spread(account, expert_row, underlying="MSFT", expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="ACN", expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="MSFT", expiry=EXPIRY_NEAR)
    account.submit_raises_on_txn = bad.id
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert [s.transaction_id for s in result.submitted] == [good.id]
    assert bad.id in result.failed
    assert any(str(bad.id) in m for m in errors), errors


def test_a_failing_pass_does_not_stop_the_analyses(monkeypatch, sleeve):
    """The pass is maintenance; the analyses are the job. Neither may take the other down."""
    account, expert, expert_row = sleeve
    open_credit_spread(account, expert_row)

    from ba2_trade_platform.core.JobManager import JobManager
    submitted: List[str] = []

    def boom(*a, **k):
        raise RuntimeError("the lifecycle pass exploded")

    monkeypatch.setattr(svc, "run_option_lifecycle_pass", boom)
    monkeypatch.setattr(JobManager, "submit_market_analysis",
                        lambda self, **k: submitted.append(k["symbol"]))
    monkeypatch.setattr(JobManager, "__init__", lambda self: None)

    JobManager()._execute_open_positions_analysis(expert_row.id, "open_positions")

    assert submitted == ["ACN"]


def test_the_pass_still_runs_when_the_sleeve_has_no_open_symbols(monkeypatch, sleeve):
    """The peak ratchet has to happen on a flat sleeve too, so the pass must sit ABOVE the
    'no open positions' early return."""
    account, expert, expert_row = sleeve

    from ba2_trade_platform.core.JobManager import JobManager
    calls: List[str] = []
    monkeypatch.setattr(svc, "run_option_lifecycle_pass",
                        lambda *a, **k: calls.append("lifecycle"))
    monkeypatch.setattr(JobManager, "__init__", lambda self: None)

    JobManager()._execute_open_positions_analysis(expert_row.id, "open_positions")

    assert calls == ["lifecycle"]


def test_a_broker_whose_position_fetch_raises_also_stops_the_pass(monkeypatch, wired):
    """An exception is no more 'flat' than a None is."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    account.positions_error = RuntimeError("broker 503")
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is True
    assert account.submitted == []
    assert errors


# ===========================================================================
# Configuration
# ===========================================================================
def test_an_expert_with_no_option_thresholds_declines_the_pass_loudly(monkeypatch, wired):
    """A missing risk threshold is a configuration error, never a substituted default."""
    account, expert, expert_row = wired
    del expert._settings["roll_dte"]
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is True
    assert account.submitted == []
    assert any("roll_dte" in m for m in errors), errors


def test_a_threshold_declared_with_no_value_counts_as_missing(monkeypatch, wired):
    """``get_setting_with_interface_default`` returns the DECLARED DEFAULT, which is
    ``None`` for a setting defined without one. "Declared but unset" is not a value, and
    passing it through would hand ``float(None)`` to a risk rule.

    Both keys are checked: ``profit_capture_pct`` is read by ``decide`` (which would raise
    a catchable ``KeyError``), but ``circuit_breaker_pct`` is read by ``update_breaker``
    BEFORE any decision is taken, so only the up-front check stands between an unset
    threshold and an unexplained crash.
    """
    account, expert, expert_row = wired
    expert._settings["profit_capture_pct"] = None
    expert._settings["circuit_breaker_pct"] = None
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is True
    assert account.submitted == []
    assert any("profit_capture_pct" in m and "circuit_breaker_pct" in m
               for m in errors), errors


def test_a_missing_breaker_threshold_declines_the_pass_rather_than_crashing(monkeypatch, wired):
    """``circuit_breaker_pct`` is read by ``update_breaker``, not by ``decide``.

    Without an up-front check for it the pass does not decline — it raises out of
    ``update_breaker`` before any decision is taken, which the job then logs as an
    unexplained failure.
    """
    account, expert, expert_row = wired
    del expert._settings["circuit_breaker_pct"]
    open_credit_spread(account, expert_row, expiry=EXPIRY_NEAR)
    quote_spread(account, expiry=EXPIRY_NEAR)
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is True
    assert account.submitted == []
    assert any("circuit_breaker_pct" in m for m in errors), errors


def test_a_threshold_only_one_rule_needs_is_reported_not_raised(monkeypatch, wired):
    """``tested_delta`` is only read when the tested-delta rule is enabled.

    So it is not required up front — but a sleeve that turns the rule ON without it must
    decline loudly, naming the threshold, rather than raising out of ``decide``.
    """
    account, expert, expert_row = wired
    expert._settings["tested_delta_enabled"] = True
    del expert._settings["tested_delta"]
    open_credit_spread(account, expert_row)
    quote_spread(account)
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is True
    assert account.submitted == []
    assert any("tested_delta" in m for m in errors), errors


def test_an_expert_with_no_options_and_no_thresholds_is_silent(monkeypatch, wired):
    """A report that always warns trains the user to ignore it."""
    account, expert, expert_row = wired
    expert._settings = {}
    errors = _capture_errors(monkeypatch)

    result = run(expert_row)

    assert result.aborted is False
    assert result.decisions == []
    assert errors == []


def test_an_account_without_option_support_is_skipped(monkeypatch, sleeve):
    """IBKR/TastyTrade have no option API here — say nothing, do nothing, break nothing."""
    account, expert, expert_row = sleeve

    class EquityOnly:
        id = account.id

    monkeypatch.setattr(svc, "_resolve_expert", lambda eid: expert)
    monkeypatch.setattr(svc, "_resolve_account", lambda aid: EquityOnly())

    result = run(expert_row)

    assert result.aborted is False
    assert result.decisions == []
