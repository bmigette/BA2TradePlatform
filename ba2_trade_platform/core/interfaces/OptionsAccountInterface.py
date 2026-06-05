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

from ..option_types import OptionContract, OptionQuote, OptionLeg, OptionPosition
from ..types import OptionRight


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
        from ...core.db import add_instance, get_instance, update_instance
        from ...core.models import TradingOrder
        from ...core.types import AssetClass, OrderDirection, OrderType as CoreOrderType, OrderStatus
        from ba2_trade_platform.logger import logger

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
        from ...core.db import add_instance
        from ...core.models import OptionIVSnapshot
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
        from ...core.db import get_db
        from ...core.models import OptionIVSnapshot
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
