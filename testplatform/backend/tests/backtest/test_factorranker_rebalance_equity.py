"""FactorRanker's rebalance sizes on EQUITY and re-prices its stops -- in the backtest, as live.

TWO BT/LIVE ASYMMETRIES, found 2026-09-28 while wiring the ``weighting`` gene:

1. THE BOOK WAS SIZED ON CASH IN THE BACKTEST. ``FactorPortfolioManager.rebalance`` read
   ``expert.get_virtual_balance()``, i.e. ``account.get_balance()``: EQUITY at Alpaca, CASH on
   ``BacktestAccount`` (finding 6; the classic RM's balance was moved onto equity too on 2026-10-07). A fully invested $100k book
   therefore looked like the $279 of cash left over, and the SECOND rebalance of every
   FactorRanker backtest sold the whole book; the third bought it back. Live kept the book.
   The same figure priced the protective stop, so every stop priced after the first fill was
   budgeted on cash. Both now read ``get_virtual_equity()`` -> ``get_tradable_equity()`` ->
   ``get_account_snapshot().equity``, which is cash + marks in both runtimes.

2. THE POST-REBALANCE STOP RE-PRICE NEVER WORKED, AND WAS WRONG WHEN IT DID. It passed
   ``_OpenedTxn`` records to ``adjust_sl`` (AttributeError in both runtimes, logged and
   ignored), priced on PRE-rebalance quantities, and -- once handed a real row -- let Alpaca
   sweep this rebalance's own SELL/BUY up as exit legs (review C1). It is removed: the stop is
   maintained at the order that changes the position (``_submit_buy`` prices an add by the rule
   on the post-add position; ``_reprotect_remainder`` keeps a trim's price by design).

Run from the backend dir:
    python -m pytest tests/backtest/test_factorranker_rebalance_equity.py -v
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pytest

from tests.backtest import test_factorranker_weighting_gene as W
from tests.backtest.test_margin_live_backtest_parity import SYMBOL, invested_world

# Four Mondays: 03-04, 03-11, 03-18, 03-25 -> four rebalances, the last on the final week.
MULTI_END = datetime(2024, 3, 29)


def _spy_rebalances(monkeypatch) -> List[Dict[str, Any]]:
    """Record every rebalance's inputs and deltas at the one place they meet."""
    from ba2_experts.FactorRanker import portfolio

    calls: List[Dict[str, Any]] = []
    real = portfolio.rebalance_deltas

    def spy(target_weights, held_shares, prices, equity, quantity_units=None):
        deltas = real(target_weights, held_shares, prices, equity, quantity_units=quantity_units)
        calls.append({"targets": dict(target_weights), "held": dict(held_shares),
                      "prices": dict(prices), "equity": equity, "deltas": dict(deltas)})
        return deltas

    monkeypatch.setattr(portfolio, "rebalance_deltas", spy)
    return calls


def _spy_adjust_sl(monkeypatch, rebalances) -> List[Dict[str, Any]]:
    """Every stop the backtest account is asked to set, tagged with the rebalance it came from
    (``rebalance`` = index into the rebalance log, or -1 outside any rebalance)."""
    from app.services.backtest.backtest_account import BacktestAccount

    calls: List[Dict[str, Any]] = []
    real = BacktestAccount.adjust_sl

    def spy(self, transaction, new_sl_price, source=""):
        before = {"symbol": transaction.symbol, "open_price": transaction.open_price,
                  "rebalance": len(rebalances) - 1}
        ok = real(self, transaction, new_sl_price, source=source)
        calls.append({**before, "transaction": transaction, "price": new_sl_price,
                      "source": source, "ok": ok,
                      "stored": getattr(transaction, "stop_loss", None)})
        return ok

    monkeypatch.setattr(BacktestAccount, "adjust_sl", spy)
    return calls


def _run(monkeypatch, *, weighting="rank", risk_pct=5.0):
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    rebalances = _spy_rebalances(monkeypatch)
    stops = _spy_adjust_sl(monkeypatch, rebalances)
    _, space = W._space()
    flat = W._genome(space, weighting, **{"model:risk_per_trade_pct": risk_pct})
    _, trial = W._trial(flat, end=MULTI_END)
    with W._hermetic(fractionable=False):
        res = run_daily_backtest(trial)
    return res, rebalances, stops


# ==================================================================================================
# 1. the book stays invested across rebalances
# ==================================================================================================
def test_every_rebalance_sizes_on_the_whole_book_and_only_resizes_it(monkeypatch):
    res, calls, _ = _run(monkeypatch)

    assert len(calls) >= 3, f"expected >= 3 rebalances, got {len(calls)}"
    first = calls[0]
    assert first["held"] == {}                       # the entry
    assert first["equity"] == pytest.approx(100_000.0)

    later_orders = 0
    for n, call in enumerate(calls[1:], start=2):
        book = sum(q * call["prices"][s] for s, q in call["held"].items())
        # THE FIX: the rebalance sees what the account is WORTH (cash + this book), not the
        # cash left over. Under the old code this was a few hundred dollars.
        assert call["equity"] > 0.9 * 100_000.0, (
            f"rebalance {n} sized on {call['equity']:.2f} with a {book:.2f} book held")
        assert call["equity"] >= book
        # ... so it RESIZES the held names instead of selling them.
        for sym, delta in call["deltas"].items():
            held = call["held"].get(sym, 0.0)
            if sym in call["targets"]:
                assert held + delta > 0, f"rebalance {n} liquidated target {sym}"
        turnover = sum(abs(d) * call["prices"][s] for s, d in call["deltas"].items())
        assert turnover < 0.1 * call["equity"], f"rebalance {n} turned over {turnover:.0f}"
        later_orders += len(call["deltas"])
    assert later_orders > 0, "no later rebalance resized anything -- the test proves nothing"

    # And the positions opened at the entry are still open at the end: one entry, no churn.
    open_syms = {p["symbol"] for p in res["open_positions"] if p["qty"]}
    assert open_syms == set(W.SYMBOLS[2:])
    exposure = sum(p["qty"] * p["current_price"] for p in res["open_positions"])
    assert exposure > 0.9 * res["final_equity"]


# ==================================================================================================
# 2. the stop follows the RULE at the order that changes the position -- nothing re-prices after
# ==================================================================================================
RISK_PCT = 5.0


def test_an_add_is_protected_at_the_rule_price_and_a_trim_keeps_its_price(monkeypatch):
    """Review I1: the stop must be priced on the POST-rebalance position.

    * An ADD is priced by ``_submit_buy`` on the position it creates -- held + added, at the
      blended cost, on this rebalance's equity -- and that is what the account stores. The
      expected price is recomputed HERE from the rule, not read back from the code.
    * A TRIM keeps its price (``_reprotect_remainder``'s documented choice): no stop call.
    * Nothing else sets a stop: the post-rebalance re-price pass is gone.
    """
    from ba2_common.core.models import Transaction

    _, rebalances, stops = _run(monkeypatch, risk_pct=RISK_PCT)

    assert {c["source"] for c in stops} == {"initial_setup"}
    adds_checked = 0
    for c in stops:
        assert isinstance(c["transaction"], Transaction) and c["ok"] is True
        assert c["stored"] == pytest.approx(c["price"])
        call = rebalances[c["rebalance"]]
        sym = c["symbol"]
        delta = call["deltas"][sym]
        assert delta > 0, f"a stop was set on {sym} for a non-buy delta {delta}"
        held = call["held"].get(sym, 0.0)
        if held == 0:
            continue            # a new name: its stop is covered by the entry path
        price = call["prices"][sym]
        total = held + delta
        avg_cost = (c["open_price"] * held + price * delta) / total
        rule = avg_cost - (call["equity"] * RISK_PCT / 100.0) / total
        assert c["price"] == pytest.approx(rule, rel=1e-12), (
            f"{sym}: add stop {c['price']} is not the rule on the post-add position ({rule})")
        adds_checked += 1
    assert adds_checked > 0, "no add was re-protected -- the test proves nothing"

    trims = [(n, s) for n, call in enumerate(rebalances)
             for s, d in call["deltas"].items() if d < 0 and call["held"].get(s, 0) + d > 0]
    assert trims, "no trim happened -- the test proves nothing"
    for n, sym in trims:
        assert not [c for c in stops if c["rebalance"] == n and c["symbol"] == sym]


# ==================================================================================================
# 3. BT/live parity: the rebalance sees the same equity for the same book
# ==================================================================================================
def _rebalance_equity(arm) -> float:
    """Drive the REAL FactorPortfolioManager.rebalance on this arm and return the equity it
    sized with (captured at ``rebalance_deltas``). The target keeps the held 10 shares
    (weight 0.25 of $4,000 at $100), so nothing is submitted."""
    from ba2_experts.FactorRanker import portfolio
    from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager

    seen: List[float] = []
    real = portfolio.rebalance_deltas

    def spy(target_weights, held_shares, prices, equity, quantity_units=None):
        seen.append(equity)
        return real(target_weights, held_shares, prices, equity, quantity_units=quantity_units)

    mgr = object.__new__(FactorPortfolioManager)
    mgr.expert_instance_id = arm.instance_id
    mgr.expert = arm.expert
    mgr.account_id = arm.account.id
    mgr.account = arm.account
    portfolio.rebalance_deltas = spy
    try:
        orders = mgr.rebalance({SYMBOL: 0.25})
    finally:
        portfolio.rebalance_deltas = real
    assert orders == []
    assert len(seen) == 1
    return seen[0]


def _state_live_fills(world) -> None:
    from ba2_common.core.db import update_instance
    from ba2_common.core.trade_repository import get_trade_repository
    from ba2_common.core.trade_store import orders_where
    from tests.backtest.test_margin_live_backtest_parity import ENTRY_QTY

    for arm in (world.l1, world.l2):
        (txn,) = get_trade_repository().open_transactions(
            expert_id=arm.instance_id, include_waiting=True)
        (order,) = orders_where(transaction_id=txn.id)
        order.filled_qty = ENTRY_QTY
        update_instance(order)


def test_the_rebalance_sees_the_same_equity_in_the_backtest_and_live():
    """Same $4,000 funding, same 10 x $100 held: the backtest (cash $3,000, equity $4,000), a live 1x
    account and a live $2,000-at-2x account all size the book on $4,000."""
    with invested_world() as world:
        # invested_world fills the backtest entry but does not roll its transaction to
        # OPENED (the engine does that after every fill); get_holdings reads OPENED.
        world.bt.account.refresh_transactions()
        # The live arms' FILLED entry carries no filled_qty (the margin file never reads
        # it); FactorRanker's live holdings are the sum of filled_qty, so state it.
        _state_live_fills(world)
        seen = {arm.name: _rebalance_equity(arm) for arm in world.arms}
        virtual = {arm.name: arm.expert.get_virtual_equity() for arm in world.arms}
        # The classic figure is the same equity share now (finding 6, fixed 2026-10-07).
        classic_bt = world.bt.expert.get_virtual_balance()

    assert seen == pytest.approx({"BT": 4_000.0, "L1": 4_000.0, "L2": 4_000.0})
    assert virtual == pytest.approx(seen)
    assert classic_bt == pytest.approx(4_000.0)
