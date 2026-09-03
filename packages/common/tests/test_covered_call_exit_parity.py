"""PARITY: the written call's DTE exit is ONE implementation reached by BOTH runtimes.

The exit moved house on 2026-09-03. It used to live in two places that did different things:
``option_lifecycle.decide`` returned ``LIFECYCLE_ROLL_DTE`` and the LIVE pass closed the
structure, while the backtest had no rule that could close a covered call at all. Now the
``cc_dte`` RULE owns it -- ``covered_call_days_to_expiry <= N`` ->
``close_option(close_target='covered_call')`` -- and ``decide`` no longer emits a closing
reason for a single-expiry structure at its expiry.

WHAT "BOTH RUNTIMES" MEANS HERE, mechanically. The two runtimes differ in exactly one thing
at this level: WHERE the trade rows live. A backtest trial runs with ``trade_store``'s
in-memory dicts active (``TradingOrder``/``Transaction`` are ``IN_MEM_MODELS``); live runs
against SQLite. Everything above that -- the condition, the repository lookup it resolves
through, the action that closes, the evaluator that walks them -- is one code path. So the
parity claim worth pinning is that the SAME book answers the SAME way through BOTH stores,
which is what this file drives; the full walk through each runtime's own caller is pinned
where each caller lives (``testplatform/backend/tests/backtest/test_covered_call_engine.py``
for ``daily_engine``, ``tests/test_option_lifecycle_service.py`` for the live pass).

The third assertion is the one that makes it a parity test rather than two tests: on the
identical book, the LIVE lifecycle pass's decider must now HOLD -- if it still closed, live
would exit twice and the backtest once.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

EXPERT = 77
SYMBOL = "AAPL"
CALL = "AAPL240719C00200000"
SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
SIM_TODAY = date(2024, 6, 15)
#: 5 days of life left: inside ``cc_dte``'s authored default floor of 7.
CALL_EXPIRY = SIM_TODAY + timedelta(days=5)


def _rec():
    return SimpleNamespace(created_at=SIM_AS_OF, instance_id=EXPERT, symbol=SYMBOL)


class _FakeAccount:
    """The REAL ``has_pending_closing_order`` bound in: the guard under test must not be
    faked away, and it reads the same rows on both stores."""
    id = 1

    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
    has_pending_closing_order = ReadOnlyAccountInterface.has_pending_closing_order

    #: The one price the re-anchor measurement needs: what a share is worth right now. A
    #: number, not a stub that refuses — the point of that test is that the wrong subject
    #: produces a confident number rather than an unevaluable.
    def get_instrument_current_price(self, symbol):
        return 18.0


def _working_close(call_txn):
    """A buy-to-close SUBMITTED and not yet filled, on the call's own transaction."""
    add_instance(TradingOrder(
        account_id=1, symbol=CALL, quantity=1.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.NEW, transaction_id=call_txn,
        asset_class=AssetClass.OPTION, contract_symbol=CALL, underlying_symbol=SYMBOL,
        option_type=OptionRight.CALL, strike=200.0, multiplier=100, expiry=CALL_EXPIRY,
        option_strategy="close"), expunge_after_flush=True)


def _build_the_book():
    """An OPENED 100-share lot, plus a covered call written on its OWN transaction.

    That split is the whole difficulty: the manage pass anchors on the stock, so nothing
    reachable from the equity transaction can see this call.
    """
    eq = add_instance(Transaction(
        symbol=SYMBOL, quantity=100.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, asset_class=AssetClass.EQUITY,
        expert_id=EXPERT, multiplier=1))
    stock = TradingOrder(
        account_id=1, symbol=SYMBOL, quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=eq,
        asset_class=AssetClass.EQUITY, open_price=190.0, filled_qty=100.0)
    add_instance(stock, expunge_after_flush=True)

    call_txn = add_instance(Transaction(
        symbol=SYMBOL, quantity=1.0, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, asset_class=AssetClass.OPTION,
        option_strategy="covered_call", expert_id=EXPERT, multiplier=100))
    call = TradingOrder(
        account_id=1, symbol=CALL, quantity=1.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=call_txn,
        asset_class=AssetClass.OPTION, contract_symbol=CALL, underlying_symbol=SYMBOL,
        option_type=OptionRight.CALL, strike=200.0, multiplier=100, expiry=CALL_EXPIRY,
        option_strategy="covered_call", open_price=2.0, filled_qty=1.0)
    add_instance(call, expunge_after_flush=True)
    return stock, call_txn


def _answers(stock):
    """(does the rule fire, which contract would the close take)."""
    from ba2_common.core.TradeActions import CloseOptionAction
    from ba2_common.core.TradeConditions import CoveredCallDaysToExpiryCondition
    from ba2_common.core.types import OrderRecommendation

    cond = CoveredCallDaysToExpiryCondition(
        account=_FakeAccount(), instrument_name=SYMBOL, expert_recommendation=_rec(),
        operator_str="<=", value=7, existing_order=stock)
    fires = cond.evaluate()
    action = CloseOptionAction(
        instrument_name=SYMBOL, account=_FakeAccount(),
        order_recommendation=OrderRecommendation.SELL, existing_order=stock,
        expert_recommendation=_rec(), close_target="covered_call")
    resolved = action._resolve_option_order()
    return fires, cond.get_calculated_value(), (resolved.contract_symbol if resolved else None)


def test_the_backtest_shaped_store_fires_the_rule_and_targets_the_written_call(tmp_path):
    """The BACKTEST side: ``trade_store``'s in-memory dicts, the store a GA trial runs on."""
    with ts.inmem_trades():
        stock, call_txn = _build_the_book()
        assert _answers(stock) == (True, 5, CALL)
        # ...and the guard answers the same way on this store: with a close already
        # working, the rule still fires and the close resolves NOTHING.
        _working_close(call_txn)
        assert _answers(stock) == (True, 5, None)


def test_the_live_shaped_store_answers_IDENTICALLY(tmp_path):
    """The LIVE side: SQLite. Same book, same numbers, same contract.

    A divergence here is the defect class ``trade_repository`` exists for: a raw
    ``select()`` against a RAM-only trial returns EMPTY rather than raising, so the rule
    would read as "no call held" in the backtest and fire in live, with nothing to notice.
    """
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "cc_parity.sqlite"))
    db.init_db()
    stock, call_txn = _build_the_book()
    assert _answers(stock) == (True, 5, CALL)
    _working_close(call_txn)
    assert _answers(stock) == (True, 5, None)


def test_the_LIVE_LIFECYCLE_PASS_no_longer_closes_the_same_structure(tmp_path):
    """THE PARITY CLAIM. One owner, so the decider must now decline.

    Before 2026-09-03 this same covered call at 5 DTE returned ``LIFECYCLE_ROLL_DTE`` -- a
    CLOSING reason the live pass acted on -- while no ruleset could express the same exit.
    With the rule owning it, a decider that still closed would make live exit twice.
    """
    from ba2_common.core.option_lifecycle import (
        LIFECYCLE_CLOSING_REASONS, LIFECYCLE_HOLD, LifecycleLeg, OptionStructure, decide)
    from ba2_common.core.option_types import OptionContract

    structure = OptionStructure(
        transaction_id=1, underlying=SYMBOL, strategy="covered_call",
        legs=[LifecycleLeg(CALL, net_qty=-1.0, strike=200.0, option_type=OptionRight.CALL,
                           expiry=CALL_EXPIRY, underlying=SYMBOL)],
        quantity=1, multiplier=100, entry_net_premium=-2.0, expiry=CALL_EXPIRY)
    chain = {CALL: OptionContract(
        symbol=CALL, underlying=SYMBOL, option_type=OptionRight.CALL, strike=200.0,
        expiry=CALL_EXPIRY, bid=1.40, ask=1.50, last=1.45, delta=0.20)}
    settings = {"profit_capture_pct": 50.0, "strangle_capture_pct": 25.0,
                "tested_delta_enabled": False, "tested_delta": 0.30, "roll_dte": 21,
                "dr_stop_enabled": False, "dr_stop_credit_mult": 2.0,
                "ur_stop_enabled": False, "ur_stop_credit_mult": 2.0}

    decision = decide([structure], chain, settings, SIM_TODAY)[0]

    assert decision.reason == LIFECYCLE_HOLD
    assert decision.should_close is False
    assert "5 DTE" in decision.detail        # measured, and reported, just not acted on
    assert "roll_dte" not in {r for r in LIFECYCLE_CLOSING_REASONS}


# ---------------------------------------------------------------------------
# M7 — the wheel's put-phase rules must not re-anchor onto the assigned stock,
# and the answer must be the same on both stores.
# ---------------------------------------------------------------------------
def _assigned_stock_book():
    """The STOCK phase: an assigned lot, no put left, no call written yet.

    ``meta_data["origin"]`` is what makes it ASSIGNED rather than bought — the fact
    ``HasAssignedSharesCondition`` reads and ``wheel_stock_guard`` gates on.
    """
    from ba2_common.core.types import TXN_ORIGIN_CSP_ASSIGNMENT

    eq = add_instance(Transaction(
        symbol=SYMBOL, quantity=100.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, asset_class=AssetClass.EQUITY,
        expert_id=EXPERT, multiplier=1, open_price=20.0,   # the assigned cost basis
        meta_data={"origin": TXN_ORIGIN_CSP_ASSIGNMENT}))
    stock = TradingOrder(
        account_id=1, symbol=SYMBOL, quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=eq,
        asset_class=AssetClass.EQUITY, open_price=20.0, filled_qty=100.0)
    add_instance(stock, expunge_after_flush=True)
    return stock


def _guard_and_reanchor(stock):
    """(does the stock guard hold, what a put-phase P&L rule WOULD read off the stock).

    The second half is the damage the guard prevents, measured rather than asserted: with
    ``opt_tp``'s subject swapped to the stock, ``profit_loss_percent`` returns a NUMBER — a
    share position's P&L compared against a threshold authored for a short put's credit.
    """
    from ba2_common.core.TradeConditions import (
        HasAssignedSharesCondition, ProfitLossPercentCondition)

    guard = HasAssignedSharesCondition(
        account=_FakeAccount(), instrument_name=SYMBOL, expert_recommendation=_rec(),
        existing_order=stock)
    reanchored = ProfitLossPercentCondition(
        account=_FakeAccount(), instrument_name=SYMBOL, expert_recommendation=_rec(),
        operator_str=">", value=50.0, existing_order=stock)
    reanchored.evaluate()
    return guard.evaluate(), reanchored.get_calculated_value()


def test_the_stock_guard_holds_on_the_BACKTEST_shaped_store(tmp_path):
    with ts.inmem_trades():
        stock = _assigned_stock_book()
        holds, _reanchored = _guard_and_reanchor(stock)
        assert holds is True, (
            "has_assigned_shares is false on an assigned lot, so wheel_stock_guard would "
            "never halt and every put-phase rule would re-anchor onto the stock")


def test_the_stock_guard_holds_IDENTICALLY_on_the_LIVE_shaped_store(tmp_path):
    """The defect class ``trade_repository`` exists for: a raw select() against a RAM-only
    trial returns EMPTY rather than raising, so a guard read that way would hold in live and
    silently not in the backtest — the worst possible split for this particular rule."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "wheel_guard_parity.sqlite"))
    db.init_db()
    stock = _assigned_stock_book()
    holds, _reanchored = _guard_and_reanchor(stock)
    assert holds is True


def test_a_put_phase_rule_WOULD_read_the_stock_which_is_why_the_guard_exists(tmp_path):
    """The measurement that makes the guard non-decorative.

    ``profit_loss_percent`` does not refuse an equity anchor — ``_get_pnl_for_condition``
    falls through to equity pricing — so a put-phase rule reached in the stock phase gets a
    real number and compares it against a short put's credit band. Unevaluable would have
    been survivable; a confident wrong number is not.
    """
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "wheel_reanchor.sqlite"))
    db.init_db()
    stock = _assigned_stock_book()
    _holds, reanchored = _guard_and_reanchor(stock)
    assert reanchored is not None, (
        "profit_loss_percent refused the equity anchor after all — if that is now true the "
        "guard's justification has changed and this design should be revisited")
