"""Options capability interface — a sibling mixin to AccountInterface.

Brokers that support options inherit BOTH, e.g.:
    class AlpacaAccount(AccountInterface, OptionsAccountInterface): ...

Capability detection elsewhere should use isinstance(account, OptionsAccountInterface).
The concrete submit_option_order() owns TradingOrder/Transaction persistence and
delegates the broker call to the abstract _submit_option_order_impl().
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, List, Optional

from ba2_common.core.option_types import OptionContract, OptionQuote, OptionLeg, OptionPosition
from ba2_common.core.types import OptionRight


class OptionsAccountInterface(ABC):
    """Mixin granting an AccountInterface subclass option-trading capability."""

    supports_options: bool = True

    # --- Market data -------------------------------------------------------
    @abstractmethod
    def get_option_chain(
        self,
        underlying: str,
        expiry_min: date,
        expiry_max: date,
        option_type: Optional[OptionRight] = None,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
    ) -> List[OptionContract]:
        """Return chain rows (quote + Greeks + liquidity) within the filters."""
        ...

    @abstractmethod
    def get_option_quote(self, contract_symbol: str) -> Optional[OptionQuote]:
        """Latest quote + Greeks for one OCC contract."""
        ...

    @abstractmethod
    def get_atm_implied_volatility(self, underlying: str) -> Optional[float]:
        """Current near-ATM implied volatility for the underlying (0-1)."""
        ...

    # --- Positions ---------------------------------------------------------
    @abstractmethod
    def get_option_positions(self) -> List[OptionPosition]:
        """All currently-held option positions."""
        ...

    # --- Orders ------------------------------------------------------------
    @abstractmethod
    def _submit_option_order_impl(self, trading_order, legs: List[OptionLeg],
                                  leg_orders: Optional[List[Any]] = None) -> Any:
        """Broker-specific submit. Receives the persisted parent TradingOrder and
        the legs; must set broker ids/status and return the parent order."""
        ...

    def submit_option_order(
        self,
        legs: List[OptionLeg],
        quantity: int,
        order_type: str = "limit",            # "market" | "limit"
        limit_price: Optional[float] = None,   # premium; +debit / -credit for spreads
        option_strategy: Optional[str] = None,
        expert_recommendation_id: Optional[int] = None,
        transaction_id: Optional[int] = None,
    ) -> Any:
        """Build & persist option TradingOrder(s), then submit to the broker.

        single leg -> one option TradingOrder (contract_symbol set)
        2-4 legs   -> a parent option order (option_strategy set, no contract_symbol)
                      + leg children linked via parent_order_id.
        """
        from ba2_common.core.db import add_instance, get_instance, update_instance
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import AssetClass, OrderDirection, OrderType as CoreOrderType, OrderStatus
        from ba2_common.logger import logger

        if not legs:
            raise ValueError("submit_option_order requires at least one leg")
        if len(legs) > 4:
            raise ValueError("Alpaca supports a maximum of 4 option legs")

        first = legs[0]
        is_multi = len(legs) > 1
        if limit_price is None:
            net_side = first.side
        else:
            net_side = OrderDirection.BUY if limit_price >= 0 else OrderDirection.SELL

        side_for_type = net_side if is_multi else first.side
        if order_type == "market":
            core_type = CoreOrderType.MARKET
        else:
            core_type = CoreOrderType.BUY_LIMIT if side_for_type == OrderDirection.BUY else CoreOrderType.SELL_LIMIT

        parent = TradingOrder(
            account_id=self.id,
            symbol=(first.underlying or first.contract_symbol),
            underlying_symbol=first.underlying,
            quantity=quantity,
            side=(first.side if not is_multi else net_side),
            order_type=core_type,
            status=OrderStatus.PENDING,
            limit_price=limit_price,
            asset_class=AssetClass.OPTION,
            multiplier=100,
            option_strategy=option_strategy or ("spread" if is_multi else "single"),
            position_intent=(first.position_intent if not is_multi else None),
            contract_symbol=(first.contract_symbol if not is_multi else None),
            option_type=(first.option_type if not is_multi else None),
            strike=(first.strike if not is_multi else None),
            expiry=(first.expiry if not is_multi else None),
            expert_recommendation_id=expert_recommendation_id,
            transaction_id=transaction_id,
        )
        parent_id = add_instance(parent, expunge_after_flush=True)
        parent = get_instance(TradingOrder, parent_id)

        # Create/link a Transaction so OPEN_POSITIONS rules can manage the position.
        if parent.transaction_id is None and hasattr(self, "_create_transaction_for_order"):
            self._create_transaction_for_order(parent)
            update_instance(parent)
            parent = get_instance(TradingOrder, parent_id)

        leg_orders = []
        if is_multi:
            for leg in legs:
                child = TradingOrder(
                    account_id=self.id,
                    symbol=leg.contract_symbol,
                    underlying_symbol=leg.underlying,
                    quantity=quantity * leg.ratio_qty,
                    side=leg.side,
                    order_type=(CoreOrderType.MARKET if order_type == "market" else (
                        CoreOrderType.BUY_LIMIT if leg.side == OrderDirection.BUY else CoreOrderType.SELL_LIMIT)),
                    status=OrderStatus.PENDING,
                    asset_class=AssetClass.OPTION,
                    multiplier=100,
                    contract_symbol=leg.contract_symbol,
                    option_type=leg.option_type,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    position_intent=leg.position_intent,
                    parent_order_id=parent.id,
                    transaction_id=parent.transaction_id,
                )
                child_id = add_instance(child, expunge_after_flush=True)
                leg_orders.append(get_instance(TradingOrder, child_id))

        try:
            return self._submit_option_order_impl(parent, legs, leg_orders or None)
        except Exception as e:
            logger.error(f"Option order submission failed for {parent.symbol}: {e}", exc_info=True)
            parent.status = OrderStatus.ERROR
            parent.comment = f"{(parent.comment or '')} | option submit error: {str(e)[:200]}"
            update_instance(parent)
            return None

    @abstractmethod
    def close_option_position(self, position: OptionPosition,
                              order_type: str = "limit",
                              limit_price: Optional[float] = None) -> Any:
        """Submit a closing order for a held option position (opposite intent)."""
        ...

    # --- IV rank (self-computed from stored ATM-IV history) ----------------
    @staticmethod
    def _iv_rank_from_series(series, current, min_samples: int = 20):
        """Percentile (0-100) of `current` against `series`, or None.

        None entries in `series` are ignored. Returns None when `current` is
        None or fewer than `min_samples` valid samples exist. Counts strictly
        below `current`.
        """
        vals = [v for v in series if v is not None]
        if current is None or len(vals) < min_samples:
            return None
        below = sum(1 for v in vals if v < current)
        return round(below / len(vals) * 100, 2)

    def record_atm_iv(self, underlying: str, iv: Optional[float] = None) -> Optional[int]:
        """Persist one ATM-IV sample for the trailing series. Returns the row id."""
        from ba2_common.core.db import add_instance
        from ba2_common.core.models import OptionIVSnapshot
        if iv is None:
            iv = self.get_atm_implied_volatility(underlying)
        if iv is None:
            return None
        return add_instance(OptionIVSnapshot(account_id=self.id, underlying=underlying, atm_iv=iv))

    def get_iv_rank(self, underlying: str, lookback_days: int = 252,
                    min_samples: int = 20) -> Optional[float]:
        """IV percentile (0-100) over the stored trailing window, or None if
        insufficient history."""
        from datetime import datetime, timezone, timedelta
        from sqlmodel import select
        from ba2_common.core.db import get_db
        from ba2_common.core.models import OptionIVSnapshot
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        with get_db() as session:
            rows = session.exec(
                select(OptionIVSnapshot).where(
                    OptionIVSnapshot.account_id == self.id,
                    OptionIVSnapshot.underlying == underlying,
                    OptionIVSnapshot.recorded_at >= cutoff,
                )
            ).all()
            series = [r.atm_iv for r in rows]   # read while session is open
        current = self.get_atm_implied_volatility(underlying)
        return self._iv_rank_from_series(series, current, min_samples)

    # --- Cash / buying-power reserve (short-premium defense-in-depth) -------
    #: Reg-T / CBOE naked-option initial-margin fraction of the underlying notional.
    #: A NAKED short option is NOT cash-secured (only an assigned cash-secured PUT is);
    #: brokers margin it at ~20% of notional less OTM amount, floored at ~10%. Reserving
    #: the FULL strike*100 (cash-secured proxy) made naked structures (short straddle/
    #: strangle, jade lizard, put ratio spread) impossible to size on a realistic account
    #: ($10k can't reserve $22k for one AAPL contract), so they never opened. The margin
    #: model below mirrors how a broker actually reserves a naked short.
    NAKED_MARGIN_FRACTION = 0.20
    NAKED_MARGIN_FLOOR_FRACTION = 0.10

    @classmethod
    def naked_margin_per_contract(cls, strike: float, *, option_type: OptionRight,
                                  spot: float | None = None) -> float:
        """Reg-T naked single-option initial margin for ONE contract (x100 multiplier).

        Reg-T / CBOE naked-short initial margin is::

            premium + max(20% * underlying - OTM_amount, 10% * underlying)

        where the OTM amount is 0 for an ITM option (NEVER negative — an ITM short
        carries the full 20%-of-notional requirement, there is no OTM deduction):
          * CALL: OTM_amount = max(strike - spot, 0)
          * PUT:  OTM_amount = max(spot - strike, 0)

        The PREMIUM term is NOT added here: this returns only the per-share bracket
        x100, and the collected premium already sits in the account's cash where it
        offsets the requirement (callers reserve/margin the bracket only). ``spot``
        is used when known; without it, falls back to ``0.20*strike*100`` (OTM term
        dropped) — still ~5x cheaper than the old full strike*100 cash proxy."""
        if strike is None or strike <= 0:
            return 0.0
        if spot is None or spot <= 0:
            return cls.NAKED_MARGIN_FRACTION * strike * 100.0
        if option_type == OptionRight.CALL:
            otm = max(float(strike) - float(spot), 0.0)
        else:
            otm = max(float(spot) - float(strike), 0.0)
        primary = cls.NAKED_MARGIN_FRACTION * spot - otm
        floor = cls.NAKED_MARGIN_FLOOR_FRACTION * spot
        return max(primary, floor) * 100.0

    @classmethod
    def option_reserve_required(cls, strategy: str, quantity: int, *, strike: float | None = None,
                               spread_width: float | None = None, net_credit: float | None = None,
                               spot: float | None = None, option_type: OptionRight | None = None) -> float:
        """Cash/BP that a short-premium strategy must reserve. 0 for long/debit strategies.

        ``option_type`` is REQUIRED for the single-sided naked strategies
        (``short_strangle``/``naked_put``/``put_ratio_spread``) — the Reg-T OTM amount
        is direction-aware, so a missing right would silently misstate the reserve.
        ``short_straddle`` needs none: it shorts BOTH rights at the same strike and
        Reg-T margins a straddle at the GREATER of the two legs, so the worst case
        over both rights is reserved."""
        if quantity <= 0:
            return 0.0
        if strategy == "cash_secured_put":
            # A CSP is fully cash-secured by definition (the cash to buy the assigned
            # shares is set aside): reserve the full assignment cost.
            if strike is None:
                return 0.0
            return strike * 100.0 * quantity
        if strategy in ("bear_call_spread", "credit_spread"):
            if spread_width is None or net_credit is None:
                return 0.0
            max_loss = (spread_width - net_credit)
            return max(0.0, max_loss) * 100.0 * quantity
        if strategy in ("short_straddle", "short_strangle", "naked_put"):
            # NAKED short premium: reserve the Reg-T naked-option margin, not full cash.
            if strike is None:
                return 0.0
            if strategy == "short_straddle":
                # Both a short call AND a short put at the SAME strike: reserve the
                # worst-case leg (only one side can finish ITM; Reg-T charges the greater).
                return max(
                    cls.naked_margin_per_contract(strike, option_type=OptionRight.CALL, spot=spot),
                    cls.naked_margin_per_contract(strike, option_type=OptionRight.PUT, spot=spot),
                ) * quantity
            if option_type is None:
                raise ValueError(
                    f"option_reserve_required({strategy!r}) requires option_type — the Reg-T "
                    "OTM amount is direction-aware (call: strike-spot, put: spot-strike).")
            return cls.naked_margin_per_contract(strike, option_type=option_type, spot=spot) * quantity
        if strategy == "put_ratio_spread":
            # Empirically confirmed against a real Alpaca paper submission (2026-07-24, same
            # session as the jade_lizard fix): a 1-long/2-short put ratio spread is NOT given
            # Reg-T naked-margin netting for its net-1-naked-short leg, nor any credit for the
            # long leg's partial protection -- Alpaca's MLEG engine margins the WHOLE position as
            # if it were a plain naked short put at the SHORT strike (cash-secured-style full
            # notional), netted by the total credit collected. Verified EXACT to the dollar: a
            # real 30-contract order (short strike 290, net_credit 3.65) reported Alpaca
            # cost_basis=$859,050 == 30 * (290*100 - 3.65*100) = 30 * 28,635. The previous formula
            # (naked_margin_per_contract on the short strike alone) reserved only ~$3,289/contract
            # for the same order -- an ~8.7x underestimate.
            if strike is None:
                return 0.0
            credit = net_credit if net_credit is not None else 0.0
            per_contract = strike * 100.0 - credit * 100.0
            return max(0.0, per_contract) * quantity
        if strategy == "jade_lizard":
            # Empirically confirmed against a real Alpaca paper submission (2026-07-24): a 3-leg
            # combo mixing a naked short put with a defined-risk call credit spread is NOT given
            # the standard Reg-T naked-margin netting/discount -- Alpaca's MLEG risk engine
            # apparently doesn't recognize "jade lizard" as an eligible strategy for that
            # treatment (unlike the 2-leg vertical / 4-leg iron condor cases below, which ARE
            # margined at their textbook defined-risk max-loss with no issue). It instead charges
            # the naked put leg at FULL cash-secured-style notional (put_strike*100, same as a
            # standalone cash_secured_put) plus the call spread's own max-loss (spread_width*100),
            # netted by the total premium collected. Verified EXACT to the dollar: a real order
            # (put strike 290, call wing width 17.5, net_credit 3.04) reported Alpaca
            # cost_basis=$30,446 == 290*100 + 17.5*100 - 3.04*100. The previous formula (plain
            # Reg-T naked_margin_per_contract on the put strike alone, ignoring the call wing
            # entirely) reserved only ~$3,289 for that same order -- a ~10x underestimate that
            # would silently let the platform think a position is affordable when Alpaca will
            # actually reject it.
            if strike is None or spread_width is None:
                return 0.0
            credit = net_credit if net_credit is not None else 0.0
            per_contract = strike * 100.0 + spread_width * 100.0 - credit * 100.0
            return max(0.0, per_contract) * quantity
        if strategy in ("iron_condor", "call_butterfly", "debit_spread"):
            if spread_width is None:
                return 0.0
            credit = net_credit if net_credit is not None else 0.0
            return max(0.0, (spread_width - credit)) * 100.0 * quantity
        return 0.0

    def reserved_option_buying_power(self) -> float:
        """Sum of stored reserves across this account's OPEN short-premium option positions.

        A reserve belongs to the POSITION, not to the order row that created it: the broker
        frees the margin/cash the moment the structure is flattened, so this must too.

        Previously this summed ``data["option_reserve"]`` over every order not in a TERMINAL
        status — but ``FILLED`` is NOT terminal (see ``OrderStatus.get_terminal_statuses``),
        and nothing ever cleared the field or terminalised a filled entry order. The reserve
        was therefore a ONE-WAY RATCHET: every credit/naked structure ever opened consumed
        buying power for the remainder of the run, even long after it closed. On the options
        grid's $20k account that exhausted BP after 1-3 structures, which is why the RESERVING
        groups (OS2/OS3) capped out at 10-20 trades all clustered in the run's opening weeks
        while the non-reserving debit groups (OS1/OS4) traded 43-214 times over the identical
        window — and why the GA appeared to "win by barely trading" (it could not trade).

        Now a reserve counts only while its owning transaction is still open. WAITING/OPENED/
        CLOSING all still hold the position (a submitted-but-unfilled close has not freed
        anything yet); CLOSED/FAILED release it. A reserve-carrying order with no transaction
        yet (submitted, not linked) is still counted — that capital is genuinely in flight.
        """
        from ba2_common.core.trade_store import orders_where, transactions_where
        from ba2_common.core.types import AssetClass, OrderStatus, TransactionStatus

        terminal = OrderStatus.get_terminal_statuses()
        unlinked_total = 0.0
        linked: list = []  # (transaction_id, reserve)
        for o in orders_where(account_id=self.id, not_statuses=terminal):
            if o.asset_class != AssetClass.OPTION:
                continue
            reserve = float((o.data or {}).get("option_reserve", 0) or 0)
            if reserve <= 0:
                continue
            if o.transaction_id is None:
                unlinked_total += reserve
            else:
                linked.append((o.transaction_id, reserve))
        if not linked:
            return unlinked_total
        # One bulk lookup of the OPEN book (small — bounded by held positions), not N queries.
        live_ids = {
            t.id for t in transactions_where(
                not_statuses=(TransactionStatus.CLOSED, TransactionStatus.FAILED))
        }
        return unlinked_total + sum(r for txn_id, r in linked if txn_id in live_ids)

    def available_option_buying_power(self) -> float:
        bal = self.get_balance() or 0.0
        return bal - self.reserved_option_buying_power()

    def check_option_buying_power(self, required: float) -> bool:
        """True if `required` reserve fits in available buying power."""
        if required <= 0:
            return True
        return required <= self.available_option_buying_power()
