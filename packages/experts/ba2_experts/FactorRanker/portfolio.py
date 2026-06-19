"""Rebalance math and execution for FactorRanker.

``rebalance_deltas`` is pure (target weights + holdings + prices -> signed share
deltas) and unit tested directly. ``FactorPortfolioManager`` (added later) wraps it
with DB/account access to actually submit the orders.
"""

import math
from collections import namedtuple
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlmodel import select

from ba2_common.core.db import add_instance, get_db, get_instance
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
        for this expert's OPENED transactions.

        The OPENED-Transaction set is expert-scoped (``Transaction.expert_id``) and is the source
        of truth for WHICH symbols/txns belong to THIS expert (needed to attach a sell to a txn id
        in ``_submit_sell``) and for each txn's cost basis (``open_price``/``open_qty``).

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
            with get_db() as session:
                transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id == self.expert_instance_id)
                    .where(Transaction.status == TransactionStatus.OPENED)
                ).all()
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
        prices = {s: self.account.get_instrument_current_price(s) for s in symbols}

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
        logger.info(
            f"FactorRanker[{self.expert_instance_id}]: rebalance submitted {len(submitted)} orders "
            f"(equity={equity:.2f}, deltas={deltas})"
        )
        return submitted

    # ------------------------------------------------------------------
    # Per-name EQUITY-loss stop (reuses risk_per_trade_pct)
    # ------------------------------------------------------------------

    def apply_stop_losses(self, risk_pct: float, equity: Optional[float] = None,
                          prices: Optional[Dict[str, float]] = None) -> List[TradingOrder]:
        """Sell (full exit) every held name whose unrealized loss has reached risk_pct% of
        equity (per-name EQUITY-loss stop; reuses risk_per_trade_pct). Computes each symbol's
        quantity-weighted avg entry cost from its OPENED transactions, uses get_virtual_balance()
        for equity when not supplied, prices held names off the account when not supplied, runs
        stop_loss_sells, and submits a market sell per stopped name via _submit_sell. Returns the
        submitted orders. Non-positive risk_pct or no holdings -> no-op. Mirrors rebalance()'s
        structure/logging.
        """
        held, by_symbol = self.get_holdings()
        if not risk_pct or risk_pct <= 0 or not held:
            return []

        # Quantity-weighted avg entry COST per symbol from its OPENED transactions' recorded
        # ``open_price``. This deliberately uses the per-transaction ``open_price`` (NOT the
        # account ledger's ``_Position.avg_price``): the two are NOT identical once a name is
        # ADDED TO across rebalances — ``open_price`` is the transaction's recorded entry price
        # (stable per transaction) while the ledger ``avg_price`` re-weights across every fill.
        # FactorRanker repeatedly adds to positions, so sourcing avg-cost from the ledger here
        # would CHANGE stop-loss decisions (verified divergence). The qty NUMBERS still come from
        # the ledger via ``held`` (identity-exact — see get_holdings); only the cost basis stays
        # on the existing DB ``open_price`` path so stop results are byte-identical.
        positions: Dict[str, tuple] = {}
        for symbol, transactions in by_symbol.items():
            total_qty = 0.0
            cost_qty = 0.0
            for trans in transactions:
                qty = trans.open_qty
                open_price = trans.open_price
                if qty is None or qty <= 0 or open_price is None or open_price <= 0:
                    continue
                total_qty += qty
                cost_qty += open_price * qty
            if total_qty <= 0:
                continue
            positions[symbol] = (cost_qty / total_qty, total_qty)

        if not positions:
            return []

        if equity is None:
            equity = self.expert.get_virtual_balance()
        if equity is None:
            logger.warning(
                f"FactorRanker[{self.expert_instance_id}]: virtual balance (equity) not "
                f"available for stop-loss; skipping"
            )
            return []

        if prices is None:
            prices = {s: self.account.get_instrument_current_price(s) for s in positions}

        sells = stop_loss_sells(positions, prices, equity, risk_pct)

        submitted: List[TradingOrder] = []
        for symbol, qty in sells.items():
            order = self._submit_sell(symbol, qty, by_symbol.get(symbol, []))
            if order is not None:
                submitted.append(order)
        logger.info(
            f"FactorRanker[{self.expert_instance_id}]: stop-loss submitted {len(submitted)} sells "
            f"(risk_pct={risk_pct}, equity={equity:.2f}, stopped={sells})"
        )
        return submitted

    def _submit_delta(self, symbol: str, delta: int, transactions: list) -> Optional[TradingOrder]:
        if delta > 0:
            return self._submit_buy(symbol, delta, transactions)
        if delta < 0:
            return self._submit_sell(symbol, -delta, transactions)
        return None

    def _submit_buy(self, symbol: str, qty: int, transactions: list) -> Optional[TradingOrder]:
        if qty <= 0:
            return None
        if transactions:
            # Adding to an existing position — link to its OPENED transaction.
            transaction_id = transactions[0].id
        else:
            # New position — pre-create an expert-attributed transaction so the
            # holding is recognised on the next rebalance (attribution path 1).
            price = self.account.get_instrument_current_price(symbol)
            trans = Transaction(
                symbol=symbol, quantity=qty, side=OrderDirection.BUY,
                status=TransactionStatus.WAITING, open_price=price,
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
        return self.account.submit_order(order, is_closing_order=False)

    def _submit_sell(self, symbol: str, qty: int, transactions: list) -> Optional[TradingOrder]:
        if qty <= 0 or not transactions:
            return None
        # Reduce/exit — attach to the existing OPENED transaction.
        order = TradingOrder(
            account_id=self.account_id, symbol=symbol, quantity=qty,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            transaction_id=transactions[0].id, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment="FactorRanker rebalance sell",
            data={"fixed_quantity": True},
        )
        return self.account.submit_order(order, is_closing_order=True)
