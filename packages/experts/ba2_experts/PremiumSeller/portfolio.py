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

import ba2_common.core.OptionRiskManagement as option_rm
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.instance_resolver import get_instance_resolver
from ba2_common.core.models import ExpertInstance, Transaction
from ba2_common.core.option_book import BreakerState, CandidateStructure, admit, update_breaker
from ba2_common.core.option_lifecycle import put_assignment_cost
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.trade_store import orders_where
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, TransactionStatus,
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

    # -- settings ------------------------------------------------------
    def _s(self, name: str):
        return self.expert.get_setting_with_interface_default(name, log_warning=False)

    # -- the drawdown breaker: ONE state, shared with the entry gate ----
    # ``_peak_equity`` and ``_halted`` used to be attributes on this manager, which meant a
    # breaker only this expert could see. They are now VIEWS on
    # ``OptionRiskManagement``'s per-sleeve store -- the same map ``option_book.check_rails``
    # reads to decline every candidate while the sleeve stands down, and the same one the
    # live exit pass writes. The arithmetic is ``option_book.update_breaker``'s and is not
    # reproduced here; these accessors exist so a caller can read or seed the latch.
    @property
    def _breaker(self) -> BreakerState:
        return option_rm.get_breaker_state(self.expert_instance_id)

    @property
    def _peak_equity(self) -> Optional[float]:
        return self._breaker.peak_equity

    @_peak_equity.setter
    def _peak_equity(self, value: Optional[float]) -> None:
        state = self._breaker
        option_rm.set_breaker_state(
            self.expert_instance_id,
            BreakerState(value, state.halted, state.tripped, state.detail, state.blind))

    @property
    def _halted(self) -> bool:
        return self._breaker.halted

    @_halted.setter
    def _halted(self, value: bool) -> None:
        state = self._breaker
        option_rm.set_breaker_state(
            self.expert_instance_id,
            BreakerState(state.peak_equity, bool(value), state.tripped, state.detail,
                         state.blind))

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

    # -- rails ---------------------------------------------------------
    # THE STOPGAP IS GONE. ``_txn_metrics`` / ``_book_totals`` / ``_within_rails`` used to
    # live here: a private, second implementation of the sleeve rails carrying defects the
    # shared modules document as FIXED — it bucketed legs by order side and never netted
    # (so a buy-to-close raised committed capital instead of lowering it), it computed a
    # vertical's width as ``min(short) - max(long)`` (right only for a put vertical, and it
    # booked every call vertical and iron condor as naked at full notional), it returned
    # ``(True, 0.0, 0.0)`` for a structure whose legs it could not see (an unknown reading
    # as a free trade), and it enforced neither the concurrent cap ordering, the
    # one-per-underlying rule as part of the same pass, nor assignment capacity at all.
    #
    # Design 2026-08-27 SS4 (operator decision, 2026-08-30): "PremiumSeller's stopgap is
    # DELETED, not migrated — replaced by the shared modules. No second implementation
    # survives." What replaces it is ``option_book.admit`` over ``OptionRiskManagement``'s
    # book — the same call the shared entry gate makes for every other expert, so this
    # sleeve and a ``classic_options`` sleeve are now measured by one implementation.
    def _rails(self):
        """(the rails this expert declares, the required ones it does not)."""
        return option_rm.rail_settings(self.expert)

    def _candidate(self, spec) -> CandidateStructure:
        """One ``StructureSpec``, as the value the rails price.

        ``max_loss`` and ``notional`` are the spec's own measured numbers (the builders
        compute them from real quotes). ``short_put_assignment`` is
        ``option_lifecycle.put_assignment_cost`` per short put leg — the single shared
        definition, so the sleeve rail and the account-wide gate cannot fork.
        """
        assignment = 0.0
        for leg in spec.legs:
            if leg.side != OrderDirection.SELL or leg.option_type != OptionRight.PUT:
                continue
            cost = put_assignment_cost(leg.strike, spec.qty, 100)
            if cost is None:
                assignment = None
                break
            assignment += cost
        return CandidateStructure(
            underlying=spec.underlying, strategy=spec.strategy,
            max_loss=spec.max_loss, notional=spec.notional,
            short_put_assignment=assignment)

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
        specs = list((targets or {}).get("structures") or [])
        if not specs:
            return []
        rails, missing = self._rails()
        if missing:
            logger.error(f"PremiumSeller[{self.expert_instance_id}]: no {', '.join(missing)} "
                         f"declared — refusing to open structures rather than substituting "
                         f"a default for a risk rail")
            return []

        held, unbuildable = option_rm.sleeve_structures(self.expert_instance_id)
        book = option_rm.sleeve_book_from(held, unbuildable)
        equity = self.account.get_balance()
        cash = option_rm.assignment_cash(self.account, self.expert_instance_id)
        breaker = option_rm.get_breaker_state(self.expert_instance_id)

        # ONE call, one verdict per spec, in order — the stand-down, the two caps, the
        # three percentage rails and assignment capacity, each charged to the RUNNING
        # sleeve so N individually-affordable structures cannot collectively breach a cap.
        verdicts = admit([self._candidate(spec) for spec in specs], book, equity, rails,
                         breaker, cash)

        submitted: List[Any] = []
        for spec, verdict in zip(specs, verdicts):
            if not verdict.allowed:
                logger.info(f"PremiumSeller: rails decline {spec.strategy} on "
                            f"{spec.underlying} — {verdict.reason}: {verdict.detail}")
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
        logger.info(f"PremiumSeller[{self.expert_instance_id}]: opened {len(submitted)} structures")
        return submitted

    # -- exits (engine: manage_open) ------------------------------------
    def manage_open(self, as_of: datetime) -> List[Any]:
        """Per-structure exit rules in spec §5 priority order; circuit breaker first.

        THE BREAKER ARITHMETIC IS NOT HERE. ``option_book.update_breaker`` owns the peak
        ratchet, the peak-to-trough test, the latch and the recovery line, and this method
        is one of its two callers (the live exit pass is the other). What was here before —
        a hand-rolled ratchet, a hand-rolled trip and a hand-rolled re-arm at half the trip
        depth — was a second implementation of the same three rules, and the constant it
        pinned by hand (0.5) had to be kept in step with
        ``BREAKER_REARM_DEPTH_FRACTION`` by a test rather than by construction.

        The state it produces is stored where the ENTRY gate reads it, so a stand-down
        decided on this bar declines every structure ``rebalance`` would open on the next.
        """
        holdings = self.get_option_holdings()
        # Every evaluation ratchets and tests, INCLUDING a flat one. Returning early on an
        # empty book stopped the sleeve tracking its peak, so a just-flattened sleeve
        # measured its next drawdown from the trough — i.e. from no drawdown at all.
        state = update_breaker(self._breaker, self.account.get_balance(),
                               {"circuit_breaker_pct": float(self._s("circuit_breaker_pct"))})
        option_rm.set_breaker_state(self.expert_instance_id, state)
        if not holdings:
            return []
        if state.tripped:
            # The EDGE, not the latch: the bar the book is flattened on. A sleeve already
            # standing down has a flat book, and re-issuing closes every bar would pay the
            # spread on nothing.
            logger.warning(f"PremiumSeller[{self.expert_instance_id}]: {state.detail}")
            # Filter Nones like the normal path below: _close_structure returns None
            # when a structure has nothing left to offset (already flat).
            return [o for o in (self._close_structure(txn, parent)
                                for txn, parent in holdings.values())
                    if o is not None]
        if state.halted:
            return []
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
