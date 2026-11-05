"""The premium style's assigned stock, against the real BacktestAccount (plan Task 9).

Reuses the wheel harness of ``test_wheel_assignment.py`` (``_build`` / ``_engine`` /
``_sell_the_put`` / ``_assign_the_put`` / ``_shares``): one 180 put on AAPL, ITM at its
2024-03-08 expiry (close 164), so 100 shares are delivered at the strike.

* WHILE AN ASSIGNED LOT IS ON THE BOOK NO OTHER MEMBER CAN ENTER THAT SYMBOL. Every premium
  member's entry rule carries a ``has_no_position`` leaf, which is expert-level per instrument
  (``HasNoPositionCondition`` counts the EXPERT's open transactions, whatever member opened
  them) -- and behind it the engine's dup gate. So a share lot and another member's structure
  never overlap on one underlying, which is what lets the concentration grouping treat a wheel
  cycle as one bet without fusing two members' bets. That holds for the wheel's HELD lot and for
  a CSP member's lot waiting one bar for its liquidation; once the lot is sold the symbol is
  free again.
* THE SETTLEMENT PATH IS WHAT PUTS STOCK ON THE BOOK. A short leg outside a unit-settled
  defined-risk combo is assigned PER LEG into shares; inside one it is not. Shown on the same
  two strikes: a 1x2 put BACKSPREAD (per leg: shares delivered -- why O_PBS / O_CBS / O_PMCC are
  in ``_STYLE_ASSIGNS_STOCK``) against a bull put spread (unit-settled: no shares).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ba2_common.core.types import AssetClass, OptionRight, OrderDirection
from tests.backtest.test_wheel_assignment import (
    _BASE_CFG, _EXP_PUT, _PUT180, _assign_the_put, _build, _leg, _sell_the_put, _shares)

_CAND = str(Path(__file__).resolve().parents[4] / "tools/option_styles.candidates.json")
_EXPERT = 909


def _launcher():
    from tests.test_option_grid_foundations import _launcher as load
    return load()


def _premium(m):
    m._STYLE_ACTIVE = m._load_style_spec(_CAND, "premium")
    return m._build_strategy_style("O_STYLE")


def _flat_leaves(strategy):
    """(rule id, the has_no_position leaf) of every premium entry rule -- exactly one each."""
    from ba2_common.core.rule_builders import tree_leaves
    out = []
    for r in strategy.entry_rules:
        flats = [x for x in tree_leaves(r["conditions"]) if x["field"] == "has_no_position"]
        assert len(flats) == 1, f"{r['id']}: {len(flats)} has_no_position leaves"
        out.append((r["id"], flats[0]))
    return out


def _flat(acct, symbol="AAPL"):
    """Evaluate the SAME condition class the engine builds for a ``has_no_position`` leaf."""
    from ba2_common.core.TradeConditions import CONDITION_MAP
    from ba2_common.core.rule_builders import FLAG_FIELD_EVENT
    cls = CONDITION_MAP[FLAG_FIELD_EVENT["has_no_position"]]
    return cls(account=acct, instrument_name=symbol,
               expert_recommendation=SimpleNamespace(instance_id=_EXPERT)).evaluate()


def _tag_and_own_the_put(acct, tag):
    """What the engine's option entry does and this harness bypasses: the entry row carries the
    member tag and the transaction the expert id."""
    from ba2_common.core.db import update_instance
    from ba2_common.core.trade_store import transactions_where
    (entry,) = [o for o in acct.get_orders() if o.contract_symbol == _PUT180]
    entry.data = {**(entry.data or {}), "entry_tag": tag}
    update_instance(entry)
    (txn,) = [t for t in transactions_where() if t.asset_class == AssetClass.OPTION]
    txn.expert_id = _EXPERT
    update_instance(txn)


@pytest.mark.parametrize("member, held", [("O_WHEEL", True), ("O_CSP", False)])
def test_while_assigned_shares_are_on_the_book_no_other_member_can_enter(
        tmp_path, member, held):
    from app.services.backtest.daily_engine import DailyBacktestEngine

    m = _launcher()
    s = _premium(m)
    flats = _flat_leaves(s)
    assert len(flats) == len(m._STYLE_ACTIVE["members"])
    cfg = {**_BASE_CFG, **m._hold_assigned_stock_account_settings("O_STYLE")}
    assert cfg["hold_assigned_stock_tags"] == [m._STYLE_MEMBER_TAGS["O_WHEEL"]]
    acct, ps, ctx = _build(tmp_path, f"styleflat{member}", 9510 + int(held), cfg)
    try:
        _sell_the_put(acct)
        _tag_and_own_the_put(acct, m._STYLE_MEMBER_TAGS[member])
        _assign_the_put(acct, ps, cfg)
        assert _shares(acct) == pytest.approx(100.0)
        assert (acct._pending_assignment_sells == {}) is held

        # the lot is on the book: EVERY member's flat leaf reads False, and the dup gate blocks
        for rid, _leaf in flats:
            assert _flat(acct) is False, rid
        eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
        assert eng._has_open_or_waiting_position(_EXPERT, "AAPL") is True

        # the next bar: the wheel's lot is still held (still blocked); the CSP's is sold (free)
        ps.set_clock(datetime(2024, 3, 11))
        acct.process_pending_assignment_liquidations()
        acct.refresh_orders()
        acct.refresh_transactions()
        assert _shares(acct) == pytest.approx(100.0 if held else 0.0)
        assert _flat(acct) is (not held)
        assert eng._has_open_or_waiting_position(_EXPERT, "AAPL") is held
    finally:
        ctx.__exit__(None, None, None)


_PUT170 = "AAPL240308P00170000"


def _add_the_170_put(tmp_path, name):
    from app.services.backtest.options_cache import OptionsHistoryCache
    cache = OptionsHistoryCache(str(tmp_path / f"{name}_options_cache.sqlite"))
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": _PUT170, "option_type": "put", "strike": 170.0,
         "expiry": _EXP_PUT.isoformat(), "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}])
    cache.write_bar_rows([
        {"occ_symbol": _PUT170, "date": d, "open": px, "high": px, "low": px, "close": px,
         "volume": 100, "underlying": "AAPL", "option_type": "put", "strike": 170.0,
         "expiry": _EXP_PUT.isoformat()}
        for d, px in (("2024-03-06", 12.0), ("2024-03-08", 6.0))])


@pytest.mark.parametrize("strategy, longs, delivers", [
    ("put_backspread", 2, True),      # O_PBS: settled PER LEG -> the short put is assigned
    ("bull_put_spread", 1, False),    # O_BULLPS: a defined-risk combo -> unit-settled, no stock
])
def test_the_settlement_path_decides_whether_a_short_put_becomes_stock(
        tmp_path, strategy, longs, delivers):
    name = f"settle{strategy}"
    acct, ps, ctx = _build(tmp_path, name, 9520 + longs, dict(_BASE_CFG))
    try:
        _add_the_170_put(tmp_path, name)
        long_leg = _leg(_PUT170, OrderDirection.BUY, "buy_to_open", OptionRight.PUT, 170.0,
                        _EXP_PUT)
        long_leg.ratio_qty = longs
        acct.submit_option_order(
            legs=[_leg(_PUT180, OrderDirection.SELL, "sell_to_open", OptionRight.PUT, 180.0,
                       _EXP_PUT), long_leg],
            quantity=1, order_type="market", option_strategy=strategy)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert [p for p in acct.get_option_positions()], "the structure never filled"
        _assign_the_put(acct, ps, dict(_BASE_CFG))
        assert (_shares(acct) == pytest.approx(100.0)) is delivers, _shares(acct)
    finally:
        ctx.__exit__(None, None, None)
