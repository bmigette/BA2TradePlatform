"""OptionPortfolioManager — owns the PremiumSeller book lifecycle (spec §3.2/§5/§6).

This manager IS the risk manager for the sleeve (the expert bypasses the classic
RM): every rail is enforced here, from explicit expert settings, with no silent
defaults. Opens go through OptionsAccountInterface.submit_option_order with a
pre-created expert-attributed Transaction (the FactorPortfolioManager attribution
pattern); closes submit offsetting legs on the SAME transaction — the B10
per-contract netting in refresh_transactions resolves the lifecycle from there.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.instance_resolver import get_instance_resolver
from ba2_common.core.models import ExpertInstance, Transaction
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.trade_store import orders_where
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderStatus, TransactionStatus,
)
from ba2_common.logger import logger


class OptionPortfolioManager:
    def __init__(self, expert_instance_id: int):
        resolver = get_instance_resolver()
        self.expert_instance_id = expert_instance_id
        self.expert = resolver.get_expert_instance(expert_instance_id)
        instance = get_instance(ExpertInstance, expert_instance_id)
        self.account_id = instance.account_id
        self.account = resolver.get_account_instance(instance.account_id)
        self._peak_equity: Optional[float] = None
        self._halted: bool = False

    # -- settings ------------------------------------------------------
    def _s(self, name: str):
        return self.expert.get_setting_with_interface_default(name, log_warning=False)

    # -- holdings ------------------------------------------------------
    def get_option_holdings(self) -> Dict[int, Tuple[Transaction, Any]]:
        """{txn_id: (txn, parent_order)} for this expert's OPENED option structures.

        Parent order = the txn's order with parent_order_id None, asset_class OPTION,
        and an option_strategy other than 'close' (covers both multi-leg parents —
        contract_symbol None — and single-leg 'single' parents)."""
        from ba2_common.core.db import get_db
        from sqlmodel import select

        with get_db() as session:
            txns = session.exec(
                select(Transaction)
                .where(Transaction.expert_id == self.expert_instance_id)
                .where(Transaction.status == TransactionStatus.OPENED)
            ).all()
        out: Dict[int, Tuple[Transaction, Any]] = {}
        for txn in txns:
            for o in orders_where(transaction_id=txn.id):
                if getattr(o, "parent_order_id", None) is not None:
                    continue
                if getattr(o, "asset_class", None) != AssetClass.OPTION:
                    continue
                strat = getattr(o, "option_strategy", None)
                if strat and strat != "close":
                    out[txn.id] = (txn, o)
                    break
        return out

    # -- per-structure metrics (rails inputs) --------------------------
    def _txn_metrics(self, txn) -> Tuple[bool, float, float]:
        """(is_defined_risk, notional_$, committed_$) for a held structure.

        notional = max short strike x 100 x max short net qty (per-side stress basis).
        committed = notional for naked structures; width x 100 x qty for defined-risk
        (conservative: credit received is NOT netted out)."""
        executed = OrderStatus.get_executed_statuses()
        shorts: Dict[str, Tuple[float, float]] = {}
        longs: Dict[str, Tuple[float, float]] = {}
        for o in orders_where(transaction_id=txn.id):
            if getattr(o, "asset_class", None) != AssetClass.OPTION or not getattr(o, "contract_symbol", None):
                continue
            if o.status not in executed:
                continue
            qty = float(o.filled_qty or o.quantity or 0.0)
            strike = float(o.strike or 0.0)
            book = shorts if o.side == OrderDirection.SELL else longs
            q, s = book.get(o.contract_symbol, (0.0, strike))
            book[o.contract_symbol] = (q + qty, s)
        if not shorts:
            return (True, 0.0, 0.0)
        max_qty = max(q for q, _ in shorts.values())
        notional = max(s for _, s in shorts.values()) * 100.0 * max_qty
        if not longs:
            return (False, notional, notional)
        width = min(s for _, s in shorts.values()) - max(s for _, s in longs.values())
        if width <= 0:
            return (False, notional, notional)
        return (True, notional, width * 100.0 * max_qty)

    def _book_totals(self, holdings) -> Tuple[float, float, float]:
        """(total_committed, naked_committed, total_notional) over held structures."""
        total_committed = naked_committed = total_notional = 0.0
        for txn, _parent in holdings.values():
            defined, notional, committed = self._txn_metrics(txn)
            total_committed += committed
            total_notional += notional
            if not defined:
                naked_committed += committed
        return total_committed, naked_committed, total_notional

    # -- rails ---------------------------------------------------------
    def _within_rails(self, spec, holdings, book) -> bool:
        equity = self.account.get_balance()
        if equity is None or equity <= 0:
            logger.warning("PremiumSeller: no account balance — declining new structure")
            return False
        committed, naked_committed, notional = book
        if committed + spec.max_loss > float(self._s("max_deployment_pct")) / 100.0 * equity:
            return False
        if notional + spec.notional > float(self._s("max_notional_leverage")) * equity:
            return False
        if spec.strategy in ("short_put", "short_strangle"):
            if naked_committed + spec.max_loss > float(self._s("undefined_risk_max_pct")) / 100.0 * equity:
                return False
        return True

    # -- entry (engine: rebalance) --------------------------------------
    def rebalance(self, targets: Dict) -> List[Any]:
        """Open the gap between the desired structures and the held book (entry cadence).
        A new entry cycle clears a circuit-breaker stand-down."""
        self._halted = False
        structures = (targets or {}).get("structures") or []
        holdings = self.get_option_holdings()
        held_underlyings = {txn.symbol for txn, _ in holdings.values()}
        submitted: List[Any] = []
        book = list(self._book_totals(holdings))
        for spec in structures:
            if len(holdings) + len(submitted) >= int(self._s("max_concurrent_structures")):
                break
            if spec.underlying in held_underlyings:
                continue
            if not self._within_rails(spec, holdings, tuple(book)):
                logger.info(f"PremiumSeller: rails decline {spec.strategy} on {spec.underlying}")
                continue
            # Pre-create an expert-attributed transaction so the structure is
            # recognised as this expert's holding on the next cycle (same
            # attribution path FactorPortfolioManager uses; Transaction has no
            # account_id column — account flows through the order itself).
            txn = Transaction(
                symbol=spec.underlying, quantity=spec.qty, side=OrderDirection.SELL,
                status=TransactionStatus.WAITING, open_price=-abs(spec.net_credit),
                open_date=datetime.now(timezone.utc),
                expert_id=self.expert_instance_id, multiplier=100,
            )
            txn_id = add_instance(txn)
            order = self.account.submit_option_order(
                legs=spec.legs, quantity=spec.qty, order_type="limit",
                limit_price=-abs(spec.net_credit), option_strategy=spec.strategy,
                transaction_id=txn_id)
            if order is not None:
                submitted.append(order)
                held_underlyings.add(spec.underlying)
                book[0] += spec.max_loss
                book[2] += spec.notional
                if spec.strategy in ("short_put", "short_strangle"):
                    book[1] += spec.max_loss
        logger.info(f"PremiumSeller[{self.expert_instance_id}]: opened {len(submitted)} structures")
        return submitted

    # -- exits (engine: manage_open) ------------------------------------
    def manage_open(self, as_of: datetime) -> List[Any]:
        """Per-structure exit rules in spec §5 priority order; circuit breaker first."""
        holdings = self.get_option_holdings()
        if not holdings:
            return []
        balance = self.account.get_balance()
        if balance is not None:
            self._peak_equity = balance if self._peak_equity is None else max(self._peak_equity, balance)
        if self._halted:
            return []
        breaker = float(self._s("circuit_breaker_pct"))
        if (balance is not None and self._peak_equity
                and balance <= self._peak_equity * (1.0 - breaker / 100.0)):
            logger.warning(f"PremiumSeller: circuit breaker hit (dd>{breaker}%) — flattening book")
            self._halted = True
            return [self._close_structure(txn, parent)
                    for txn, parent in holdings.values()]
        closed: List[Any] = []
        for txn, parent in holdings.values():
            if self._should_close(txn, parent, as_of):
                order = self._close_structure(txn, parent)
                if order is not None:
                    closed.append(order)
        return closed

    def _structure_pnl_pct(self, txn, parent) -> Optional[float]:
        from ba2_common.core.TradeConditions import (
            _get_option_pnl_via_transaction, _get_spread_pnl_via_transaction,
        )
        res = (_get_option_pnl_via_transaction(self.account, parent)
               if getattr(parent, "contract_symbol", None)
               else _get_spread_pnl_via_transaction(self.account, parent))
        return None if res is None else res["percent"]

    def _should_close(self, txn, parent, as_of: datetime) -> bool:
        strategy = getattr(parent, "option_strategy", "") or ""
        pct = self._structure_pnl_pct(txn, parent)
        capture = (float(self._s("strangle_capture_pct")) if strategy == "short_strangle"
                   else float(self._s("profit_capture_pct")))
        if pct is not None:
            if pct >= capture:                                     # 1. profit capture
                return True
            if strategy in ("short_put", "short_strangle"):        # 5. undefined-risk stop
                if self._s("ur_stop_enabled") and pct <= -100.0 * float(self._s("ur_stop_credit_mult")):
                    return True
            elif self._s("dr_stop_enabled") and pct <= -100.0 * float(self._s("dr_stop_credit_mult")):
                return True                                        # 4. defined-risk stop
        if self._s("tested_delta_enabled") and self._tested(parent):   # 2. tested side
            return True
        expiry = getattr(parent, "expiry", None)
        if expiry is not None and (expiry - as_of.date()).days <= int(self._s("roll_dte")):
            return True                                            # 3. time stop / roll
        return False

    def _tested(self, parent) -> bool:
        """True iff any SHORT leg's |delta| >= tested_delta threshold (chain lookup —
        quotes carry no greeks). Missing chain/greeks -> False (no action this bar)."""
        executed = OrderStatus.get_executed_statuses()
        for o in orders_where(transaction_id=parent.transaction_id):
            if getattr(o, "asset_class", None) != AssetClass.OPTION or not getattr(o, "contract_symbol", None):
                continue
            if o.status not in executed or o.side != OrderDirection.SELL or o.parent_order_id is None:
                continue
            expiry = o.expiry
            chain = self.account.get_option_chain(o.underlying_symbol, expiry, expiry)
            for c in chain:
                if c.symbol == o.contract_symbol and c.delta is not None:
                    if abs(c.delta) >= float(self._s("tested_delta")):
                        return True
        return False

    def _close_structure(self, txn, parent) -> Optional[Any]:
        """Offset every still-held contract on the transaction (B10 netting closes it)."""
        executed = OrderStatus.get_executed_statuses()
        net: Dict[str, float] = {}
        meta: Dict[str, Any] = {}
        for o in orders_where(transaction_id=txn.id):
            if getattr(o, "asset_class", None) != AssetClass.OPTION or not getattr(o, "contract_symbol", None):
                continue
            if o.status not in executed:
                continue
            sign = 1.0 if o.side == OrderDirection.BUY else -1.0
            net[o.contract_symbol] = net.get(o.contract_symbol, 0.0) + sign * float(o.filled_qty or o.quantity or 0.0)
            meta[o.contract_symbol] = o
        legs: List[OptionLeg] = []
        for contract in sorted(net):
            n = net[contract]
            if abs(n) < 1e-9:
                continue
            o = meta[contract]
            legs.append(OptionLeg(
                contract_symbol=contract,
                side=(OrderDirection.SELL if n > 0 else OrderDirection.BUY),
                ratio_qty=int(abs(n)),
                position_intent=("sell_to_close" if n > 0 else "buy_to_close"),
                option_type=getattr(o, "option_type", None), strike=getattr(o, "strike", None),
                expiry=getattr(o, "expiry", None), underlying=getattr(o, "underlying_symbol", None)))
        if not legs:
            return None
        return self.account.submit_option_order(legs=legs, quantity=1, order_type="market",
                                                option_strategy="close", transaction_id=txn.id)
