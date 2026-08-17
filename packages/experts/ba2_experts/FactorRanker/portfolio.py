"""Rebalance math and execution for FactorRanker.

``rebalance_deltas`` is pure (target weights + holdings + prices -> signed share
deltas) and unit tested directly. ``FactorPortfolioManager`` (added later) wraps it
with DB/account access to actually submit the orders.
"""

import math
from collections import namedtuple
from datetime import datetime, timezone
from typing import Dict, List, Optional


from ba2_common.core.db import add_instance, get_instance, update_instance
from ba2_common.core.models import ExpertInstance, TradingOrder, Transaction
from ba2_common.core.types import (
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from ba2_common.logger import logger

# Lightweight per-OPENED-transaction record carried in get_holdings()'s ``by_symbol``: just the
# fields the rebalance / stop-loss / sell-submit paths need (the transaction id for closing-order
# attribution, plus the recorded entry price and net filled qty for cost-basis). Replacing raw
# ``Transaction`` ORM objects with this lets the qty (``open_qty``, = get_current_open_qty) be
# computed ONCE when the account builds its snapshot instead of via a per-bar DB query.
_OpenedTxn = namedtuple("_OpenedTxn", ["id", "open_price", "open_qty"])


def rebalance_deltas(target_weights: Dict[str, float], held_shares: Dict[str, float],
                     prices: Dict[str, float], equity: float) -> Dict[str, float]:
    """Signed whole-share deltas to move from current holdings to target weights.

    target_shares = floor(weight * equity / price); delta = target - held. Names
    held but absent from the target weight 0 (sold down). A held name we must exit
    but cannot price is still fully sold using its held quantity. Zero deltas are
    omitted from the result.
    """
    deltas: Dict[str, float] = {}
    # Iterate in a STABLE (sorted) order. A plain ``set`` union iterates in a
    # process-dependent order (PYTHONHASHSEED), which makes the ORDER deltas are
    # yielded — and therefore the order the engine submits/fills the orders and
    # first inserts each symbol into its position ledger — non-deterministic.
    # The equity mark-to-market then sums ``qty * price`` over that ledger in a
    # different float order each run, producing ~1e-11 equity-curve jitter that
    # flips which GA individual wins. Sorting the symbols makes the deltas dict
    # (and the downstream fill/ledger/MTM-sum order) byte-identical across runs.
    symbols = sorted(set(target_weights) | set(held_shares))
    for s in symbols:
        price = prices.get(s)
        if price is None or price <= 0:
            # Can't price a held name we must exit -> still allow full sell using held qty
            if s in held_shares and target_weights.get(s, 0.0) == 0.0:
                deltas[s] = -float(held_shares[s])
            continue
        target_shares = math.floor((target_weights.get(s, 0.0) * equity) / price)
        delta = target_shares - float(held_shares.get(s, 0.0))
        if delta != 0.0:
            deltas[s] = float(delta)
    return deltas


def stop_loss_sells(positions: Dict[str, tuple], prices: Dict[str, float],
                    equity: float, risk_pct: float) -> Dict[str, int]:
    """Per-name EQUITY-loss stop: full-exit quantities for held names whose unrealized
    loss has reached risk_pct% of total equity. Pure (no IO).

    positions[symbol] = (avg_entry_cost, held_qty). Long-only (FactorRanker holds long
    weights). A name is stopped when  held_qty * (avg_entry_cost - price) >= equity * risk_pct/100
    (i.e. the dollar loss has reached the equity cap). Names missing a price, or with
    non-positive cost/price/qty, or trading at/above entry, are skipped. risk_pct <= 0 or
    equity <= 0 -> no stops ({}). Returns {symbol: int(held_qty)} for stopped names only.
    """
    if risk_pct is None or risk_pct <= 0 or equity is None or equity <= 0:
        return {}
    cap = equity * risk_pct / 100.0
    sells: Dict[str, int] = {}
    for symbol, pos in positions.items():
        avg_cost, held_qty = pos
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        if avg_cost is None or avg_cost <= 0 or held_qty is None or held_qty <= 0:
            continue
        loss = held_qty * (avg_cost - price)
        # Only a real loss can breach the cap (>= 0 < cap when trading at/above entry).
        if loss >= cap:
            sells[symbol] = int(held_qty)
    return sells


class FactorPortfolioManager:
    """Diffs FactorRanker target weights against current holdings and submits the
    buy/sell deltas directly (no ExpertRecommendation, no SmartRiskManager).

    Expert attribution flows through ``Transaction.expert_id``: new positions
    pre-create a Transaction stamped with this expert's id (the same path the
    SmartRiskManager uses), so the buys are recognised as this expert's holdings
    on the next rebalance.
    """

    def __init__(self, expert_instance_id: int):
        from ba2_common.core.instance_resolver import get_instance_resolver
        resolver = get_instance_resolver()
        self.expert_instance_id = expert_instance_id
        self.expert = resolver.get_expert_instance(expert_instance_id)
        instance = get_instance(ExpertInstance, expert_instance_id)
        self.account_id = instance.account_id
        self.account = resolver.get_account_instance(instance.account_id)

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------

    def get_holdings(self):
        """Return (held_shares {symbol: qty}, transactions_by_symbol {symbol: [_OpenedTxn]})
        for this expert's OPENED (and, on live, WAITING-but-actually-filled) transactions.

        The OPENED/WAITING-Transaction set is expert-scoped (``Transaction.expert_id``) and is the
        source of truth for WHICH symbols/txns belong to THIS expert (needed to attach a sell to a
        txn id in ``_submit_sell``) and for each txn's cost basis (``open_price``/``open_qty``).

        On a BACKTEST account we read it from the account's cached, fill-invalidated
        ``opened_position_snapshot`` (GENERAL account infra) instead of issuing the OPENED ``SELECT``
        + a per-transaction ``get_current_open_qty()`` DB query on EVERY bar — the OPENED set only
        changes when an order fills, so the cache is rebuilt per fill, not per bar. The QTY NUMBERS
        in ``held`` still come from the in-memory ledger (``self.account._positions``); for a
        long-only book the ledger's signed per-symbol qty equals the filled-order signed sum, but
        without the per-bar round-trips. We do NOT enumerate ``_positions`` blindly (it is
        account-wide, not expert-scoped); we only read it for the expert-owned symbols. Live
        accounts (no snapshot / no ledger) fall back to the direct DB path.
        """
        # NOTE ON THE TWO BRANCHES: this is a CACHE choice, not a storage choice — both paths
        # answer "which transactions does this expert hold". Storage is the repository's
        # business (see ba2_common.core.trade_repository); the snapshot exists purely because
        # re-deriving the book per bar is expensive, and it is invalidated per fill rather than
        # per bar. Collapsing to the repository alone would be correct but pays that per-bar
        # cost back, so the fast path stays deliberate.
        snapshot_fn = getattr(self.account, "opened_position_snapshot", None)
        if snapshot_fn is not None:
            # Backtest: cached, fill-invalidated OPENED snapshot from the account (no per-bar DB).
            by_symbol: Dict[str, list] = {
                sym: [_OpenedTxn(tid, open_price, open_qty)
                      for (tid, open_price, open_qty) in recs]
                for sym, recs in snapshot_fn(self.expert_instance_id).items()
            }
        else:
            # Live: no account snapshot -> direct DB query (build the same lightweight records;
            # get_current_open_qty is computed once here, exactly as the cost-basis loop needs it).
            # Include WAITING alongside OPENED: a transaction stays WAITING until the account's
            # refresh_transactions() cycle promotes it, which can lag well behind the order actually
            # filling at the broker (refresh_orders() and refresh_transactions() are separate calls
            # on separate cadences). get_current_open_qty() only counts orders whose OWN status is
            # already FILLED, so this is safe either way — a still-pending WAITING transaction
            # contributes 0. Without this, re-triggering FactorRanker between an order filling and
            # its transaction being promoted makes get_holdings() see an empty book and re-buy the
            # full target from scratch, stacking duplicate positions (see 2026-07-14 incident where
            # 3 rapid re-triggers 3x'd a live position before this fix).
            from ba2_common.core.trade_repository import get_trade_repository
            transactions = get_trade_repository().open_transactions(
                expert_id=self.expert_instance_id, include_waiting=True)
            by_symbol = {}
            for trans in transactions:
                by_symbol.setdefault(trans.symbol, []).append(
                    _OpenedTxn(trans.id, trans.open_price, trans.get_current_open_qty())
                )

        ledger = getattr(self.account, "_positions", None)
        held: Dict[str, float] = {}
        if ledger is not None:
            # Backtest: read signed qty for each expert-owned symbol from the in-memory ledger.
            for symbol in by_symbol:
                pos = ledger.get(symbol)
                qty = pos.qty if pos is not None else 0.0
                if qty == 0:
                    continue
                # The ledger qty accrues via repeated += of filled quantities, so its float
                # bit-pattern can drift ~1e-11 from the freshly-summed DB value even though the
                # share count is identical. FactorRanker is a WHOLE-SHARE long-only book (deltas
                # are math.floor'd), so snap to the exact integer when within tolerance: this
                # makes the qty byte-identical to the DB path (get_current_open_qty's filled-qty
                # sum) and removes the sub-nanodollar equity-curve jitter. A genuinely fractional
                # qty (defensive — should not occur here) passes through unchanged.
                rounded = round(qty)
                if abs(qty - rounded) < 1e-6:
                    qty = float(rounded)
                held[symbol] = qty
        else:
            # Live: no in-memory ledger -> sum each txn's filled qty (precomputed in open_qty).
            for symbol, transactions_for_symbol in by_symbol.items():
                qty = 0.0
                for trans in transactions_for_symbol:
                    qty += trans.open_qty
                if qty == 0:
                    continue
                held[symbol] = qty

        # Drop symbols that net to zero so by_symbol only carries actually-held names.
        by_symbol = {s: txns for s, txns in by_symbol.items() if s in held}
        return held, by_symbol

    # ------------------------------------------------------------------
    # Rebalance
    # ------------------------------------------------------------------

    def rebalance(self, target_weights: Dict[str, float], equity: Optional[float] = None) -> List[TradingOrder]:
        """Submit the buy/sell orders needed to move current holdings to the targets.

        Returns the list of submitted orders.
        """
        held, by_symbol = self.get_holdings()
        # Sorted (not a raw set) so any iteration over ``symbols`` is deterministic;
        # ``rebalance_deltas`` also sorts internally, but keeping this stable too
        # avoids re-introducing process-dependent ordering if this set is reused.
        symbols = sorted(set(target_weights) | set(held))
        # ONE unpriceable symbol must not kill the whole rebalance. The backtest account RAISES
        # ("No backtest price for GIBO at ...") rather than returning None for a symbol with no
        # bar on this date, so a bare dict comprehension propagated that out of rebalance() and
        # the engine logged "bypass rebalance failed" and skipped the ENTIRE basket. Measured
        # 2026-08-06: GIBO had no price on any Monday, so every weekly rebalance of the large
        # universe aborted and FactorRanker produced ZERO trades over the whole window.
        #
        # ``rebalance_deltas`` already handles a None price correctly (it skips an unpriceable
        # BUY target and still fully exits an unpriceable HELD name using its held quantity), so
        # degrading to None here is exactly what the downstream logic already expects.
        prices: Dict[str, Optional[float]] = {}
        unpriced: List[str] = []
        for s in symbols:
            try:
                prices[s] = self.account.get_instrument_current_price(s)
            except Exception as e:  # noqa: BLE001 — one bad symbol disables itself, never the basket
                prices[s] = None
                unpriced.append(s)
                logger.debug(f"FactorRanker: no price for {s} this bar ({e}); excluded from rebalance")
        if unpriced:
            logger.warning(
                f"FactorRanker: {len(unpriced)}/{len(symbols)} symbol(s) unpriceable this "
                f"rebalance and skipped: {', '.join(unpriced[:10])}"
                f"{' ...' if len(unpriced) > 10 else ''}"
            )

        if equity is None:
            equity = self.expert.get_virtual_balance()
        if equity is None:
            raise ValueError("FactorRanker: virtual balance (equity) not available for rebalance")

        deltas = rebalance_deltas(target_weights, held, prices, equity)

        submitted: List[TradingOrder] = []
        for sym, delta in deltas.items():
            order = self._submit_delta(sym, int(delta), by_symbol.get(sym, []))
            if order is not None:
                submitted.append(order)

        # The stop price is a function of avg entry cost, held qty AND equity — a rebalance moves
        # all three, so a stop priced at the original entry goes stale the moment a name is added
        # to or trimmed. Re-price the survivors here, the same way every other expert's stop is
        # maintained (adjust_sl), so the resting order keeps encoding the CURRENT rule.
        self._resync_protective_stops(by_symbol, changed={s for s, d in deltas.items() if d})
        logger.info(
            f"FactorRanker[{self.expert_instance_id}]: rebalance submitted {len(submitted)} orders "
            f"(equity={equity:.2f}, deltas={deltas})"
        )
        return submitted

    # ------------------------------------------------------------------
    # Per-name EQUITY-loss stop (reuses risk_per_trade_pct)
    # ------------------------------------------------------------------

    # apply_stop_losses() REMOVED 2026-08-06.
    #
    # It priced every held name and submitted market sells -- i.e. it evaluated a stop against a
    # price and issued the exit itself. That is execution, which belongs to the account/broker,
    # not to expert code. It also had NO live caller and never had one, so live FactorRanker ran
    # with no downside protection at all: 22 open positions across all 6 instances.
    #
    # The stop is now a RESTING ORDER priced by protective_stop_price() below (the same
    # inequality stop_loss_sells encodes, solved for price). Alpaca enforces it live;
    # BacktestAccount.refresh_orders fills it per bar in simulation. One mechanism, both sides.
    #
    # Leaving this method AND the resting order in place would DOUBLE-EXIT in backtest: the stop
    # order fills, and this would submit a second market sell on the same bar.
    #
    # stop_loss_sells() is deliberately KEPT: it is pure (no IO, no orders) and is the canonical
    # statement of the rule that protective_stop_price inverts.

    def _resync_protective_stops(self, by_symbol: Dict[str, list], changed: set) -> None:
        """Re-price the resting stop of every still-held name whose position just changed.

        Best-effort and never raises: a stop that could not be amended is worse than one that
        could, but far better than aborting a rebalance that has already submitted orders.
        """
        for symbol in sorted(changed):
            transactions = by_symbol.get(symbol) or []
            if not transactions:
                continue  # fully exited — its protective leg is closed with the transaction
            sl = self.protective_stop_price(symbol, transactions)
            if sl is None:
                continue
            for trans in transactions:
                try:
                    self.account.adjust_sl(trans, sl, source="factorranker_rebalance")
                except NotImplementedError:
                    return  # broker cannot amend stops — nothing to retry per symbol
                except Exception as e:  # noqa: BLE001 — one bad amend must not void the rebalance
                    logger.warning(
                        f"FactorRanker[{self.expert_instance_id}]: could not re-price the "
                        f"protective stop for {symbol} to {sl:.4f}: {e}")

    def protective_stop_price(self, symbol: str, transactions: list,
                              extra_qty: float = 0.0, extra_price: Optional[float] = None
                              ) -> Optional[float]:
        """The RESTING stop price that encodes this expert's per-name equity-loss rule.

        ``stop_loss_sells`` stops a name when its dollar loss reaches the equity budget:

            held_qty * (avg_entry_cost - price) >= equity * risk_pct/100

        which is a straight inequality in ``price``, so it collapses to one number:

            stop_price = avg_entry_cost - (equity * risk_pct / 100) / held_qty

        Placing THAT as a real stop order is not an approximation of the rule -- it is the same
        rule, enforced by whoever owns the order book.

        WHY A RESTING ORDER AND NOT A PER-BAR CHECK (2026-08-06)
        -------------------------------------------------------
        The backtest evaluates the stop on every bar because a simulator has no exchange; that
        per-bar pass IS the broker stand-in, not a cadence the live side should copy. Live, the
        exchange already does it -- continuously, and while our platform is down. Porting the
        loop into production instead of placing an order is how FactorRanker ended up as the ONLY
        expert with no broker-side protection: 22 open positions across all 6 live instances with
        no stop of any kind, because ``apply_stop_losses`` has no live caller and never had one.

        With a real order, ``BacktestAccount.refresh_orders`` fills it per bar (it already fills
        stop_price orders) and Alpaca fills it live -- one mechanism, no divergence to maintain.

        ``extra_qty``/``extra_price`` fold in a buy that has not yet been persisted, so the stop
        priced at entry reflects the position that is about to exist. Returns None when the rule
        is off (no risk_pct), the inputs are unavailable, or the maths yields a non-positive
        price -- a stop must never be invented from missing data.
        """
        try:
            risk_pct = float(self.expert.get_setting_with_interface_default("risk_per_trade_pct") or 0.0)
        except Exception:  # noqa: BLE001 — a stub expert -> no stop
            return None
        if risk_pct <= 0:
            return None

        equity = self.expert.get_virtual_balance()
        if not equity or equity <= 0:
            logger.warning(f"FactorRanker[{self.expert_instance_id}]: no virtual balance; "
                           f"cannot price a protective stop for {symbol}")
            return None

        # Same quantity-weighted avg-entry-COST basis apply_stop_losses uses (per-transaction
        # open_price, NOT the ledger avg) so the resting price and the simulated rule agree.
        total_qty, cost_qty = 0.0, 0.0
        for trans in (transactions or []):
            qty, open_price = trans.open_qty, trans.open_price
            if qty is None or qty <= 0 or open_price is None or open_price <= 0:
                continue
            total_qty += qty
            cost_qty += open_price * qty
        if extra_qty > 0 and extra_price and extra_price > 0:
            total_qty += extra_qty
            cost_qty += extra_price * extra_qty
        if total_qty <= 0 or cost_qty <= 0:
            return None

        avg_cost = cost_qty / total_qty
        stop = avg_cost - (equity * risk_pct / 100.0) / total_qty
        return stop if stop > 0 else None

    def _submit_delta(self, symbol: str, delta: int, transactions: list) -> Optional[TradingOrder]:
        if delta > 0:
            return self._submit_buy(symbol, delta, transactions)
        if delta < 0:
            return self._submit_sell(symbol, -delta, transactions)
        return None

    def _submit_buy(self, symbol: str, qty: int, transactions: list) -> Optional[TradingOrder]:
        if qty <= 0:
            return None
        entry_price = self.account.get_instrument_current_price(symbol)
        if transactions:
            # Adding to an existing position — link to its OPENED transaction.
            transaction_id = transactions[0].id
        else:
            # New position — pre-create an expert-attributed transaction so the
            # holding is recognised on the next rebalance (attribution path 1).
            trans = Transaction(
                symbol=symbol, quantity=qty, side=OrderDirection.BUY,
                status=TransactionStatus.WAITING, open_price=entry_price,
                open_date=datetime.now(timezone.utc), expert_id=self.expert_instance_id,
            )
            transaction_id = add_instance(trans)

        order = TradingOrder(
            account_id=self.account_id, symbol=symbol, quantity=qty,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            transaction_id=transaction_id, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment="FactorRanker rebalance buy",
            # Deliberate rebalance sizing — never let transaction qty-sync resize it.
            data={"fixed_quantity": True},
        )
        # Attach the per-name equity-loss stop as a REAL protective order, via the same
        # submit_order(sl_price=...) -> adjust_sl path every other expert uses. The broker (or
        # BacktestAccount.refresh_orders, which fills stop_price orders per bar) then enforces
        # it; nothing has to poll. Passing an sl_price also lets the wash-trade gate form an OTO
        # when the symbol is blocked, instead of locking the entry.
        sl_price = self.protective_stop_price(
            symbol, transactions, extra_qty=qty, extra_price=entry_price)
        if sl_price is None:
            logger.warning(
                f"FactorRanker[{self.expert_instance_id}]: no protective stop priced for "
                f"{symbol} x{qty} — position will be UNPROTECTED at the broker"
            )
        return self.account.submit_order(order, sl_price=sl_price, is_closing_order=False)

    # Working legs that RESERVE shares at the broker. A resting protective order sits on the
    # SAME side as the exit (long -> SELL_STOP/SELL_LIMIT), so the wash-trade lock, which looks
    # for an OPPOSING working order, cannot see it. Both sides are listed because a short
    # position's protective legs are BUYs.
    _PROTECTIVE_ORDER_TYPES = (
        OrderType.SELL_STOP, OrderType.SELL_LIMIT, OrderType.SELL_STOP_LIMIT,
        OrderType.BUY_STOP, OrderType.BUY_LIMIT, OrderType.BUY_STOP_LIMIT,
    )

    def _working_protective_legs(self, transactions: list) -> list:
        """Protective TP/SL legs for these transactions that are still live at the broker."""
        from sqlmodel import select
        from ba2_common.core.db import get_db

        txn_ids = [t.id for t in (transactions or []) if getattr(t, "id", None) is not None]
        if not txn_ids:
            return []
        working = OrderStatus.get_unfilled_statuses() | {OrderStatus.PARTIALLY_FILLED}
        try:
            with get_db() as session:
                return list(session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == self.account_id,
                        TradingOrder.transaction_id.in_(txn_ids),
                        TradingOrder.order_type.in_(self._PROTECTIVE_ORDER_TYPES),
                        TradingOrder.status.in_(working),
                    )
                ).all())
        except Exception as e:  # noqa: BLE001 — never block an exit on a lookup failure
            logger.error(
                f"FactorRanker[{self.expert_instance_id}]: could not read protective legs for "
                f"transactions {txn_ids}: {e}", exc_info=True)
            return []

    def _release_protective_legs(self, legs: list) -> list:
        """Cancel resting protective legs so their reserved shares become sellable."""
        released = []
        for leg in legs:
            try:
                if leg.broker_order_id:
                    self.account.cancel_order(leg.broker_order_id)
                leg.status = OrderStatus.CANCELED
                update_instance(leg)
                released.append(leg)
                logger.info(
                    f"FactorRanker[{self.expert_instance_id}]: released protective "
                    f"{leg.order_type} order {leg.id} ({leg.symbol} x{leg.quantity} @ "
                    f"{leg.stop_price or leg.limit_price}) to free shares for the exit")
            except Exception as e:  # noqa: BLE001 — one stuck leg must not block the whole exit
                logger.error(
                    f"FactorRanker[{self.expert_instance_id}]: failed to cancel protective order "
                    f"{leg.id} for {leg.symbol}: {e}", exc_info=True)
        return released

    def _submit_sell(self, symbol: str, qty: int, transactions: list) -> Optional[TradingOrder]:
        if qty <= 0 or not transactions:
            return None

        # RELEASE THE PROTECTIVE LEG BEFORE SELLING.
        #
        # ``_submit_buy`` deliberately attaches a stop covering the FULL position. At the broker a
        # working stop RESERVES those shares, so a later exit for the same quantity has nothing
        # available and is rejected outright:
        #
        #   order 630 (dev, 2026-08-17): SELL 11 SPCX -> 40310000 insufficient_qty
        #   {"available":"0","existing_qty":"11","held_for_orders":"11",
        #    "related_orders":["6ba609..."]}   <- the broker id of our OWN stop, order 608
        #
        # It only bites when the stop covers everything we are selling, which is why the same
        # rebalance sold AAPL and NVO fine (stop covered 1 of 4, and none, respectively) and only
        # the FactorRanker-owned SPCX position failed. ``is_closing_order=True`` does NOT help --
        # it only skips the opposite-position safety check (see AlpacaAccount._submit_order_impl).
        # The classic exit path avoids this because ``close_transaction`` cancels first; this
        # bypass path reimplemented the exit without that step.
        released = self._release_protective_legs(self._working_protective_legs(transactions))

        # Reduce/exit — attach to the existing OPENED transaction.
        order = TradingOrder(
            account_id=self.account_id, symbol=symbol, quantity=qty,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            transaction_id=transactions[0].id, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment="FactorRanker rebalance sell",
            data={"fixed_quantity": True},
        )
        result = self.account.submit_order(order, is_closing_order=True)
        self._reprotect_remainder(symbol, qty, transactions, released, result)
        return result

    def _reprotect_remainder(self, symbol: str, sold_qty: int, transactions: list,
                             released: list, sell_order: Optional[TradingOrder]) -> None:
        """Re-attach a stop over whatever the reduce left behind.

        A PARTIAL reduce would otherwise leave the remainder naked: we just cancelled the only
        protective order, and nothing else re-places one until the next rebalance happens to BUY
        this name again. Releasing the leg to fix the exit must not quietly create an unprotected
        position -- that would trade one bug for a worse one.

        The replacement carries the RELEASED leg's own price, not a freshly computed one. Only the
        quantity may change (it must -- it is protecting fewer shares), so a partial reduce cannot
        move a stop that the strategy already chose, and backtest results stay identical to a run
        where the leg had simply been resized. A FULL exit leaves nothing to protect and is
        skipped, which is the common rebalance case (and exactly what order 630 was).
        """
        held = sum((t.open_qty or 0) for t in (transactions or []))
        remaining = held - sold_qty
        if remaining <= 0 or not released:
            return
        # Prefer a stop price; a released TP-only leg has limit_price instead.
        source_leg = next((l for l in released if l.stop_price), released[0])
        price = source_leg.stop_price or source_leg.limit_price
        if not price or price <= 0:
            logger.warning(
                f"FactorRanker[{self.expert_instance_id}]: released leg {source_leg.id} for "
                f"{symbol} carried no usable price — {remaining} share(s) left UNPROTECTED")
            return
        try:
            replacement = TradingOrder(
                account_id=self.account_id, symbol=symbol, quantity=remaining,
                side=source_leg.side, order_type=source_leg.order_type,
                stop_price=source_leg.stop_price, limit_price=source_leg.limit_price,
                transaction_id=source_leg.transaction_id, status=OrderStatus.PENDING,
                open_type=OrderOpenType.AUTOMATIC,
                # Depend on the reducing sell so the leg goes live only once the reduce has
                # FILLED -- submitting it beforehand would reserve shares the sell still needs
                # and reproduce the very rejection this method exists to avoid.
                depends_on_order=(sell_order.id if sell_order is not None else None),
                depends_order_status_trigger=OrderStatus.FILLED,
                comment=f"FactorRanker protective leg resized after partial reduce "
                        f"(replaces order {source_leg.id})",
            )
            add_instance(replacement)
            self.account.submit_order(replacement)
            logger.info(
                f"FactorRanker[{self.expert_instance_id}]: re-protected {symbol} x{remaining} at "
                f"{price} after reducing {sold_qty} (replaces released order {source_leg.id})")
        except Exception as e:  # noqa: BLE001 — surface loudly; the reduce itself already went in
            logger.error(
                f"FactorRanker[{self.expert_instance_id}]: FAILED to re-protect {symbol} "
                f"x{remaining} after a partial reduce — position is UNPROTECTED: {e}",
                exc_info=True)
