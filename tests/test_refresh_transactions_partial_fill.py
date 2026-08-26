"""Tests for ReadOnlyAccountInterface.refresh_transactions and canceled partial fills.

A cancel-and-replace (TP/SL rebase) can race a live fill: the broker executes
part of the order before honoring the cancel, leaving the order CANCELED with
filled_qty > 0. refresh_transactions() recalculates each open transaction's
quantity from its orders on every call (it runs on every TradeManager cycle,
not just on a detected status change), so if it drops those genuinely-filled
shares it permanently re-inflates the transaction back to the pre-fill
quantity - even overwriting a manual correction made via the Overview UI.
This is the NNE "Quantity Mismatch: broker +8 / transactions +12" incident
that kept reappearing no matter how many times it was manually fixed
(2026-06-24).
"""
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction, create_trading_order
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


class TestRefreshTransactionsCanceledPartialFill:
    def test_canceled_partial_fill_counted_in_recalculated_quantity(self):
        """A SELL order that partially filled (4/6) before being CANCELED must
        still reduce the recalculated transaction quantity - the 4 shares
        really traded at the broker."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=4.0,
            transaction_id=txn.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 2.0

    def test_canceled_zero_fill_does_not_count(self):
        """A CANCELED order that never filled (filled_qty falsy) must not
        contribute - this keeps the never-filled / rejected-leg case
        behaving exactly as before."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=0.0,
            transaction_id=txn.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 6.0


class TestRefreshTransactionsMultiLegOptionCombo:
    """A multi-leg option combo (e.g. call_butterfly) writes its "structures"
    quantity onto BOTH its PARENT order (asset_class OPTION, no contract_symbol)
    and each CHILD leg order (contract_symbol + parent_order_id set) - each leg
    independently carries that same structures count, scaled by its own ratio.
    refresh_transactions() must count the parent's filled_qty ONCE and ignore
    the legs entirely; summing every order unconditionally counted one combo
    fill event (1 parent + N legs) times, which - compounding every bar as the
    resulting bogus quantity inflated mark-to-market equity and therefore the
    next trade's position size - produced multi-trillion-scale runaway
    quantities in the options optimization grid (2026-07-21)."""

    def test_child_leg_fills_excluded_from_parent_quantity(self):
        """A child leg's filled_qty must not add onto the parent's - only the
        parent's own filled_qty is the transaction-level position size."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="WMT", quantity=5.0, side=OrderDirection.BUY)
        parent = create_trading_order(
            account_id=acct_def.id, symbol="WMT", quantity=5.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=5.0,
            transaction_id=txn.id, asset_class=AssetClass.OPTION,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="WMT240101C00050000", quantity=999.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=999.0, transaction_id=txn.id, asset_class=AssetClass.OPTION,
            contract_symbol="WMT240101C00050000", parent_order_id=parent.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 5.0

    def test_standalone_single_leg_option_order_still_counted(self):
        """A single-leg option order (carries contract_symbol but NO
        parent_order_id - it IS the fillable order, not a combo child) must
        still count normally; only actual combo children get excluded."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="WMT", quantity=5.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="WMT240101C00050000", quantity=5.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=5.0, transaction_id=txn.id, asset_class=AssetClass.OPTION,
            contract_symbol="WMT240101C00050000",
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 5.0


class TestHasPendingClosingOrder:
    """``ReadOnlyAccountInterface.has_pending_closing_order`` — shared by live
    (``TradeManager.process_open_positions_recommendations``) and the backtest engine
    (``DailyBacktestEngine._held_transactions``) — must report True only while a
    transaction's closing order is still WORKING (not yet filled/canceled), so callers
    managing open positions skip re-evaluating exit rules (and re-submitting a close) until
    it resolves. See testplatform/backend/tests/backtest/test_pending_close_guard.py for the
    backtest-side coverage of the same shared method."""

    def test_true_while_closing_order_still_working(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.NEW,
            transaction_id=txn.id,
        )

        assert account.has_pending_closing_order(txn.id) is True

    def test_false_once_closing_order_is_terminal(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )

        assert account.has_pending_closing_order(txn.id) is False

    def test_false_with_no_closing_order_submitted(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )

        assert account.has_pending_closing_order(txn.id) is False

    def test_dependent_tp_sl_order_is_not_treated_as_a_pending_close(self):
        """A dependent TP/SL leg (``depends_on_order`` set) is NOT a market-entry-level
        order, so it must not affect this check at all — a still-WORKING bracket order
        sitting at the broker is normal and must not suppress exit-rule management."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.NEW,
            transaction_id=txn.id, depends_on_order=entry.id,
        )

        assert account.has_pending_closing_order(txn.id) is False


# ===========================================================================
# OPT-S3 (live) / OPT-S8 (backtest) — ONE LEG SETTLING IS NOT THE STRUCTURE
# CLOSING. One branch, both engines.
#
# ``refresh_transactions``' "OPENED -> CLOSED: filled closing order (TP/SL)"
# arm fires on ANY filled DEPENDENT order. On a multi-leg option structure a
# single leg produces one by itself — an assignment, an expiry settlement (the
# backtest's ``_record_option_expiry_close`` links its synthetic close to the
# entry via ``depends_on_order``), or a one-leg margin liquidation — and the
# whole transaction was closed as ``tp_sl_filled``, pre-empting the
# per-contract ``position_balanced`` that is the only thing here that knows
# whether the STRUCTURE is flat.
#
# The surviving legs — INCLUDING the protective long of a spread — then vanish
# from ``get_option_positions`` and ``_option_transaction_for_contract`` (both
# filter ``TransactionStatus.OPENED``), so nothing can see, manage, expire or
# close them; in the backtest their ``_OptionLot`` stays in the ledger and keeps
# being charged maintenance margin. Same orphaning as the B10 defect, through a
# different door: B10 came in via the mixed-unit balance sums, this walks past
# that fix. See testplatform/backend/tests/backtest/test_spread_orphan_leg.py
# for the engine-level half, where the surviving leg's visibility is asserted
# against a real ``get_option_positions``.
# ===========================================================================
class TestOneLegSettlingDoesNotCloseTheStructure:

    CALL = "AAPL260116C00210000"
    PUT = "AAPL260116P00180000"

    def _short_strangle(self, acct_def, *, contracts=1.0):
        """The real row shape ``submit_option_order`` writes for 2-4 legs: a
        net-only PARENT (OPTION, no contract_symbol, no parent_order_id) plus
        one contract-carrying CHILD per leg, joined by ``parent_order_id``."""
        txn = create_transaction(symbol="AAPL", quantity=contracts,
                                 side=OrderDirection.SELL)
        parent = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=contracts,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=contracts,
            transaction_id=txn.id, asset_class=AssetClass.OPTION,
            option_strategy="short_strangle", open_price=1.0)
        for occ in (self.CALL, self.PUT):
            create_trading_order(
                account_id=acct_def.id, symbol=occ, quantity=contracts,
                side=OrderDirection.SELL, order_type=OrderType.MARKET,
                status=OrderStatus.FILLED, filled_qty=contracts,
                transaction_id=txn.id, asset_class=AssetClass.OPTION,
                contract_symbol=occ, underlying_symbol="AAPL",
                parent_order_id=parent.id, open_price=1.0)
        return txn, parent

    def _settle_leg(self, acct_def, txn, parent, occ, *, contracts=1.0):
        """One leg bought back, recorded the way every one-leg settlement is:
        a standalone contract-carrying order (NO ``parent_order_id`` — it is not
        part of the entry combo) linked to the entry by ``depends_on_order``,
        which is exactly what puts it in ``filled_closing_orders``."""
        return create_trading_order(
            account_id=acct_def.id, symbol=occ, quantity=contracts,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=contracts,
            transaction_id=txn.id, asset_class=AssetClass.OPTION,
            contract_symbol=occ, underlying_symbol="AAPL",
            depends_on_order=parent.id, open_price=0.5)

    def test_one_leg_of_a_strangle_bought_back_leaves_it_OPENED(self):
        """THE HEADLINE. The call leg is closed; the short PUT is still live and
        unmanaged if this transaction goes CLOSED."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn, parent = self._short_strangle(acct_def)
        self._settle_leg(acct_def, txn, parent, self.CALL)

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.OPENED, (
            f"one leg settling closed the whole structure (close_reason="
            f"{fresh.close_reason!r}) — the short {self.PUT} is now orphaned")
        assert fresh.close_reason is None

    def test_the_surviving_leg_is_still_reachable(self):
        """WHY IT MATTERS, stated as the property the accessors depend on.
        ``get_option_positions`` and ``_option_transaction_for_contract`` both
        find a leg only through an OPENED transaction, so the surviving contract
        must still resolve to this one."""
        from ba2_common.core.trade_store import orders_where, transactions_where

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn, parent = self._short_strangle(acct_def)
        self._settle_leg(acct_def, txn, parent, self.CALL)

        account.refresh_transactions()

        opened = {t.id for t in transactions_where(status=TransactionStatus.OPENED)}
        survivors = [o for o in orders_where(account_id=acct_def.id,
                                             parent_order_id=parent.id)
                     if o.contract_symbol == self.PUT]
        assert len(survivors) == 1
        assert survivors[0].transaction_id in opened, (
            "the surviving put leg can no longer be found through an OPENED "
            "transaction — it is invisible to expiry, management and exit")

    def test_BOTH_legs_closed_DOES_close_the_structure(self):
        """THE INVERSE, and the proof this is not a freeze. Every contract flat
        means the structure is flat, and the transaction must close exactly as
        it did before."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn, parent = self._short_strangle(acct_def)
        self._settle_leg(acct_def, txn, parent, self.CALL)
        self._settle_leg(acct_def, txn, parent, self.PUT)

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "tp_sl_filled"

    def test_a_SINGLE_LEG_option_still_closes_on_its_own_closing_fill(self):
        """NOT HELD BACK. A single leg writes one contract-carrying order and no
        net-only parent, so there is no sibling to wait on; stranding it OPENED
        would be the mirror of the bug this fixes.

        The ``close_reason`` is asserted, not just the status: a single leg's
        buy and sell also balance, so the LAST arm of the chain would close it
        as ``position_balanced`` regardless. Only ``tp_sl_filled`` shows the
        branch under test still fires for it."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=1.0,
                                 side=OrderDirection.SELL)
        entry = create_trading_order(
            account_id=acct_def.id, symbol=self.CALL, quantity=1.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=1.0, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, contract_symbol=self.CALL,
            underlying_symbol="AAPL", open_price=1.0)
        create_trading_order(
            account_id=acct_def.id, symbol=self.CALL, quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=1.0, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, contract_symbol=self.CALL,
            underlying_symbol="AAPL", depends_on_order=entry.id, open_price=0.5)

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "tp_sl_filled"

    def test_an_EQUITY_transaction_is_completely_unaffected(self):
        """No OPTION orders means no net-only parent, so the new condition can
        never be true here and the everyday TP/SL close is untouched."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=100.0,
                                 side=OrderDirection.BUY)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=100.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=100.0, transaction_id=txn.id,
            open_price=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
            status=OrderStatus.FILLED, filled_qty=100.0, transaction_id=txn.id,
            depends_on_order=entry.id, limit_price=12.0, open_price=12.0)

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "tp_sl_filled"
        assert fresh.close_price == 12.0

    def test_an_EQUITY_transaction_is_unaffected_even_when_it_is_UNBALANCED(self):
        """THE SCOPE OF THE GUARD — and the conjunct every balanced fixture hides.

        ``one_leg_of_many_settled`` is ``bool(multi_leg_parent_ids) and not
        position_balanced``. The two tests above close their position in FULL, so
        ``position_balanced`` is True in both and the whole expression is False
        WHATEVER the first conjunct says. Dropping ``bool(multi_leg_parent_ids)``
        therefore passes them, and OPT-S3 — a rule about multi-leg OPTION structures —
        silently starts governing every equity transaction in the book. (Measured: that
        edit survives the entire live and backtest option suite without it.)

        Here the sale is PARTIAL, 40 of 100 shares, so ``position_balanced`` is False
        and ONLY ``bool(multi_leg_parent_ids)`` can keep this branch reachable.

        WHAT THIS DOES NOT SAY. Marking the transaction CLOSED with 60 shares still
        held, on the strength of one filled dependent order, is this branch's
        PRE-EXISTING behaviour and is not endorsed here — it long predates OPT-S3 and is
        a separate question. What is asserted is only that OPT-S3 did not change it, in
        either direction: an equity transaction carries no OPTION orders and so no
        net-only parent, and the guard must never reach it.
        """
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=100.0,
                                 side=OrderDirection.BUY)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=100.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=100.0, transaction_id=txn.id,
            open_price=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=40.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=40.0, transaction_id=txn.id,
            depends_on_order=entry.id, open_price=12.0)

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 60.0, (
            "the fixture is only interesting while the position is UNBALANCED")
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "tp_sl_filled", (
            "the OPT-S3 guard has reached an EQUITY transaction — it must key on "
            "multi_leg_parent_ids, which an equity transaction can never have")
