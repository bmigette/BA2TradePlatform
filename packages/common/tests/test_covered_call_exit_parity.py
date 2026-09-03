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
    id = 1


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
    return stock


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
        stock = _build_the_book()
        assert _answers(stock) == (True, 5, CALL)


def test_the_live_shaped_store_answers_IDENTICALLY(tmp_path):
    """The LIVE side: SQLite. Same book, same numbers, same contract.

    A divergence here is the defect class ``trade_repository`` exists for: a raw
    ``select()`` against a RAM-only trial returns EMPTY rather than raising, so the rule
    would read as "no call held" in the backtest and fire in live, with nothing to notice.
    """
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "cc_parity.sqlite"))
    db.init_db()
    stock = _build_the_book()
    assert _answers(stock) == (True, 5, CALL)


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
