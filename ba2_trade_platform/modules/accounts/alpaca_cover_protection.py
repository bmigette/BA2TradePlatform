"""Alpaca: suspend the platform's own resting stop while a covered call pledges its shares.

THE BUG (paper, Alpaca). An expert bought 100 shares with a plain equity buy; the platform
staged a resting GTC SELL_STOP for all 100 (the stop-loss). The next day "Sell Covered Call"
was rejected ``{"code":40310000,"message":"account not eligible to trade uncovered option
contracts"}``: the stop held every share (``qty_available`` 0), so the broker saw the call as
uncovered. It failed every day. (The recorded KNOWN GAP in ``OptionsAccountInterface``.)

THE RULE, which is what the backtest already does (``BacktestAccount._pledged_share_lock``):
shares pledged to an open short call are never sold. A stock stop / take-profit cannot fire
while the call is open, and works again once it is gone. Live cannot "not fire" an order that
rests at the broker, so it SUSPENDS it:

1. ``_release_cover_protection_for_short_calls`` (the seam ``submit_option_order`` calls for a
   ``covered_call``): cancel the resting protective legs of the equity transaction(s) that hold
   the covering shares, wait (bounded) until the broker reports the shares AVAILABLE, then let
   the call go. ``transaction.stop_loss`` / ``take_profit`` are untouched -- the protection is
   suspended, not removed -- and the transaction carries ``meta_data["protection_suspended"]``.
2. A call that is not submitted or not accepted re-stages the legs at once.
3. While suspended, NOTHING may put a sell order on the pledged shares: ``_adjust_tpsl_internal``
   stores the new values and stages nothing, ``_submit_order_impl`` refuses a protective leg
   (the net under the waiting-trigger sweep, the PENDING dependents and the inline creators),
   and ``TradeManager``'s breached-stop safety net and partial-fill re-protection skip the lot.
   Each skip logs and the marker stays on the transaction.
4. ``reconcile_suspended_protection`` (called from the account refresh) re-stages the legs from
   the stored values when the call is gone and the shares are still held, and clears the marker.
   Shares called away close their equity transaction (``_settle_called_away``) and nothing is
   re-staged.

THE MARKER. ``meta_data["protection_suspended"] = {"reason": "pledged_to_short_call", "stage":
..., "symbol", "since", "cancelled_order_ids", "call_order_id"}``; ``stage`` is:

* ``releasing``   -- the legs are being cancelled and the call is not yet on the wire. BLOCKING
  (a refresh in this window must not put the stop back); stale after ``RELEASING_STALE_SECONDS``
  (a crash mid-write), when the refresh resolves it like any other marker.
* ``pledged``     -- the call is accepted. BLOCKING.
* ``rearming`` / ``rearm_failed`` -- the legs are being put back / the broker refused them. NOT
  blocking: a lot whose protection could not be restored must be guarded by the safety net, not
  exempted by a marker that no longer describes anything true. The refresh retries.

UNKNOWN IS NOT ZERO. A ``qty_available`` that cannot be read refuses the release (nothing is
cancelled); an option book that cannot be read keeps a suspended lot suspended.

Broker-specific on purpose: IBKR and TastyTrade have their own hold rules, which nobody has
verified for this case, and keep their behaviour unchanged (the seam's default releases nothing).
"""
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from alpaca.common.exceptions import APIError
from sqlmodel import Session, select

from ...logger import logger
from ...core.db import get_db, get_instance, update_instance
from ...core.models import TradingOrder, Transaction
from ...core.types import (
    ActivityLogSeverity, ActivityLogType, AssetClass, OptionRight, OrderDirection,
    OrderStatus, TransactionStatus,
)

PROTECTION_SUSPENDED_KEY = "protection_suspended"
REASON_PLEDGED = "pledged_to_short_call"

STAGE_RELEASING = "releasing"
STAGE_PLEDGED = "pledged"
STAGE_REARMING = "rearming"
STAGE_REARM_FAILED = "rearm_failed"
#: The stages in which nothing may sell the lot's shares.
BLOCKING_STAGES = frozenset({STAGE_RELEASING, STAGE_PLEDGED})

#: How long the call waits for the broker to report the shares available after the legs are
#: cancelled. Alpaca releases a cancelled order's hold in well under a second; this only bounds
#: the pathological case, and a timeout RE-STAGES the protection and refuses the call.
RELEASE_WAIT_SECONDS = 20.0
RELEASE_POLL_SECONDS = 0.5
#: A ``releasing`` marker older than this belongs to a writer that died; the refresh resolves it.
RELEASING_STALE_SECONDS = 300.0

_sleep = time.sleep


class CoverReleaseRefused(Exception):
    """The covering shares could not be freed; the call must not be sent. Nothing is left
    cancelled: every leg this attempt touched has been restored (or its restore is marked
    for retry) before this is raised."""


def suspension_marker(transaction) -> Optional[Dict[str, Any]]:
    """The ``protection_suspended`` marker on ``transaction``, or None."""
    meta = getattr(transaction, "meta_data", None)
    if not isinstance(meta, dict):
        return None
    marker = meta.get(PROTECTION_SUSPENDED_KEY)
    return marker if isinstance(marker, dict) else None


def is_protection_suspended(transaction) -> bool:
    """True while the lot's shares are pledged and NOTHING may sell them (blocking stages)."""
    marker = suspension_marker(transaction)
    return marker is not None and marker.get("stage") in BLOCKING_STAGES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_equity(txn) -> bool:
    return txn.asset_class != AssetClass.OPTION and txn.multiplier in (None, 1) and bool(txn.symbol)


class CoverProtectionMixin:
    """The Alpaca half of the covered-call protection suspension (see the module docstring)."""

    # ------------------------------------------------------------------ marker plumbing
    @staticmethod
    def _write_suspension_marker(txn_id: int, marker: Optional[Dict[str, Any]]) -> None:
        txn = get_instance(Transaction, txn_id)
        meta = dict(txn.meta_data or {})
        if marker is None:
            meta.pop(PROTECTION_SUSPENDED_KEY, None)
        else:
            meta[PROTECTION_SUSPENDED_KEY] = marker
        txn.meta_data = meta or None
        update_instance(txn)

    def _set_suspension_stage(self, txn_id: int, stage: str, **extra) -> None:
        marker = dict(suspension_marker(get_instance(Transaction, txn_id)) or {})
        marker.update(extra)
        marker["stage"] = stage
        self._write_suspension_marker(txn_id, marker)

    def _log_protection_activity(self, severity, description: str, txn, **data) -> None:
        try:
            from ...core.db import log_activity
            log_activity(
                severity=severity, activity_type=ActivityLogType.TP_SL_ADJUSTED,
                description=description,
                data={"transaction_id": txn.id, "symbol": txn.symbol,
                      "reason": REASON_PLEDGED, **data},
                source_account_id=self.id, source_expert_id=txn.expert_id)
        except Exception as e:  # noqa: BLE001 -- the log line above already says it
            logger.warning(f"[Account {self.id}] could not record the protection activity: {e}")

    # ------------------------------------------------------------------ broker reads
    def _broker_available_shares(self, symbol: str) -> Optional[float]:
        """``qty_available`` for ``symbol`` (shares not held for open orders): TRI-STATE.

        A float including ``0.0`` is MEASURED. ``None`` is UNKNOWN (transport failure, or a
        position row without the field) and the caller must refuse -- it is not "nothing is
        available" and it is not "everything is". A 404 is the broker saying the account holds
        none of the symbol: a measured 0.
        """
        try:
            position = self.client.get_open_position(symbol)
        except APIError as e:
            if e.status_code == 404:
                return 0.0
            logger.error(f"[Account {self.id}] get_open_position({symbol}) failed: {e}")
            return None
        except Exception as e:  # noqa: BLE001 -- unknown, reported, never read as zero
            logger.error(f"[Account {self.id}] get_open_position({symbol}) failed: {e}")
            return None
        raw = getattr(position, "qty_available", None)
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.error(f"[Account {self.id}] position {symbol} publishes no usable "
                         f"qty_available ({raw!r}); the shares available for a call are UNKNOWN")
            return None

    # ------------------------------------------------------------------ book reads
    def _open_equity_transactions(self, symbol: Optional[str] = None) -> List[Transaction]:
        """OPENED equity transactions of THIS account (transactions link to an account only
        through their orders), as detached rows, oldest first."""
        with Session(get_db().bind) as session:
            query = select(Transaction).join(TradingOrder).where(
                TradingOrder.account_id == self.id,
                Transaction.status == TransactionStatus.OPENED)
            if symbol is not None:
                query = query.where(Transaction.symbol == symbol)
            rows = [t for t in session.exec(query.distinct()).all() if _is_equity(t)]
            for t in rows:
                session.expunge(t)
        return sorted(rows, key=lambda t: t.id)

    @staticmethod
    def _working_protective_legs(txn_id: int) -> List[TradingOrder]:
        """The transaction's resting protection that still holds shares or could be sent:
        top-level SELL legs (an OCO is ONE order; cancelling it takes its children), not
        terminal, not filled."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.trade_store import orders_where
        dead = OrderStatus.get_terminal_statuses() | {OrderStatus.FILLED}
        return [o for o in orders_where(transaction_id=txn_id)
                if o.parent_order_id is None and o.side == OrderDirection.SELL
                and o.status not in dead and TransactionHelper.is_resting_protection(o)]

    @staticmethod
    def _leg_hold(leg: TradingOrder) -> float:
        if leg.status in OrderStatus.get_unsent_statuses():
            return 0.0                       # never reached the broker: holds nothing
        return max(0.0, float(leg.quantity or 0.0) - float(leg.filled_qty or 0.0))

    # ------------------------------------------------------------------ the seam
    def _release_cover_protection_for_short_calls(self, legs, quantity, option_strategy):
        """``OptionsAccountInterface``'s seam for a ``covered_call``: free the shares it pledges.

        Returns a token (``confirm(order)`` / ``abort(why)``) when protection was suspended,
        None when there was nothing to release. RAISES ``CoverReleaseRefused`` (after restoring
        whatever it touched) when the shares cannot be freed -- the call must not be sent.
        """
        from ...core.interfaces.OptionsAccountInterface import (
            COVERED_CALL_STRATEGY, DEFAULT_OPTION_MULTIPLIER)

        if (option_strategy or "").strip().lower() != COVERED_CALL_STRATEGY:
            return None
        needs: Dict[str, int] = {}
        for leg in legs:
            if leg.side != OrderDirection.SELL or leg.option_type != OptionRight.CALL:
                continue
            underlying = (leg.underlying or "").strip().upper()
            if not underlying:
                continue
            ratio = float(getattr(leg, "ratio_qty", 1) or 1)
            shares = int(math.ceil(round(
                float(quantity) * ratio * DEFAULT_OPTION_MULTIPLIER, 6)))
            needs[underlying] = needs.get(underlying, 0) + shares
        suspended: List[int] = []
        try:
            for underlying in sorted(needs):
                suspended += self._release_shares_for(underlying, needs[underlying])
        except BaseException:
            # Roll back what an earlier underlying already did; the failing one rolled itself back.
            if suspended:
                self._rearm_now(suspended, "the call could not be written")
            raise
        if not suspended:
            return None
        return CoverProtectionRelease(self, suspended)

    def _release_shares_for(self, underlying: str, need: int) -> List[int]:
        candidates = []
        for txn in self._open_equity_transactions(underlying):
            if txn.side != OrderDirection.BUY:
                continue
            legs = self._working_protective_legs(txn.id)
            if legs:
                candidates.append((txn, legs, max(self._leg_hold(l) for l in legs)))
        if not candidates:
            return []                        # nothing of ours is holding shares: as before

        available = self._broker_available_shares(underlying)
        if available is None:
            raise CoverReleaseRefused(
                f"covered call on {underlying} NOT SENT: how many shares are available "
                f"(qty_available) could not be read, so whether the platform's own resting "
                f"stop is what blocks the call is unknown. Nothing was cancelled.")
        if available >= need:
            return []                        # the shares are free already

        chosen, freed = [], available
        for txn, legs, hold in candidates:
            chosen.append((txn, legs))
            freed += hold
            if freed >= need:
                break
        if freed < need:
            msg = (f"covered call on {underlying} NOT SENT: it needs {need} shares free but "
                   f"the broker reports {available:g} available and the platform's resting "
                   f"protection on the {len(candidates)} open lot(s) frees only "
                   f"{freed - available:g} more, so suspending it would still leave the call "
                   f"uncovered. Nothing was cancelled.")
            logger.error(f"[Account {self.id}] {msg}")
            raise CoverReleaseRefused(msg)

        lot_ids = [txn.id for txn, _ in chosen]
        try:
            for txn, legs in chosen:
                self._suspend_lot(txn, legs, underlying)
            self._wait_for_available(underlying, need)
        except BaseException as e:
            logger.error(
                f"[Account {self.id}] releasing {underlying} cover for a covered call FAILED "
                f"({e}); restoring the protection on transaction(s) {lot_ids}")
            touched = [i for i in lot_ids
                       if suspension_marker(get_instance(Transaction, i)) is not None]
            self._rearm_now(touched, str(e))
            raise
        # the cancels are confirmed by the broker's own number: bring our rows into line
        for txn_id in lot_ids:
            marker = suspension_marker(get_instance(Transaction, txn_id)) or {}
            self._resync_cancelled_legs(marker.get("cancelled_order_ids", []))
        return lot_ids

    def _suspend_lot(self, txn: Transaction, legs: List[TradingOrder], symbol: str) -> None:
        """Mark the lot FIRST (so a crash mid-way is visible and cannot leave a half-cancelled
        lot with no record), then cancel its legs."""
        self._write_suspension_marker(txn.id, {
            "reason": REASON_PLEDGED, "stage": STAGE_RELEASING, "symbol": symbol,
            "since": _now_iso(), "cancelled_order_ids": [], "call_order_id": None,
            "stop_loss": txn.stop_loss, "take_profit": txn.take_profit})
        cancelled: List[int] = []
        for leg in legs:
            if leg.status in OrderStatus.get_unsent_statuses() and not leg.broker_order_id:
                fresh = get_instance(TradingOrder, leg.id)
                fresh.status = OrderStatus.CANCELED
                fresh.comment = (f"{fresh.comment or ''} | protection suspended: shares "
                                 f"pledged to a short call")[:500]
                update_instance(fresh)
            elif leg.status == OrderStatus.PENDING_CANCEL:
                pass                         # already going; just wait for it with the rest
            elif not self.cancel_order(leg.id):
                self._set_suspension_stage(txn.id, STAGE_RELEASING, cancelled_order_ids=cancelled)
                raise CoverReleaseRefused(
                    f"the broker did not accept the cancel of protective order {leg.id} "
                    f"({symbol} transaction {txn.id}), so its shares cannot be freed")
            cancelled.append(leg.id)
            self._set_suspension_stage(txn.id, STAGE_RELEASING, cancelled_order_ids=list(cancelled))
        logger.warning(
            f"[Account {self.id}] {symbol} transaction {txn.id}: protective order(s) "
            f"{cancelled} cancelled to free shares for a covered call. The protection is "
            f"SUSPENDED (stop_loss={txn.stop_loss}, take_profit={txn.take_profit} stay stored) "
            f"until the call is gone.")
        self._log_protection_activity(
            ActivityLogSeverity.WARNING,
            f"Protection on {symbol} suspended for a covered call (cancelled orders {cancelled})",
            txn, cancelled_order_ids=cancelled)

    def _wait_for_available(self, symbol: str, need: int) -> None:
        deadline = time.monotonic() + RELEASE_WAIT_SECONDS
        last = None
        while True:
            last = self._broker_available_shares(symbol)
            if last is not None and last >= need:
                return
            if time.monotonic() >= deadline:
                raise CoverReleaseRefused(
                    f"covered call on {symbol} NOT SENT: after cancelling the protective "
                    f"order(s) the broker still reports {last if last is not None else 'an unreadable'}"
                    f" shares available, {need} needed, after {RELEASE_WAIT_SECONDS:g}s")
            _sleep(RELEASE_POLL_SECONDS)

    # ------------------------------------------------------------------ restore / re-arm
    def _resync_cancelled_legs(self, order_ids: List[int]) -> None:
        """Bring the rows of legs we cancelled into line with the BROKER's answer, by id.

        The cancel only marks them PENDING_CANCEL. Whatever the broker says is adopted: CANCELED
        when the cancel landed, the live status when it did not (the stop is then still working
        and needs no re-staging). Never invented, never optimistic.
        """
        for order_id in order_ids:
            try:
                row = get_instance(TradingOrder, order_id)
                if row.status != OrderStatus.PENDING_CANCEL or not row.broker_order_id:
                    continue
                broker_order, absent = self._get_broker_order_or_absent(row.broker_order_id)
                if absent or broker_order is None:
                    logger.error(f"[Account {self.id}] broker does not know order "
                                 f"{row.broker_order_id} (row {order_id}); left PENDING_CANCEL")
                    continue
                self._write_pending_cancel_result(order_id, broker_order.status, broker_order)
            except Exception as e:  # noqa: BLE001 -- the refresh's own PENDING_CANCEL pass retries
                logger.error(f"[Account {self.id}] could not resolve cancelled order {order_id} "
                             f"against the broker: {e}", exc_info=True)

    def _has_working_protection(self, txn_id: int) -> bool:
        dead = {OrderStatus.PENDING_CANCEL}
        return any(o.status not in dead for o in self._working_protective_legs(txn_id))

    def _rearm_transaction(self, txn_id: int, why: str) -> bool:
        """Put the stored stop / take-profit back under a lot. True when the lot is protected
        again (marker cleared); False when it is not (marker ``rearm_failed``, error logged and
        in the activity log -- the refresh retries)."""
        txn = get_instance(Transaction, txn_id)
        marker = suspension_marker(txn) or {}
        if txn.status != TransactionStatus.OPENED:
            self._write_suspension_marker(txn_id, None)
            return True                      # nothing left to protect
        self._set_suspension_stage(txn_id, STAGE_REARMING)
        self._resync_cancelled_legs(marker.get("cancelled_order_ids", []))
        txn = get_instance(Transaction, txn_id)
        has_levels = bool(txn.stop_loss) or bool(txn.take_profit)
        ok = True
        if has_levels:
            ok = bool(self._adjust_tpsl_internal(
                txn, new_tp_price=None, new_sl_price=None,
                source="short_call_cover_released"))
            # adjust skips as "already exists" while a cancelled leg is still PENDING_CANCEL: a
            # skip is not protection.
            ok = ok and self._has_working_protection(txn_id)
        if ok:
            self._write_suspension_marker(txn_id, None)
            logger.warning(
                f"[Account {self.id}] {txn.symbol} transaction {txn_id}: protection RE-ARMED "
                f"at stop_loss={txn.stop_loss}, take_profit={txn.take_profit} ({why})")
            self._log_protection_activity(
                ActivityLogSeverity.SUCCESS,
                f"Protection on {txn.symbol} re-armed ({why})", txn)
            return True
        self._set_suspension_stage(txn_id, STAGE_REARM_FAILED, last_error=why,
                                   failed_at=_now_iso())
        msg = (f"Re-arming the protection on {txn.symbol} transaction {txn_id} FAILED ({why}): "
               f"the shares have NO resting stop. Retried on the next account refresh.")
        logger.error(f"[Account {self.id}] {msg}")
        self._log_protection_activity(ActivityLogSeverity.FAILURE, msg, txn)
        return False

    def _rearm_now(self, txn_ids: List[int], why: str) -> None:
        """Restore after a release that failed BEFORE any call was written."""
        for txn_id in txn_ids:
            try:
                self._rearm_transaction(txn_id, why)
            except Exception as e:  # noqa: BLE001 -- one lot's failure must not strand the rest
                logger.error(f"[Account {self.id}] re-arming transaction {txn_id} failed: {e}",
                             exc_info=True)
                try:
                    self._set_suspension_stage(txn_id, STAGE_REARM_FAILED, last_error=str(e))
                except Exception:  # noqa: BLE001
                    logger.error(f"[Account {self.id}] could not even mark transaction "
                                 f"{txn_id} rearm_failed", exc_info=True)

    # ------------------------------------------------------------------ the periodic re-arm
    def reconcile_suspended_protection(self, only_txn_ids: Optional[List[int]] = None,
                                       include_fresh_releasing: bool = False) -> int:
        """Re-stage the protection of every suspended lot whose call is gone. Returns how many
        lots were re-armed.

        Called from the account refresh (and by a failed call's abort). Per symbol, the
        account-wide pledge (``shares_pledged_to_short_calls``, tri-state) decides how many of
        the suspended lots must STAY suspended: lots are kept oldest first until their size
        covers the pledge, the rest are re-armed. UNKNOWN pledge keeps everything suspended.
        A lot already being written (fresh ``releasing``) is left to its writer.
        """
        marked: Dict[str, List[Transaction]] = {}
        for txn in self._open_equity_transactions():
            if suspension_marker(txn) is None:
                continue
            if only_txn_ids is not None and txn.id not in only_txn_ids:
                continue
            marked.setdefault(txn.symbol, []).append(txn)

        rearmed = 0
        for symbol, txns in sorted(marked.items()):
            try:
                pledged = self.shares_pledged_to_short_calls(symbol)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[Account {self.id}] could not read the {symbol} pledge: {e}",
                             exc_info=True)
                pledged = None
            if pledged is None:
                logger.warning(
                    f"[Account {self.id}] {symbol}: how many shares are pledged to short calls "
                    f"is UNKNOWN, so the suspended protection on transaction(s) "
                    f"{[t.id for t in txns]} stays suspended (unknown is not 'the call is gone')")
                continue
            pledged = self.pledged_shares_in_equity_units(symbol, pledged)
            kept = 0.0
            for txn in txns:
                marker = suspension_marker(txn)
                open_qty = abs(txn.get_current_open_qty())
                if marker.get("stage") == STAGE_RELEASING and not include_fresh_releasing \
                        and self._releasing_is_fresh(marker):
                    kept += open_qty
                    logger.info(f"[Account {self.id}] {symbol} transaction {txn.id}: a covered "
                                f"call is being written against it; leaving it to the writer")
                    continue
                if kept < pledged:
                    kept += open_qty
                    logger.info(
                        f"[Account {self.id}] {symbol} transaction {txn.id}: protection stays "
                        f"SUSPENDED -- {pledged} {symbol} share(s) are pledged to open short "
                        f"call(s) (stop_loss={txn.stop_loss}, take_profit={txn.take_profit} "
                        f"are stored and not enforced while the call is open)")
                    if marker.get("stage") != STAGE_PLEDGED:
                        self._set_suspension_stage(txn.id, STAGE_PLEDGED)
                    continue
                if self._rearm_transaction(txn.id, "the short call that pledged these shares "
                                                    "is gone"):
                    rearmed += 1
        return rearmed

    @staticmethod
    def _releasing_is_fresh(marker: Dict[str, Any]) -> bool:
        try:
            since = datetime.fromisoformat(marker["since"])
        except (KeyError, TypeError, ValueError):
            return False                     # unreadable stamp: not fresh, so it is resolved
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - since).total_seconds() < RELEASING_STALE_SECONDS

    # ------------------------------------------------------------------ the net at the send
    def _protective_leg_blocked_by_suspension(self, trading_order: TradingOrder) -> bool:
        """True (and the leg is marked ERROR with the reason) when ``trading_order`` is a
        protective SELL on a lot whose shares are pledged. The net under EVERY path that writes
        a leg -- the waiting-trigger sweep, the PENDING dependents, the inline creators."""
        if trading_order.transaction_id is None or trading_order.side != OrderDirection.SELL:
            return False
        from ba2_common.core.TransactionHelper import TransactionHelper
        if not TransactionHelper.is_resting_protection(trading_order):
            return False
        try:
            txn = get_instance(Transaction, trading_order.transaction_id)
        except Exception:  # noqa: BLE001 -- not our lot; the normal path reports it
            return False
        if not is_protection_suspended(txn):
            return False
        note = ("[protection_suspended] not sent: the shares are pledged to an open short call "
                "(the stored stop/take-profit are re-staged when it is gone)")
        logger.warning(
            f"[Account {self.id}] protective order {trading_order.id} for {txn.symbol} "
            f"transaction {txn.id} NOT SENT -- its shares are pledged to a short call.")
        if trading_order.id is not None:
            fresh = get_instance(TradingOrder, trading_order.id)
            fresh.status = OrderStatus.ERROR
            fresh.comment = f"{fresh.comment} | {note}"[:500] if fresh.comment else note
            update_instance(fresh)
        return True


class CoverProtectionRelease:
    """The token ``submit_option_order`` holds between releasing the shares and the call going out."""

    def __init__(self, account: CoverProtectionMixin, txn_ids: List[int]):
        self._account = account
        self._txn_ids = list(txn_ids)

    def confirm(self, order) -> None:
        """The call was accepted: the lots are pledged until it is gone.

        A call the broker hands back already dead (rejected / cancelled / expired) was NOT
        accepted: the protection goes back now instead of waiting for the next refresh."""
        status = getattr(order, "status", None)
        if status in OrderStatus.get_terminal_statuses():
            self.abort(f"the broker returned the call already {getattr(status, 'value', status)}")
            return
        for txn_id in self._txn_ids:
            self._account._set_suspension_stage(
                txn_id, STAGE_PLEDGED, call_order_id=getattr(order, "id", None))

    def abort(self, why: str) -> None:
        """The call was not submitted or not accepted: put the protection back NOW, unless the
        call turns out to be live (then it stays suspended and the refresh decides)."""
        self._account.reconcile_suspended_protection(
            only_txn_ids=self._txn_ids, include_fresh_releasing=True)
