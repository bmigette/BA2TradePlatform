"""Broker-neutral maintenance of a transaction's standing TP/SL exit order.

THE MODEL (shared by every live broker, see ``docs/plans/2026-10-03-ibkr-support-design.md`` s4.4):
a transaction carries exactly ONE standing exit order, shaped by what is set on it --

    TP and SL  ->  an ``OCO`` row (limit leg = TP, stop leg = SL)
    TP only    ->  a plain limit row
    SL only    ->  a plain stop row
    neither    ->  no exit order at all (a resting placeholder would block / mislead)

While the entry is unfilled the exit row is ``WAITING_TRIGGER`` and exists only in our DB;
``TradeManager._check_all_waiting_trigger_orders`` submits it when the entry reaches ``FILLED``.
Once the entry is filled the exit row is submitted to the broker (``self.submit_order``). A change
of price/size is a cancel of the live row plus a replacement staged ``WAITING_TRIGGER`` on the
cancel (``depends_order_status_trigger = CANCELED``), unless the broker can modify in place
(``_modify_exit_in_place`` hook, IBKR).

This body was ported from ``AlpacaAccount._adjust_tpsl_internal`` and its handlers (which touch
no Alpaca API: only ``self.cancel_order`` / ``self.submit_order`` and our own tables). It is used
by ``IBKRAccount``. ``AlpacaAccount`` keeps its own copy for now: migrating it onto this mixin is a
live-prod change that needs Alpaca's own regression run, and is recorded as a follow-up.

A host class needs: ``self.id``, ``cancel_order(db_id)``, ``submit_order(row)`` and
``get_instrument_current_price(symbol)``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from ba2_common.core.db import get_db, get_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import OrderDirection, OrderOpenType, OrderStatus
from ba2_common.core.types import OrderType as CoreOrderType
from ba2_common.logger import logger


#: How far THROUGH its stop price an OCO stop leg's limit is placed (0.5 %). A DUPLICATE, on purpose,
#: of ``AlpacaAccount.OCO_STOP_LIMIT_CUSHION`` (the constant ``TradeManager._force_close_breached_stops``
#: reads): importing it from the Alpaca adapter would make the IBKR adapter depend on that SDK module,
#: and editing AlpacaAccount is out of scope here. ``tests/test_ibkr_review_fixes.py`` pins the two
#: values equal, so they cannot drift apart unnoticed.
OCO_STOP_LIMIT_CUSHION = 0.005


class ProtectiveLegsMixin:
    """``adjust_tp`` / ``adjust_sl`` / ``adjust_tp_sl`` and the exit-order rows behind them."""

    # ------------------------------------------------------------------
    # Public adjust API (the AccountInterface abstract trio)
    # ------------------------------------------------------------------
    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        return self._adjust_tpsl_internal(transaction, new_tp_price=new_tp_price,
                                          new_sl_price=None, source=source)

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        return self._adjust_tpsl_internal(transaction, new_tp_price=None,
                                          new_sl_price=new_sl_price, source=source)

    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: float | None = None,
                     new_sl_price: float | None = None, source: str = "") -> bool:
        return self._adjust_tpsl_internal(transaction, new_tp_price=new_tp_price,
                                          new_sl_price=new_sl_price, source=source)

    # ------------------------------------------------------------------
    # Hooks a broker may override
    # ------------------------------------------------------------------
    def _modify_exit_in_place(self, session: Session, transaction: Transaction,
                              entry_order: TradingOrder, spec: tuple,
                              live_broker_orders: list, quantity: float) -> bool:
        """Try to move the live exit order(s) to ``spec`` WITHOUT cancelling them.

        Return True when it was done (the caller then skips the cancel-and-replace path), False
        when this broker cannot or when the structure/size differs (the caller falls back). The
        default cannot. Implementations must be all-or-nothing: a half-modified OCO is worse than
        either state.
        """
        return False

    # ------------------------------------------------------------------
    # Comments / maths
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_tpsl_comment(order_type: str, account_id: int, transaction_id: int,
                               parent_order_id: int) -> str:
        from ba2_common.core.TransactionHelper import TransactionHelper
        return TransactionHelper.tpsl_comment(order_type, account_id, transaction_id,
                                              parent_order_id)

    @staticmethod
    def _calculate_tp_percent(entry_order: TradingOrder, tp_price: float) -> float:
        if not entry_order.open_price or entry_order.open_price == 0:
            return 0.0
        return ((tp_price - entry_order.open_price) / entry_order.open_price) * 100

    @staticmethod
    def _calculate_sl_percent(entry_order: TradingOrder, sl_price: float) -> float:
        if not entry_order.open_price or entry_order.open_price == 0:
            return 0.0
        return ((entry_order.open_price - sl_price) / entry_order.open_price) * 100

    def _tpsl_reference_price(self, entry_order: TradingOrder):
        """Best pre-fill anchor the TP/SL were computed against (fill, limit, recommendation
        snapshot, then the live price), so a pending OCO can be re-based to the actual fill."""
        if entry_order.open_price:
            return entry_order.open_price
        if entry_order.limit_price:
            return entry_order.limit_price
        rec_id = getattr(entry_order, "expert_recommendation_id", None)
        if rec_id:
            try:
                from ba2_common.core.models import ExpertRecommendation
                rec = get_instance(ExpertRecommendation, rec_id)
                if rec and getattr(rec, "price_at_date", None):
                    return rec.price_at_date
            except Exception:  # noqa: BLE001 -- falls through to the live price
                pass
        try:
            return self.get_instrument_current_price(entry_order.symbol)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _target_exit_spec(transaction: Transaction, entry_order: TradingOrder):
        """``(order_type, limit_price, stop_price, label)`` or ``None`` (no TP and no SL)."""
        has_tp = transaction.take_profit is not None and transaction.take_profit > 0
        has_sl = transaction.stop_loss is not None and transaction.stop_loss > 0
        exit_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
        if has_tp and has_sl:
            return (CoreOrderType.OCO, transaction.take_profit, transaction.stop_loss, "TPSL")
        if has_tp:
            order_type = (CoreOrderType.SELL_LIMIT if exit_side == OrderDirection.SELL
                          else CoreOrderType.BUY_LIMIT)
            return (order_type, transaction.take_profit, None, "TP")
        if has_sl:
            order_type = (CoreOrderType.SELL_STOP if exit_side == OrderDirection.SELL
                          else CoreOrderType.BUY_STOP)
            return (order_type, None, transaction.stop_loss, "SL")
        return None

    def _build_exit_order(self, transaction: Transaction, entry_order: TradingOrder, spec: tuple,
                          quantity: float, depends_on: int, trigger_status: OrderStatus,
                          comment_suffix: str = "") -> TradingOrder:
        """Build (not persist) a WAITING_TRIGGER exit order for ``spec``."""
        order_type, tp_price, sl_price, label = spec
        exit_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
        comment = self._generate_tpsl_comment(label, self.id, transaction.id, entry_order.id) + comment_suffix
        data = {}
        if tp_price:
            data["tp_percent_target"] = self._calculate_tp_percent(entry_order, tp_price)
        if sl_price:
            data["sl_percent_target"] = self._calculate_sl_percent(entry_order, sl_price)
        ref_price = self._tpsl_reference_price(entry_order)
        if ref_price:
            data["tpsl_reference_price"] = ref_price
        return TradingOrder(
            account_id=self.id, symbol=entry_order.symbol, quantity=quantity, side=exit_side,
            order_type=order_type, limit_price=tp_price, stop_price=sl_price,
            transaction_id=transaction.id, status=OrderStatus.WAITING_TRIGGER,
            depends_on_order=depends_on, depends_order_status_trigger=trigger_status,
            open_type=OrderOpenType.AUTOMATIC, comment=comment, data=data,
            created_at=datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------
    def _adjust_tpsl_internal(self, transaction: Transaction, new_tp_price: float | None = None,
                              new_sl_price: float | None = None, source: str = "") -> bool:
        """Set or move a transaction's TP and/or SL and bring its exit order in line.

        A call with BOTH prices ``None`` is a RECONCILE request: the standing exit order is rebuilt
        (or cancelled) to match the transaction's current TP/SL.
        """
        old_tp = transaction.take_profit
        old_sl = transaction.stop_loss
        try:
            parts = []
            if new_tp_price is not None:
                parts.append(f"TP=${new_tp_price:.2f}")
            if new_sl_price is not None:
                parts.append(f"SL=${new_sl_price:.2f}")
            logger.info(f"Adjusting {', '.join(parts)} for transaction {transaction.id}"
                        f"{f' (source: {source})' if source else ''}")

            with Session(get_db().bind) as session:
                txn = session.get(Transaction, transaction.id)
                if not txn:
                    logger.error(f"Transaction {transaction.id} not found in database")
                    return False

                # Manual-override lock: a non-manual update to a locked side is dropped BEFORE any
                # work; manual updates always pass and set the lock.
                requested_any = new_tp_price is not None or new_sl_price is not None
                if source == "manual":
                    if new_tp_price is not None:
                        txn.tp_manual_override = True
                    if new_sl_price is not None:
                        txn.sl_manual_override = True
                    # Commit the lock NOW: it is the operator's intent and must survive whatever
                    # happens later in this call (and a nested session opened below must not be
                    # able to discard it).
                    session.add(txn)
                    session.commit()
                else:
                    if new_tp_price is not None and txn.tp_manual_override:
                        logger.info(f"Transaction {transaction.id}: ignoring {source or 'auto'} TP "
                                    f"adjustment to ${new_tp_price:.2f} -- TP is manually locked")
                        new_tp_price = None
                    if new_sl_price is not None and txn.sl_manual_override:
                        logger.info(f"Transaction {transaction.id}: ignoring {source or 'auto'} SL "
                                    f"adjustment to ${new_sl_price:.2f} -- SL is manually locked")
                        new_sl_price = None
                    if requested_any and new_tp_price is None and new_sl_price is None:
                        return True  # blocked by policy, not a failure

                entry_order = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == transaction.id,
                        TradingOrder.order_type.in_([CoreOrderType.MARKET, CoreOrderType.BUY_LIMIT,
                                                     CoreOrderType.SELL_LIMIT])
                    ).order_by(TradingOrder.created_at)
                ).first()
                if not entry_order:
                    logger.error(f"No entry order found for transaction {transaction.id}")
                    return False

                # Only the position's OWN resting protection is maintained here (review finding C1
                # 2026-09-29): an add/trim/close working on the same transaction is left alone and
                # only changes the SIZE the protection must cover.
                from ba2_common.core.TransactionHelper import TransactionHelper
                from ba2_common.core.trade_store import orders_where
                entry_filled = entry_order.status in OrderStatus.get_executed_statuses()
                post_fill_qty = None
                if entry_filled:
                    txn_orders = orders_where(transaction_id=transaction.id)
                    try:
                        post_fill_qty = TransactionHelper.post_fill_position_quantity(
                            txn, entry_order.side, txn_orders)
                    except ValueError as e:
                        in_flight = [o for o in txn_orders
                                     if o.status in (OrderStatus.get_unfilled_statuses()
                                                     | OrderStatus.get_unsent_statuses())
                                     and o.id != entry_order.id
                                     and not TransactionHelper.is_resting_protection(o)]
                        if in_flight:
                            logger.error(f"Refusing TP/SL adjustment for transaction "
                                         f"{transaction.id}: {e}, and order(s) "
                                         f"{[o.id for o in in_flight]} are still working")
                            return False
                        logger.error(f"Transaction {transaction.id}: {e}; sizing its exit on "
                                     f"transaction.quantity={txn.quantity} as before")

                tp_unchanged = (new_tp_price is None or
                                (txn.take_profit is not None and abs(txn.take_profit - new_tp_price) < 0.01))
                sl_unchanged = (new_sl_price is None or
                                (txn.stop_loss is not None and abs(txn.stop_loss - new_sl_price) < 0.01))
                if tp_unchanged and sl_unchanged:
                    target_spec = self._target_exit_spec(txn, entry_order)
                    valid = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.transaction_id == transaction.id,
                            TradingOrder.order_type.in_([
                                CoreOrderType.OCO, CoreOrderType.SELL_LIMIT, CoreOrderType.BUY_LIMIT,
                                CoreOrderType.SELL_STOP, CoreOrderType.BUY_STOP]),
                            TradingOrder.status.notin_([OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                                        OrderStatus.ERROR, OrderStatus.REJECTED])
                        )
                    ).all()
                    valid = [o for o in valid if TransactionHelper.is_resting_protection(o)]
                    if target_spec is None:
                        if not valid:
                            logger.info(f"Skipping TP/SL adjustment for transaction {transaction.id}: "
                                        f"no TP/SL set and no exit orders exist")
                            return True
                    elif valid:
                        target_type, want_tp, want_sl, _ = target_spec
                        matches = True
                        for order in valid:
                            if order.order_type != target_type:
                                matches = False
                            elif want_tp is not None and (order.limit_price is None
                                                          or abs(order.limit_price - want_tp) >= 0.01):
                                matches = False
                            elif want_sl is not None and (order.stop_price is None
                                                          or abs(order.stop_price - want_sl) >= 0.01):
                                matches = False
                            elif (post_fill_qty is not None and order.parent_order_id is None
                                  and (order.quantity is None
                                       or abs(float(order.quantity) - post_fill_qty) > 1e-9)):
                                matches = False
                            if not matches:
                                break
                        if matches:
                            logger.info(f"Skipping TP/SL adjustment for transaction {transaction.id}: "
                                        f"values unchanged and {len(valid)} valid order(s) already "
                                        f"exist with correct structure and prices")
                            return True

                if new_tp_price is not None:
                    txn.take_profit = new_tp_price
                if new_sl_price is not None:
                    txn.stop_loss = new_sl_price
                session.add(txn)
                session.commit()

                all_orders = [
                    o for o in session.exec(
                        select(TradingOrder).where(
                            TradingOrder.transaction_id == transaction.id,
                            TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                            TradingOrder.id != entry_order.id)
                    ).all()
                    if o.status not in OrderStatus.get_executed_statuses()
                    and TransactionHelper.is_resting_protection(o)
                ]
                spec = self._target_exit_spec(txn, entry_order)

                if entry_order.status in (OrderStatus.get_unsent_statuses()
                                          | OrderStatus.get_unfilled_statuses()):
                    result = self._handle_unfilled_entry_exit(session, txn, entry_order, spec, all_orders)
                elif entry_order.status in OrderStatus.get_executed_statuses():
                    result = self._handle_filled_entry_exit(session, txn, entry_order, spec,
                                                            all_orders, quantity=post_fill_qty)
                else:
                    logger.warning(f"Entry order {entry_order.id} in unexpected state: "
                                   f"{entry_order.status.value}")
                    result = False

                self._log_tpsl_activity(txn, entry_order, old_tp, old_sl, new_tp_price,
                                        new_sl_price, source, ok=result, error=None)
                return result
        except Exception as e:  # noqa: BLE001 -- reported as a failed adjustment
            logger.error(f"Error adjusting TP/SL for transaction {transaction.id}: {e}", exc_info=True)
            self._log_tpsl_activity(transaction, None, old_tp, old_sl, new_tp_price, new_sl_price,
                                    source, ok=False, error=str(e))
            return False

    def _log_tpsl_activity(self, txn, entry_order, old_tp, old_sl, new_tp, new_sl, source, *,
                           ok: bool, error: Optional[str]) -> None:
        try:
            from ba2_common.core.db import log_activity
            from ba2_common.core.types import ActivityLogSeverity, ActivityLogType

            desc = []
            if new_tp is not None:
                desc.append(f"TP {f'${old_tp:.2f}' if old_tp else 'none'} -> ${new_tp:.2f}")
            if new_sl is not None:
                desc.append(f"SL {f'${old_sl:.2f}' if old_sl else 'none'} -> ${new_sl:.2f}")
            suffix = f" (source: {source})" if source else ""
            if ok:
                text, severity = (f"Adjusted {' and '.join(desc)} for {txn.symbol}{suffix}",
                                  ActivityLogSeverity.SUCCESS)
            else:
                reason = error if error else "broker rejected order"
                text, severity = (f"Failed to adjust {' and '.join(desc)} for {txn.symbol}{suffix}: "
                                  f"{reason}", ActivityLogSeverity.FAILURE)
            data = {"transaction_id": txn.id, "symbol": txn.symbol, "old_tp": old_tp,
                    "new_tp": new_tp, "old_sl": old_sl, "new_sl": new_sl, "source": source}
            if entry_order is not None:
                data["entry_order_status"] = entry_order.status.value
            if not ok:
                data["error"] = error if error else "broker rejected order"
            log_activity(severity=severity, activity_type=ActivityLogType.TP_SL_ADJUSTED,
                         description=text, data=data, source_account_id=self.id,
                         source_expert_id=txn.expert_id)
        except Exception as log_error:  # noqa: BLE001 -- logging must never fail the adjust
            logger.warning(f"Failed to log TP/SL adjustment activity: {log_error}")

    # ------------------------------------------------------------------
    # Exit-order handlers
    # ------------------------------------------------------------------
    def _handle_unfilled_entry_exit(self, session: Session, transaction: Transaction,
                                    entry_order: TradingOrder, spec: tuple | None,
                                    all_orders: list) -> bool:
        """Entry not filled: keep at most ONE DB-only WAITING_TRIGGER exit order shaped by ``spec``."""
        keep: TradingOrder | None = None
        for order in all_orders:
            if (spec and keep is None and order.order_type == spec[0]
                    and order.status == OrderStatus.WAITING_TRIGGER and not order.broker_order_id
                    and order.depends_on_order == entry_order.id):
                keep = order
                continue
            if order.broker_order_id:
                try:
                    self.cancel_order(order.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Failed to cancel broker order {order.id}: {e}")
            else:
                order.status = OrderStatus.CANCELED
                session.add(order)
        session.commit()

        if spec is None:
            logger.info(f"No TP/SL set for transaction {transaction.id} -- no exit order created")
            return True
        order_type, tp_price, sl_price, _label = spec
        if keep:
            keep.limit_price = tp_price
            keep.stop_price = sl_price
            session.add(keep)
            session.commit()
        else:
            exit_order = self._build_exit_order(transaction, entry_order, spec,
                                                quantity=entry_order.quantity,
                                                depends_on=entry_order.id,
                                                trigger_status=OrderStatus.FILLED)
            session.add(exit_order)
            session.commit()
            logger.info(f"Created pending {order_type.value} exit order {exit_order.id} waiting "
                        f"for entry {entry_order.id} to fill: TP={tp_price}, SL={sl_price}")
        return True

    def _handle_filled_entry_exit(self, session: Session, transaction: Transaction,
                                  entry_order: TradingOrder, spec: tuple | None, all_orders: list,
                                  quantity: float | None = None) -> bool:
        """Entry filled: maintain the target exit structure at the broker."""
        order_quantity = transaction.quantity if quantity is None else quantity
        terminal = OrderStatus.get_terminal_statuses()
        existing = [o for o in all_orders if o is not entry_order]
        live = [o for o in existing if o.broker_order_id and o.status not in terminal]
        db_only = [o for o in existing if o not in live]

        for order in db_only:
            if order.status not in terminal:
                order.status = OrderStatus.CANCELED
                session.add(order)
        if db_only:
            session.commit()

        if spec is None:
            for order in live:
                try:
                    self.cancel_order(order.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Failed to cancel broker order {order.id}: {e}")
            return True

        order_type, tp_price, sl_price, _label = spec
        if not order_quantity or order_quantity <= 0:
            logger.error(f"Not placing a {order_type.value} exit for transaction {transaction.id}: "
                         f"the position is {order_quantity} once its working orders fill -- "
                         f"nothing to protect")
            return False

        if not live:
            return self._create_broker_exit_order(session, transaction, entry_order, spec,
                                                  order_quantity)

        # The broker can move a live exit without a gap in protection.
        if self._modify_exit_in_place(session, transaction, entry_order, spec, live, order_quantity):
            return True

        live.sort(key=lambda o: o.id, reverse=True)
        parent_for_trigger = live[0]
        waiting_exit = self._build_exit_order(
            transaction, entry_order, spec, quantity=order_quantity,
            depends_on=parent_for_trigger.id, trigger_status=OrderStatus.CANCELED,
            comment_suffix=f" (chained on cancel of {parent_for_trigger.id})")
        session.add(waiting_exit)
        session.commit()
        logger.info(f"Staged replacement {order_type.value} exit order {waiting_exit.id} "
                    f"(WAITING_TRIGGER) for transaction {transaction.id} -- will submit when order "
                    f"{parent_for_trigger.id} reaches CANCELED at broker")

        failed = []
        for order in live:
            try:
                ok = self.cancel_order(order.id)
            except Exception as e:  # noqa: BLE001
                ok = False
                logger.error(f"Failed to cancel broker order {order.id}: {e}", exc_info=True)
            if not ok:
                failed.append(order.id)
        if failed:
            if parent_for_trigger.id in failed:
                waiting_exit.status = OrderStatus.CANCELED
                session.add(waiting_exit)
                session.commit()
            logger.error(f"Exit replacement for transaction {transaction.id} NOT complete: the "
                         f"broker did not accept the cancel of protective order(s) {failed}")
            return False
        return True

    def _create_broker_exit_order(self, session: Session, transaction: Transaction,
                                  entry_order: TradingOrder, spec: tuple,
                                  quantity: float) -> bool:
        """Persist a PENDING exit row for ``spec`` and submit it now (OCO, limit or stop)."""
        order_type, tp_price, sl_price, label = spec
        if order_type == CoreOrderType.OCO:
            if not tp_price or tp_price <= 0 or not sl_price or sl_price <= 0:
                logger.error(f"Cannot create OCO order for transaction {transaction.id}: invalid "
                             f"take_profit {tp_price} / stop_loss {sl_price}")
                return False
        elif tp_price is not None and tp_price <= 0:
            logger.error(f"Cannot create TP order for transaction {transaction.id}: invalid "
                         f"take_profit {tp_price}")
            return False
        elif sl_price is not None and sl_price <= 0:
            logger.error(f"Cannot create SL order for transaction {transaction.id}: invalid "
                         f"stop_loss {sl_price}")
            return False
        if not quantity or quantity <= 0:
            logger.error(f"Cannot create {order_type.value} order for transaction {transaction.id}: "
                         f"invalid quantity {quantity}")
            return False

        exit_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
        data = {}
        if tp_price:
            data["tp_percent_target"] = self._calculate_tp_percent(entry_order, tp_price)
        if sl_price:
            data["sl_percent_target"] = self._calculate_sl_percent(entry_order, sl_price)
        if order_type == CoreOrderType.OCO:
            data["tpsl_reference_price"] = self._tpsl_reference_price(entry_order)
        row = TradingOrder(
            account_id=self.id, symbol=entry_order.symbol, quantity=quantity, side=exit_side,
            order_type=order_type, limit_price=tp_price, stop_price=sl_price,
            transaction_id=transaction.id, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment=self._generate_tpsl_comment(label, self.id, transaction.id, entry_order.id),
            data=data, created_at=datetime.now(timezone.utc))
        session.add(row)
        session.commit()
        session.refresh(row)
        try:
            self.submit_order(row)
            session.refresh(row)
            if row.status == OrderStatus.ERROR:
                logger.error(f"{order_type.value} exit order {row.id} was rejected by the broker "
                             f"(status=ERROR) for transaction {transaction.id}")
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to submit {order_type.value} exit order: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # PENDING dependents (cancel-replace flow)
    # ------------------------------------------------------------------
    def _check_and_submit_dependent_orders(self) -> int:
        """Submit PENDING dependents whose parent reached its trigger status (or any terminal
        status when none is set). ``WAITING_TRIGGER`` dependents are TradeManager's, not these."""
        try:
            to_submit, to_error = [], []
            with Session(get_db().bind) as session:
                dependents = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == self.id,
                        TradingOrder.status == OrderStatus.PENDING,
                        TradingOrder.depends_on_order.is_not(None))
                ).all()
                for order in dependents:
                    parent = session.exec(
                        select(TradingOrder).where(TradingOrder.id == order.depends_on_order)).first()
                    if not parent:
                        logger.warning(f"Order {order.id} depends on non-existent order "
                                       f"{order.depends_on_order}")
                        continue
                    if order.depends_order_status_trigger:
                        met = parent.status == order.depends_order_status_trigger
                    else:
                        met = parent.status in OrderStatus.get_terminal_statuses()
                    if not met:
                        continue
                    if not parent.quantity or parent.quantity <= 0:
                        to_error.append(order.id)
                        continue
                    to_submit.append(order.id)

            from ba2_common.core.db import update_instance
            for order_id in to_error:
                row = get_instance(TradingOrder, order_id)
                if row:
                    row.status = OrderStatus.ERROR
                    update_instance(row)

            triggered = 0
            for order_id in to_submit:
                try:
                    row = get_instance(TradingOrder, order_id)
                    if row and row.status == OrderStatus.PENDING:
                        self.submit_order(row)
                        triggered += 1
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Failed to submit dependent order {order_id}: {e}", exc_info=True)
                    row = get_instance(TradingOrder, order_id)
                    if row:
                        row.status = OrderStatus.ERROR
                        update_instance(row)
            return triggered
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error checking dependent orders: {e}", exc_info=True)
            return 0
