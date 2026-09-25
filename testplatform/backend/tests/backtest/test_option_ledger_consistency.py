"""Task 13 (plan 2026-09-24 "Revisions", widened by the Task 1b review): the option LOT ledger
(``_option_positions``, per contract) must agree with the TRANSACTION view
(``get_option_positions()``, per transaction), and the routes that close a lot must book the
close on the transaction(s) that actually hold it.

THE DEFECT. The ledger is keyed by contract; transactions are not. When two OPENED transactions
hold the same contract (two experts, or a re-keyed lot merged at a split), the margin-call
buy-back and ``close_option_position`` found ONE transaction through
``_option_transaction_for_contract`` (the first) and booked the whole close there. The first
transaction was over-closed and the second stayed OPENED holding contracts the ledger no longer
had: an orphan that expiry settles again and the exits keep acting on.

The fixture is the split-rekey harness at a pre-split date (no split is crossed here).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus, TransactionStatus
from ba2_common.core.trade_store import transactions_where

from tests.backtest.test_option_split_crossing import OPEN_DAY, _bar, _dt, _sessions
from tests.backtest.test_option_split_rekey import CFG, NBO, PUT410, _closes, _harness, _open, _rows

D0825, D0826 = OPEN_DAY.replace(day=25), OPEN_DAY.replace(day=26)


def _bars(contract, close):
    return {(contract, d): _bar(close) for d in _sessions() if d < OPEN_DAY.replace(day=31)}


def _add(acct, side, qty):
    """A SECOND transaction on the same contract (``_open`` asserts the lot total)."""
    from tests.backtest.test_option_split_rekey import _leg
    acct.submit_option_order(legs=[_leg(PUT410, OptionRight.PUT, 410.0, side)], quantity=qty,
                             order_type="market", option_strategy="long_put")
    acct.refresh_orders()
    acct.refresh_transactions()


def _txn_ids(acct, contract):
    return sorted({o.transaction_id for o in _rows(acct, contract)})


def _txn(tid):
    (t,) = [t for t in transactions_where() if t.id == tid]
    return t


def _held(acct, contract, tid):
    return sum((o.filled_qty or 0) * (1 if o.side == OrderDirection.BUY else -1)
               for o in _rows(acct, contract)
               if o.transaction_id == tid and o.status in OrderStatus.get_executed_statuses())


def test_a_margin_call_closes_every_transaction_holding_the_contract():
    with _harness(_bars(PUT410, 15.0), _closes()) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.SELL, "short_put", qty=1)
        acct.submit_option_order(legs=[_short_leg()], quantity=2, order_type="market",
                                 option_strategy="short_put")
        acct.refresh_orders()
        acct.refresh_transactions()
        t1, t2 = _txn_ids(acct, PUT410)
        assert acct._option_positions[PUT410].qty == -3
        assert acct.check_option_ledger(context="test") == []

        acct._cash = 0.0                       # equity < 0 -> breach
        assert acct.maybe_margin_call_liquidation()

        assert acct._option_positions[PUT410].qty == 0
        assert (_held(acct, PUT410, t1), _held(acct, PUT410, t2)) == (0, 0)
        assert _txn(t1).status == TransactionStatus.CLOSED
        assert _txn(t2).status == TransactionStatus.CLOSED
        assert acct.get_option_positions() == []
        assert acct.check_option_ledger(context="test") == []


def _short_leg():
    from tests.backtest.test_option_split_rekey import _leg
    return _leg(PUT410, OptionRight.PUT, 410.0, OrderDirection.SELL)


def test_close_option_from_the_second_transaction_is_booked_on_the_second():
    """T1 holds 1 x P410 bought at 15, T2 holds 3 bought at 12 (a day later). T2's exit rule
    fires: the sell-to-close of 3 must ride T2 -- not T1, which used to be over-closed (net -1)
    while T2 stayed OPENED with 3 contracts the ledger no longer held."""
    from ba2_common.core.TradeActions import CloseOptionAction
    from ba2_common.core.types import OrderRecommendation
    bars = {(PUT410, d): _bar(15.0 if d <= OPEN_DAY else 12.0)
            for d in _sessions() if d < OPEN_DAY.replace(day=31)}
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put", qty=1)
        ps.set_clock(_dt(D0825))
        _add(acct, OrderDirection.BUY, 3)
        t1, t2 = _txn_ids(acct, PUT410)
        (t2_entry,) = [o for o in _rows(acct, PUT410) if o.transaction_id == t2]

        ps.set_clock(_dt(D0826))
        action = CloseOptionAction(instrument_name="AAPL", account=acct,
                                   order_recommendation=OrderRecommendation.SELL,
                                   existing_order=t2_entry)
        action.create_and_save_action_result = lambda **kw: SimpleNamespace(**kw)
        assert action.execute().success
        (close,) = [o for o in _rows(acct, PUT410) if o.side == OrderDirection.SELL]
        assert close.transaction_id == t2 and close.quantity == 3

        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[PUT410].qty == 1
        assert _held(acct, PUT410, t1) == 1 and _held(acct, PUT410, t2) == 0
        assert _txn(t1).status == TransactionStatus.OPENED
        assert _txn(t2).status == TransactionStatus.CLOSED
        assert acct.check_option_ledger(context="test") == []


def test_close_option_position_honours_an_explicit_transaction_id():
    bars = {(PUT410, d): _bar(15.0) for d in _sessions() if d < OPEN_DAY.replace(day=31)}
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put", qty=1)
        _add(acct, OrderDirection.BUY, 3)
        t1, t2 = _txn_ids(acct, PUT410)
        pos = [p for p in acct.get_option_positions() if p.quantity == 3][0]
        acct.close_option_position(pos, order_type="market", transaction_id=t2)
        (close,) = [o for o in _rows(acct, PUT410) if o.side == OrderDirection.SELL]
        assert close.transaction_id == t2


def test_a_contract_level_close_matching_no_holder_is_booked_fifo(caplog):
    bars = {(PUT410, d): _bar(15.0) for d in _sessions() if d < OPEN_DAY.replace(day=31)}
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put", qty=1)
        _add(acct, OrderDirection.BUY, 3)
        t1, t2 = _txn_ids(acct, PUT410)
        pos = acct.get_option_positions()[0]
        pos.quantity, pos.avg_entry_price = 4, 99.0        # matches no single holder
        with caplog.at_level(logging.WARNING):
            acct.close_option_position(pos, order_type="market")
        closes = sorted((o.transaction_id, o.quantity) for o in _rows(acct, PUT410)
                        if o.side == OrderDirection.SELL)
        assert closes == [(t1, 1), (t2, 3)]
        assert any("FIFO" in r.getMessage() for r in caplog.records)


def test_the_ledger_check_reports_a_constructed_orphan_once(caplog):
    with _harness(_bars(PUT410, 15.0), _closes()) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put", qty=1)
        assert acct.check_option_ledger(context="test") == []
        acct._option_positions[PUT410].qty = 3.0          # a lot no transaction holds
        with caplog.at_level(logging.ERROR):
            first = acct.check_option_ledger(context="test")
            again = acct.check_option_ledger(context="test")
        assert first == again == [{"contract": PUT410, "lot_qty": 3.0, "view_qty": 1.0}]
        errs = [r.getMessage() for r in caplog.records if "LEDGER MISMATCH" in r.getMessage()]
        assert len(errs) == 1 and PUT410 in errs[0]


def test_the_expiry_pass_runs_the_ledger_check(caplog):
    with _harness(_bars(PUT410, 15.0), _closes()) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put", qty=1)
        acct._option_positions[PUT410].qty = 2.0
        with caplog.at_level(logging.ERROR):
            engine._apply_option_expiry(_dt(D0825))
        assert any("LEDGER MISMATCH (expiry pass 2020-08-25" in r.getMessage()
                   for r in caplog.records)
