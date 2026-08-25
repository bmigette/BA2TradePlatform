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
        contract_symbol None — and single-leg 'single' parents).

        Reads via the dual-path ``transactions_where`` accessor: a raw SQL session is
        BLIND under the backtest's in-memory trade store (``backtest_trading_db``'s
        default), where Transaction rows live in the RAM dict store — with a raw
        session this returned {} forever, so manage_open never closed anything and
        rebalance re-opened duplicate structures every entry bar. Flag-OFF (live)
        the accessor issues the identical SELECT, so live behaviour is unchanged."""
        from ba2_common.core.trade_store import transactions_where

        txns = transactions_where(expert_id=self.expert_instance_id,
                                  status=TransactionStatus.OPENED)
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

        A circuit-breaker stand-down opens NOTHING. The latch used to short-circuit
        manage_open and nothing else, so the breaker flattened the book and this method
        re-opened it on the very next entry bar — at the bottom of the drawdown that had
        just flattened it — whereupon the next managed bar flattened it again. The sleeve
        could run that cycle for the rest of the campaign, paying the spread every round,
        and (once re-arming was conditioned on a real re-entry) each round also cleared
        the halt, so the breaker's only lasting effect was the round trip.

        The halt is NOT cleared here. What clears it is a recovery, tested in manage_open
        on every evaluation including a flat one — see the comment there for why it is
        neither an entry (unreachable now that entry is gated: that is the deadlock) nor
        a bar count (the peak is kept, so a sleeve re-armed under water re-trips at once
        and the cycle merely runs slower)."""
        if self._halted:
            logger.info(f"PremiumSeller[{self.expert_instance_id}]: standing down after "
                        f"the circuit breaker — opening no new structures")
            return []
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
        # Ratchet the peak on EVERY evaluation, INCLUDING a flat sleeve — this must stay
        # above the `not holdings` early return. A sleeve the breaker just flattened is
        # flat, so returning first stopped it tracking its peak entirely and it would then
        # measure its next drawdown from wherever equity stood on re-entry (i.e. from the
        # trough), which is no drawdown at all. Same rule as option_book.update_breaker.
        balance = self.account.get_balance()
        if balance is not None:
            self._peak_equity = balance if self._peak_equity is None else max(self._peak_equity, balance)
        breaker = float(self._s("circuit_breaker_pct"))
        # Clearing a stand-down: a RECOVERY, and nothing else. This also has to sit above
        # the `not holdings` early return — a standing-down sleeve is flat by definition
        # (the breaker just flattened it) and rebalance now opens nothing while halted, so
        # a clear evaluated only on a sleeve that holds something could never be reached.
        #
        # Why not the alternatives:
        #   * a successful entry (what 46195b1 used) is unreachable once entry is gated:
        #     blocked because halted, halted because never entered. That is the deadlock.
        #   * a bar count / cool-off re-arms a sleeve that is still under water, and the
        #     peak is deliberately KEPT, so it trips again on its first managed bar — the
        #     same open/flatten cycle, just slower.
        #   * an operator reset alone leaves the only exit outside the system.
        # A recovery is the one condition under which resuming does not immediately
        # re-trip. The re-arm line is HALF the trip depth (trip -20% -> re-arm -10%):
        # re-arming on the trip line itself leaves no hysteresis and the sleeve flaps.
        # Kept numerically identical to option_book.BREAKER_REARM_DEPTH_FRACTION (0.5),
        # which Task 8 rewires onto; test_the_stopgap_and_option_book_agree_on_the_rearm_line
        # fails if the two ever drift.
        if self._halted:
            peak = self._peak_equity
            if (balance is not None and peak is not None and peak > 0
                    and balance >= peak * (1.0 - 0.5 * breaker / 100.0)):
                logger.info(f"PremiumSeller[{self.expert_instance_id}]: circuit-breaker "
                            f"stand-down cleared — equity {balance} recovered to within "
                            f"{0.5 * breaker}% of peak {peak}")
                self._halted = False
        if not holdings:
            return []
        if self._halted:
            return []
        # BOTH operands are Optional[float] and BOTH are tested explicitly — never by
        # truthiness, which cannot tell "not measured" from a legitimate 0.0:
        #   balance is None -> equity was not measured; blind, and blind is not a trip.
        #   balance == 0.0  -> a 100% drawdown, the deepest there is. It MUST trip.
        # A peak-to-trough drawdown is likewise only defined against a POSITIVE peak
        # (`and self._peak_equity` read a peak of exactly 0.0 as "no breaker", and a
        # negative peak as a usable baseline — against which `peak x 0.8` sits ABOVE
        # the peak and fired on a phantom drawdown). Same three-way split as
        # option_book.update_breaker: unknown / unusable-peak / measured.
        peak = self._peak_equity
        if (balance is not None and peak is not None and peak > 0
                and balance <= peak * (1.0 - breaker / 100.0)):
            logger.warning(f"PremiumSeller: circuit breaker hit (dd>{breaker}%) — flattening book")
            self._halted = True
            # Filter Nones like the normal path below: _close_structure returns None
            # when a structure has nothing left to offset (already flat).
            return [o for o in (self._close_structure(txn, parent)
                                for txn, parent in holdings.values())
                    if o is not None]
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
        """True iff any CURRENTLY-SHORT contract's |delta| >= tested_delta threshold.

        Currently-short is found by per-contract netting over the transaction's
        executed option orders — the same netting _close_structure performs (BUY
        +qty, SELL −qty) — because combo child legs carry parent_order_id and a
        parent-only filter can never see them. Each net-short contract's delta comes
        from a chain lookup (quotes carry no greeks) using the underlying/expiry on
        that contract's most recent order row. Missing chain rows or None deltas ->
        False (no action this bar)."""
        executed = OrderStatus.get_executed_statuses()
        net: Dict[str, float] = {}
        meta: Dict[str, Any] = {}
        for o in orders_where(transaction_id=parent.transaction_id):
            if getattr(o, "asset_class", None) != AssetClass.OPTION or not getattr(o, "contract_symbol", None):
                continue
            if o.status not in executed:
                continue
            sign = 1.0 if o.side == OrderDirection.BUY else -1.0
            net[o.contract_symbol] = net.get(o.contract_symbol, 0.0) + sign * float(o.filled_qty or o.quantity or 0.0)
            meta[o.contract_symbol] = o
        threshold = float(self._s("tested_delta"))
        for contract, n in net.items():
            if n >= 0:
                continue
            o = meta[contract]
            expiry = o.expiry
            chain = self.account.get_option_chain(o.underlying_symbol, expiry, expiry)
            for c in chain:
                if c.symbol == contract and c.delta is not None:
                    if abs(c.delta) >= threshold:
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
