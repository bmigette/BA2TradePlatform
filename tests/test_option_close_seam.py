"""Seam 1 — the equity adjust path and the allocation planner must not see options."""
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType,
)


def _record_broker_calls(account):
    """Wrap the double so the test can see what would have reached the broker.

    ``adjust_quantity_with_tpsl`` swallows every per-order cancel failure, so
    "the mock didn't change" is not evidence that nothing was attempted. Only
    the call log is.
    """
    submitted, canceled = [], []
    real_submit, real_cancel = account.submit_order, account.cancel_order

    def _submit(order, *a, **kw):
        submitted.append(order)
        return real_submit(order, *a, **kw)

    def _cancel(order, *a, **kw):
        canceled.append(order)
        return real_cancel(order, *a, **kw)

    account.submit_order = _submit
    account.cancel_order = _cancel
    return submitted, canceled


class TestAdjustQuantityRefusesOptions:
    def test_adjusting_an_option_transaction_is_refused(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=2.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=2.0, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-1.0)
        assert result["success"] is False
        assert "OPTION" in result["message"]

    def test_adjusting_an_equity_transaction_is_unaffected(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        # Must not be the OPTION refusal. Any other outcome is not this test's business.
        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-5.0)
        assert "OPTION" not in (result["message"] or "")

    def test_an_equity_trim_still_goes_through(self):
        """The guard must not cost the equity path the trim it does today."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-4.0)
        assert result["success"] is True, result["message"]
        assert result["orders_created"]
        assert get_instance(Transaction, txn.id).quantity == 6.0

    def test_the_refusal_writes_no_order_and_moves_nothing(self):
        """Caller-obeys. Without the guard this path does NOT merely build a bad
        order: it persists a MARKET row on the UNDERLYING sized in contracts,
        submits it (no TP/SL leg means nothing else ever would), writes the
        transaction down to the post-trim size, and returns success=True."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_common.core.trade_store import orders_where
        from ba2_trade_platform.core.models import Transaction
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted, _canceled = _record_broker_calls(account)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION,
                                 take_profit=5.2, stop_loss=1.1)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=2.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=2.0, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        before = len(orders_where(transaction_id=txn.id))

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-1.0)

        assert submitted == [], "an equity order reached the broker for an option"
        assert len(orders_where(transaction_id=txn.id)) == before
        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 2.0, "the option was written down without closing"
        assert fresh.take_profit == 5.2 and fresh.stop_loss == 1.1
        assert result["success"] is False
        assert result["orders_created"] == []

    def test_the_refusal_cancels_no_protective_leg(self):
        """Caller-obeys. The refusal lands before the existing option TP/SL legs
        are canceled, so a structure cannot be left naked by a rejected resize."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted, canceled = _record_broker_calls(account)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION,
                                 take_profit=5.2, stop_loss=1.1)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=2.0,
            transaction_id=txn.id, status=OrderStatus.FILLED, filled_qty=2.0,
            asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00250000")
        leg = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=2.0,
            transaction_id=txn.id, side=OrderDirection.SELL,
            order_type=OrderType.OCO, status=OrderStatus.NEW,
            limit_price=5.2, stop_price=1.1, depends_on_order=entry.id,
            asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00250000")

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=1.0)

        assert canceled == [], "an option's protective leg was canceled by the equity path"
        assert submitted == [], "an equity order reached the broker for an option"
        assert get_instance(TradingOrder, leg.id).status == OrderStatus.NEW
        assert result["success"] is False
        assert result["orders_canceled"] == []


def _open_equity_and_option(account_id, symbol="AAPL", shares=100.0, contracts=1.0):
    """A covered call: `shares` of `symbol` plus an option whose symbol IS `symbol`.

    Both transactions reach the account only through their TradingOrder, which is
    what ``_open_transaction_ids`` joins on, so each needs a filled order.
    """
    equity = create_transaction(symbol=symbol, quantity=shares)
    create_trading_order(account_id=account_id, symbol=symbol, quantity=shares,
                         transaction_id=equity.id, status=OrderStatus.FILLED,
                         filled_qty=shares)
    option = create_transaction(symbol=symbol, quantity=contracts,
                                side=OrderDirection.SELL,
                                asset_class=AssetClass.OPTION)
    create_trading_order(account_id=account_id, symbol=symbol, quantity=contracts,
                         transaction_id=option.id, side=OrderDirection.SELL,
                         status=OrderStatus.FILLED, filled_qty=contracts,
                         asset_class=AssetClass.OPTION,
                         contract_symbol="AAPL260116C00250000")
    return equity, option


class _FakeAccount:
    """Duck-typed broker for ``build_position_states``: positions and prices only."""

    def __init__(self, account_id, positions, prices):
        self.id = account_id
        self._positions = positions
        self._prices = prices

    def get_positions(self):
        return self._positions

    def get_instrument_current_price(self, symbols):
        return {s: self._prices[s] for s in symbols if s in self._prices}


class _FakePosition:
    def __init__(self, symbol, qty, cost_basis, market_value):
        self.symbol = symbol
        self.qty = qty
        self.cost_basis = cost_basis
        self.market_value = market_value
        self.side = None


class TestAllocationExcludesOptions:
    def test_an_option_transaction_is_not_in_the_allocation_plan(self):
        """An option txn's symbol is the UNDERLYING, so without an asset_class filter the
        allocator treats a covered call as a holding of the stock and can sell the cover."""
        from ba2_trade_platform.core.portfolio_allocation_service import _open_transaction_ids
        acct_def = create_account_definition()
        equity, option = _open_equity_and_option(acct_def.id)

        ids = _open_transaction_ids(acct_def.id, ["AAPL"]).get("AAPL", [])

        assert equity.id in ids
        assert option.id not in ids

    def test_the_equity_side_is_untouched(self):
        """An account holding only equities plans exactly as it did before the filter:
        every open/closing transaction, grouped by symbol, oldest first."""
        from ba2_trade_platform.core.portfolio_allocation_service import _open_transaction_ids
        from ba2_trade_platform.core.types import TransactionStatus
        acct_def = create_account_definition()
        first = create_transaction(symbol="AAPL", quantity=20.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=20.0,
                             transaction_id=first.id, status=OrderStatus.FILLED,
                             filled_qty=20.0)
        second = create_transaction(symbol="AAPL", quantity=10.0,
                                    status=TransactionStatus.CLOSING)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=second.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        other = create_transaction(symbol="MSFT", quantity=5.0)
        create_trading_order(account_id=acct_def.id, symbol="MSFT", quantity=5.0,
                             transaction_id=other.id, status=OrderStatus.FILLED,
                             filled_qty=5.0)

        assert _open_transaction_ids(acct_def.id, ["AAPL", "MSFT"]) == {
            "AAPL": sorted([first.id, second.id]),
            "MSFT": [other.id],
        }

    def test_the_option_never_reaches_the_state_the_close_loop_walks(self):
        """``_close_symbol`` closes every id in ``PositionState.transaction_ids``, so the
        seam only holds if the covered call is absent THERE, not merely in the query."""
        from ba2_trade_platform.core import portfolio_allocation_service as svc
        acct_def = create_account_definition()
        equity, option = _open_equity_and_option(acct_def.id)
        account = _FakeAccount(
            acct_def.id,
            positions=[_FakePosition("AAPL", 100.0, 15000.0, 16000.0)],
            prices={"AAPL": 160.0},
        )

        states = svc.build_position_states(account, ["AAPL"])

        assert states["AAPL"].transaction_ids == [equity.id]
        assert option.id not in states["AAPL"].transaction_ids


# ===========================================================================
# OPT-L1, THE EXIT HALF -- the TRIM path asks the same question as the close
#
# ``adjust_quantity_with_tpsl`` sells equity too and carried only the OPT-L3
# asset-class guard. A "set AAPL to 50%" allocation run reaches it through
# ``_adjust_symbol``, so the exit half was only half done: the close path refused
# to sell pledged cover and the trim path sold it. Measured before the fix, 100
# shares held with one short call written against them:
#
#   success: True
#   message: Created triggered order chain for partial close of 50.0 shares...
#   sent to broker: [('AAPL', 'SELL', 50.0)]
#
# Half a cover is no cover: that call could still have 100 shares called away.
# ===========================================================================
class TestAdjustQuantityRefusesToTrimPledgedCover:

    OCC = "AAPL260116C00150000"

    def _short_call(self, acct_def, *, contracts=1.0, multiplier=100,
                    underlying="AAPL"):
        from ba2_trade_platform.core.types import OptionRight
        txn = create_transaction(symbol=underlying, quantity=contracts,
                                 side=OrderDirection.SELL,
                                 asset_class=AssetClass.OPTION)
        return create_trading_order(
            account_id=acct_def.id, symbol=self.OCC, quantity=contracts,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=contracts,
            asset_class=AssetClass.OPTION, multiplier=multiplier,
            contract_symbol=self.OCC, underlying_symbol=underlying,
            option_type=OptionRight.CALL, strike=150.0)

    def _long_lot(self, acct_def, *, shares=100.0):
        txn = create_transaction(symbol="AAPL", quantity=shares)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=shares,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=shares)
        return txn

    def _holding(self, account, shares):
        from types import SimpleNamespace
        account._positions = [SimpleNamespace(
            symbol="AAPL", qty=shares, qty_available=shares,
            side=OrderDirection.BUY, asset_class="us_equity")]

    def test_trimming_into_the_cover_is_refused(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.interfaces.AccountInterface import PLEDGED_COVER_REFUSAL
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted, canceled = _record_broker_calls(account)
        self._holding(account, 100.0)
        self._short_call(acct_def)
        txn = self._long_lot(acct_def, shares=100.0)

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)

        assert result["success"] is False
        message = result["message"]
        assert PLEDGED_COVER_REFUSAL in message, message
        assert "short by 50" in message, "the SHORTFALL must be named: " + message
        assert "NAKED" in message, message
        assert submitted == [], "the broker was asked to sell the cover"
        assert result["orders_created"] == []

    def test_trimming_only_the_UNPLEDGED_EXCESS_is_allowed(self):
        """THE INVERSE. 150 held, 100 pledged, trim 50: the call keeps its cover
        and the free shares stay sellable. A guard that refuses every trim on a
        ticker with one written call is not a guard, it is a freeze."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._holding(account, 150.0)
        self._short_call(acct_def)
        txn = self._long_lot(acct_def, shares=150.0)

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)

        assert result["success"] is True, result["message"]
        assert get_instance(Transaction, txn.id).quantity == 100.0

    def test_the_refusal_lands_before_any_order_or_cancel(self):
        """Caller-obeys. The trim persists its close order as the very next
        statement and cancels the position's real TP/SL the statement after, so a
        refusal any further down would leave the shares unprotected on their way
        to being refused — and write the transaction down to a size the account
        never reached."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_common.core.trade_store import orders_where
        from ba2_trade_platform.core.models import Transaction, TradingOrder
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted, canceled = _record_broker_calls(account)
        self._holding(account, 100.0)
        self._short_call(acct_def)
        txn = self._long_lot(acct_def, shares=100.0)
        entry = orders_where(transaction_id=txn.id)[0]
        leg = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            transaction_id=txn.id, side=OrderDirection.SELL,
            order_type=OrderType.OCO, status=OrderStatus.NEW,
            limit_price=180.0, stop_price=140.0, depends_on_order=entry.id)
        before = len(orders_where(transaction_id=txn.id))

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)

        assert result["success"] is False
        assert canceled == [], "the position's protective leg was canceled by a refusal"
        assert get_instance(TradingOrder, leg.id).status == OrderStatus.NEW
        assert submitted == []
        assert len(orders_where(transaction_id=txn.id)) == before, \
            "a refused trim left an order row behind"
        assert get_instance(Transaction, txn.id).quantity == 100.0

    def test_ADDING_to_a_position_is_never_refused(self):
        """A BUY can only ADD cover. Refusing it would block the one action that
        FIXES a shortfall — and every accessor is broken here on purpose to prove
        the question is not even asked."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._holding(account, 100.0)
        self._short_call(acct_def)
        txn = self._long_lot(acct_def, shares=100.0)
        account.get_positions = lambda: (_ for _ in ()).throw(
            AssertionError("an add-to-position must not read the position feed"))
        account.open_option_orders_book_wide = lambda: (_ for _ in ()).throw(
            AssertionError("an add-to-position must not read the option book"))

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=+50.0)

        assert result["success"] is True, result["message"]

    def test_trimming_a_SHORT_position_is_never_refused(self):
        """Closing part of a SHORT equity position BUYS shares back, which can
        only ADD cover; refusing it would strand a short nobody can flatten."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._short_call(acct_def)
        txn = create_transaction(symbol="AAPL", quantity=100.0,
                                 side=OrderDirection.SELL)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=100.0,
                             transaction_id=txn.id, side=OrderDirection.SELL,
                             status=OrderStatus.FILLED, filled_qty=100.0)
        account.get_positions = lambda: (_ for _ in ()).throw(
            AssertionError("a BUY-side trim must not read the position feed"))

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)

        assert result["success"] is True, result["message"]

    def test_a_NON_OPTIONS_account_is_completely_unaffected(self):
        """An account that cannot hold options has nothing pledged by
        construction. The double has no cover accessors at all (they live on the
        mixin), so a guard that asked without checking the capability would
        AttributeError here rather than quietly pass."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from tests.test_accounts.test_account_interface import (
            _equity_only_account_class,
        )
        acct_def = create_account_definition()
        account = _equity_only_account_class()(acct_def.id)
        assert not hasattr(account, "shares_pledged_to_short_calls")
        self._holding(account, 100.0)
        self._short_call(acct_def)          # in the book; not this account's business
        txn = self._long_lot(acct_def, shares=100.0)

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)

        assert result["success"] is True, result["message"]

    def test_the_trim_shares_ONE_decision_with_the_close_path__and_no_exclusion(self):
        """Both seams call the same function, so neither can drift from the other
        about the same account state — the defect being fixed is precisely that
        one of them had never been taught to ask.

        THE SAME FUNCTION, NOT THE SAME ARGUMENTS. ``except_transaction_id`` is
        the close seam's alone: a close disposes of the whole net open quantity,
        so a re-issued one would otherwise refuse itself over its own staged row.
        A trim is never a retry of itself — each one stages an ADDITIONAL slice
        and writes ``transaction.quantity`` down — so it must pass no exclusion,
        or its own earlier slices go unseen. See
        ``TestTwoTrimsOnOneTransactionCannotBothPass`` for what that cost."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._holding(account, 100.0)
        self._short_call(acct_def)
        txn = self._long_lot(acct_def, shares=100.0)
        asked = []
        real = account.cover_refusal_for_equity_sale

        def _spy(symbol, quantity, **kwargs):
            asked.append((symbol, quantity, kwargs.get("except_transaction_id")))
            return real(symbol, quantity, **kwargs)

        account.cover_refusal_for_equity_sale = _spy

        TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)

        assert asked == [("AAPL", 50.0, None)], (
            "the trim must ask about the shares IT is selling, through the shared "
            "decision, and must NOT exclude its own transaction — every trim is an "
            f"additional sale, never a retry of the last one: {asked}")


# ===========================================================================
# TWO TRIMS OF ONE TRANSACTION, AND THE EXCLUSION THAT HID THE FIRST FROM THE
# SECOND.
#
# ``cover_refusal_for_equity_sale`` offers ``except_transaction_id`` so a
# RE-ISSUED CLOSE does not refuse itself: a close disposes of the transaction's
# whole net open quantity, so its own staged row and the row it is about to
# write are the same inventory read twice. The trim seam passed it too, and
# there the reasoning does not hold — a trim is never a retry of itself. Every
# trim stages an ADDITIONAL slice and writes ``transaction.quantity`` DOWN, so
# the next one sizes itself against the reduced figure and, with the exclusion,
# cannot see the slice before it. Measured on the doubles before the fix, 150
# held with 100 pledged and two -50 trims:
#
#   R1 (-50): True   -> order 4: AAPL SELL 50 MARKET WAITING_TRIGGER, txn 150 -> 100
#   R2 (-50): True   -> order 6: AAPL SELL 50 MARKET WAITING_TRIGGER, txn 100 ->  50
#   equity_shares_working_to_sell("AAPL")                        = 50
#   equity_shares_working_to_sell("AAPL", except_transaction_id) =  0  <- the guard
#
# 100 of the 150 shares committed to be sold against a 100-share pledge. The
# arithmetic the guard SHOULD have done at R2 is 150 - 50 - 50 = 50 < 100.
# ===========================================================================
class TestTwoTrimsOnOneTransactionCannotBothPass:

    OCC = TestAdjustQuantityRefusesToTrimPledgedCover.OCC
    _short_call = TestAdjustQuantityRefusesToTrimPledgedCover._short_call
    _holding = TestAdjustQuantityRefusesToTrimPledgedCover._holding

    def _protected_lot(self, acct_def, *, shares=150.0, protected=True):
        """A long AAPL lot, optionally with the live OCO leg that puts the trim
        on the WAITING_TRIGGER branch (and the TP/SL scalars it re-arms from)."""
        from tests.factories import create_transaction as _txn
        txn = _txn(symbol="AAPL", quantity=shares,
                   take_profit=180.0 if protected else None,
                   stop_loss=140.0 if protected else None)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=shares,
            transaction_id=txn.id, status=OrderStatus.FILLED, filled_qty=shares)
        if protected:
            create_trading_order(
                account_id=acct_def.id, symbol="AAPL", quantity=shares,
                transaction_id=txn.id, side=OrderDirection.SELL,
                order_type=OrderType.OCO, status=OrderStatus.NEW,
                limit_price=180.0, stop_price=140.0, depends_on_order=entry.id)
        return txn

    def _cancels_by_id(self, account):
        """``adjust_quantity_with_tpsl`` cancels by ORDER ID, not by object.

        The shared double's ``cancel_order`` takes an object, so without this the
        cancel raises, the WAITING_TRIGGER chain is declared dead and the trim
        aborts before it can demonstrate anything.
        """
        from ba2_common.core.db import get_instance, update_instance
        from ba2_trade_platform.core.models import TradingOrder

        def _cancel(order_or_id):
            oid = order_or_id if isinstance(order_or_id, int) else order_or_id.id
            row = get_instance(TradingOrder, oid)
            row.status = OrderStatus.CANCELED
            update_instance(row)
            return True

        account.cancel_order = _cancel

    def _working_sells(self, account):
        from ba2_common.core.trade_store import orders_where
        from ba2_trade_platform.core.types import OrderStatus as _OS
        return [o for o in orders_where(account_id=account.id,
                                        statuses=_OS.get_active_statuses())
                if o.side == OrderDirection.SELL
                and o.order_type == OrderType.MARKET]

    def test_the_SECOND_trim_sees_the_FIRST_trims_staged_sale(self):
        """THE REGRESSION, on the deterministic WAITING_TRIGGER branch.

        A live protective leg means the partial close is written
        WAITING_TRIGGER and the only broker traffic is the CANCEL of that leg —
        nothing sells, so ``get_positions()`` CANNOT decrement between the two
        trims however slowly they run. The first trim's staged 50 is therefore
        durable, readable state by the time the second one asks, and the second
        must be refused: 150 held − 50 already committed − 50 more = 50 left
        against a 100-share pledge.
        """
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_common.core.interfaces.AccountInterface import PLEDGED_COVER_REFUSAL
        from ba2_trade_platform.core.models import Transaction

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._cancels_by_id(account)
        self._holding(account, 150.0)
        self._short_call(acct_def)                      # pledges 100
        txn = self._protected_lot(acct_def, shares=150.0)

        first = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)
        assert first["success"] is True, first["message"]
        assert get_instance(Transaction, txn.id).quantity == 100.0
        assert account.equity_shares_working_to_sell("AAPL") == 50, (
            "the first trim's staged sale must be visible account-wide")

        second = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=get_instance(Transaction, txn.id),
            qty_change=-50.0)

        assert second["success"] is False, (
            "the second trim sold into the cover: 150 held, 100 pledged, "
            "50 already committed by the first trim")
        message = second["message"]
        assert PLEDGED_COVER_REFUSAL in message, message
        assert "already committed" in message, (
            "the refusal must name the first trim's shares: " + message)
        assert "short by 50" in message, "the SHORTFALL must be named: " + message
        assert second["orders_created"] == []
        assert get_instance(Transaction, txn.id).quantity == 100.0, (
            "a refused trim wrote the transaction down anyway")

    def test_the_first_trims_staged_sale_SURVIVES_the_refusal(self):
        """The refusal must not cost the trim that WAS legitimate.

        Refusing the second trim leaves exactly 50 shares committed against a
        150-share holding — 100 left, precisely the pledge — and the first
        trim's order row untouched and still working.
        """
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._cancels_by_id(account)
        self._holding(account, 150.0)
        self._short_call(acct_def)
        txn = self._protected_lot(acct_def, shares=150.0)

        TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)
        staged = self._working_sells(account)
        TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=get_instance(Transaction, txn.id),
            qty_change=-50.0)

        still_working = self._working_sells(account)
        assert [o.id for o in still_working] == [o.id for o in staged]
        assert sum(o.quantity for o in still_working) == 50.0
        assert account.held_shares_for_cover("AAPL") - 50 == 100 == \
            account.shares_pledged_to_short_calls("AAPL")

    def test_an_UNPROTECTED_first_trim_is_seen_too(self):
        """THE OTHER BRANCH, and the one the old comment's second claim was
        wrong about. With no protective leg the partial close is written PENDING
        and submitted straight out, carrying NO ``depends_on_order`` — so it is
        not in ``get_active_tpsl_orders`` and the next trim's cancel step never
        touches it. It is live at the broker and simply has not printed yet;
        the second trim must be charged for it."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance, update_instance
        from ba2_common.core.interfaces.AccountInterface import PLEDGED_COVER_REFUSAL
        from ba2_trade_platform.core.models import Transaction

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._holding(account, 150.0)
        self._short_call(acct_def)
        txn = self._protected_lot(acct_def, shares=150.0, protected=False)

        def _accepts_and_works(order, **kwargs):
            """A broker that ACCEPTED the sell and has not printed it yet — the
            honest state between submission and fill, and the only one in which
            the shares are both still held and already spoken for."""
            order.status = OrderStatus.NEW
            order.broker_order_id = "bkr-1"
            update_instance(order)
            return order

        account.submit_order = _accepts_and_works

        first = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)
        assert first["success"] is True, first["message"]
        staged = self._working_sells(account)
        assert [o.depends_on_order for o in staged] == [None], (
            "the unprotected partial close must carry no dependency — that is "
            "why the next trim's TP/SL cancel step cannot reach it")

        second = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=get_instance(Transaction, txn.id),
            qty_change=-50.0)

        assert second["success"] is False, second["message"]
        assert PLEDGED_COVER_REFUSAL in second["message"], second["message"]

    def test_TWO_trims_that_BOTH_fit_are_BOTH_allowed(self):
        """THE INVERSE, and the proof this is netting rather than a one-trim
        quota. 200 held with 100 pledged leaves 100 free: two -50 trims spend
        exactly that and both must go through. A guard that refuses the second
        trim on principle is a freeze, not a guard.

        Run on the UNPROTECTED branch so the two staged sales genuinely
        accumulate — see ``test_a_WAITING_TRIGGER_trim_CANCELS_the_previous_one``
        for why the protected branch cannot show a running total."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance, update_instance
        from ba2_trade_platform.core.models import Transaction

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._holding(account, 200.0)
        self._short_call(acct_def)                      # pledges 100
        txn = self._protected_lot(acct_def, shares=200.0, protected=False)

        def _accepts_and_works(order, **kwargs):
            order.status = OrderStatus.NEW
            update_instance(order)
            return order

        account.submit_order = _accepts_and_works

        first = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)
        second = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=get_instance(Transaction, txn.id),
            qty_change=-50.0)

        assert first["success"] is True, first["message"]
        assert second["success"] is True, second["message"]
        assert get_instance(Transaction, txn.id).quantity == 100.0
        assert account.equity_shares_working_to_sell("AAPL") == 100

        # ...and the very next share is over the line.
        third = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=get_instance(Transaction, txn.id),
            qty_change=-1.0)
        assert third["success"] is False, third["message"]

    def test_a_WAITING_TRIGGER_trim_CANCELS_the_previous_one__recorded_here(self):
        """OBSERVED WHILE MEASURING THE ABOVE, and recorded because it is what
        makes the WAITING_TRIGGER branch's running total NOT accumulate — it is
        NOT what keeps the cover safe.

        ``TransactionHelper.is_tpsl_order`` answers True for ANY order carrying
        ``depends_on_order``, and a WAITING_TRIGGER partial close carries one (it
        triggers off the cancel of the leg it replaced). So the NEXT trim's step
        2 — "cancel existing TP/SL orders" — sweeps up the PREVIOUS trim's
        staged sale along with the real OCO, and the earlier trim's 50 shares
        silently never sell even though the transaction was already written down
        for them.

        This is a separate defect, deliberately out of scope here. It must not
        be mistaken for cover protection: the cover guard runs BEFORE step 2 (it
        has to — step 2 strips the position of its protective legs), so at the
        moment of the decision both sales are outstanding; and the sweep cannot
        reach a close already FILLED or one written with no dependency at all.
        Cover safety comes from the guard seeing the earlier row, not from an
        incidental cancel.
        """
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        self._cancels_by_id(account)
        self._holding(account, 400.0)          # far above any pledge: not the subject
        self._short_call(acct_def)
        txn = self._protected_lot(acct_def, shares=400.0)

        TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-50.0)
        first_close = self._working_sells(account)[0]
        TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=get_instance(Transaction, txn.id),
            qty_change=-50.0)

        from ba2_trade_platform.core.models import TradingOrder
        assert get_instance(TradingOrder, first_close.id).status == \
            OrderStatus.CANCELED
        assert account.equity_shares_working_to_sell("AAPL") == 50, (
            "only the newest trim is left working — the running total does not "
            "accumulate on this branch")
        assert get_instance(Transaction, txn.id).quantity == 300.0, (
            "...yet the transaction was written down for BOTH trims")
